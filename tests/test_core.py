from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import app as app_module
import backup_engine
import config_store
import notifications
from app import (
    BackupApp,
    backup_notification_message,
    format_notification_time,
    notification_subject,
    should_send_notification,
)
from config_store import CONFIG_PATH, LOG_DIR, ConfigStore


class BackupEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="dba_test_"))
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def test_file_backup_uses_timestamp_outer_and_original_inner_name(self) -> None:
        source = self.temp_dir / "工程资料"
        source.mkdir()
        (source / "readme.txt").write_text("content", encoding="utf-8")

        result = backup_engine.run_backup_job(
            {
                "id": "file-job",
                "name": "文件夹备份",
                "job_type": "files",
                "file_source": str(source),
                "file_skip_unreadable": True,
                "output_dir": str(self.temp_dir / "output"),
                "retention_days": 0,
            }
        )

        self.assertRegex(
            result.path.name,
            r"^【\d{14}】 文件夹备份 - 工程资料\.zip$",
        )
        self.assertRegex(result.path.parent.name, r"^\d{8}备份$")
        self.assertEqual(result.path.parent.parent, self.temp_dir / "output")
        with zipfile.ZipFile(result.path) as archive:
            self.assertIn("工程资料/readme.txt", archive.namelist())
            self.assertIn("备份说明.txt", archive.namelist())

    def test_same_second_backups_do_not_overwrite(self) -> None:
        source = self.temp_dir / "a.txt"
        source.write_text("a", encoding="utf-8")
        job = {
            "id": "same-job",
            "name": "文件备份",
            "job_type": "files",
            "file_source": str(source),
            "output_dir": str(self.temp_dir / "output"),
            "retention_days": 0,
        }
        first = backup_engine.run_backup_job(job)
        second = backup_engine.run_backup_job(job)
        third = backup_engine.run_backup_job(job)

        self.assertEqual(
            len({first.path.name, second.path.name, third.path.name}),
            3,
        )
        self.assertTrue(second.path.stem.endswith("_2"))
        self.assertTrue(third.path.stem.endswith("_3"))
        self.assertEqual(
            {first.path.parent, second.path.parent, third.path.parent},
            {first.path.parent},
        )

    def test_source_named_like_report_does_not_overwrite_report(self) -> None:
        source = self.temp_dir / "备份说明.txt"
        source.write_text("original", encoding="utf-8")
        result = backup_engine.run_backup_job(
            {
                "id": "report-name-job",
                "name": "文件备份",
                "job_type": "files",
                "file_source": str(source),
                "output_dir": str(self.temp_dir / "output"),
                "retention_days": 0,
            }
        )
        with zipfile.ZipFile(result.path) as archive:
            self.assertIn("备份说明.txt", archive.namelist())
            self.assertIn("备份说明.txt_2", archive.namelist())
            self.assertIn(
                "任务: 文件备份",
                archive.read("备份说明.txt").decode("utf-8"),
            )

    def test_sqlite_backup_keeps_original_database_name(self) -> None:
        database = self.temp_dir / "原始数据库.db"
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE sample(id INTEGER)")
            connection.commit()
        finally:
            connection.close()

        result = backup_engine.run_backup_job(
            {
                "id": "sqlite-job",
                "name": "SQLite 备份",
                "job_type": "sqlite",
                "sqlite_path": str(database),
                "output_dir": str(self.temp_dir / "output"),
                "retention_days": 0,
            }
        )
        with zipfile.ZipFile(result.path) as archive:
            self.assertIn("原始数据库.db", archive.namelist())
            self.assertIn("备份说明.txt", archive.namelist())

    def test_mysql_backup_inner_name_is_database_name(self) -> None:
        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, query, args=None):
                self.query = query

            def fetchone(self):
                if "SCHEMATA" in self.query:
                    return "utf8mb4", "utf8mb4_unicode_ci"
                if "VERSION()" in self.query:
                    return ("8.4.0",)
                return None

            def fetchall(self):
                return []

        class FakeConnection:
            def cursor(self, *_args, **_kwargs):
                return FakeCursor()

            def commit(self):
                return None

            def close(self):
                return None

        with patch.object(
            backup_engine,
            "_mysql_connect",
            return_value=FakeConnection(),
        ):
            result = backup_engine.run_backup_job(
                {
                    "id": "mysql-job",
                    "name": "MySQL 数据库备份",
                    "job_type": "mysql",
                    "host": "192.168.2.250",
                    "port": 3306,
                    "user": "backup",
                    "password": "secret",
                    "database": "package_master",
                    "output_dir": str(self.temp_dir / "output"),
                    "retention_days": 0,
                }
            )

        with zipfile.ZipFile(result.path) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"package_master.sql", "备份说明.txt"},
            )
            self.assertIn(
                "备份内容: package_master.sql",
                archive.read("备份说明.txt").decode("utf-8"),
            )

    def test_retention_only_removes_owner_job(self) -> None:
        output = self.temp_dir / "output"
        output.mkdir()
        first = output / "【20260101010101】 任务 - a.zip"
        second = output / "【20260101010101】 任务 - a_2.zip"
        for path, job_id in ((first, "job-a"), (second, "job-b")):
            with zipfile.ZipFile(path, "w") as archive:
                archive.comment = f"DBA_JOB_ID={job_id}".encode()
                archive.writestr("备份说明.txt", "test")
            old_time = time.time() - 5 * 86400
            os.utime(path, (old_time, old_time))

        removed = backup_engine.apply_retention(
            output,
            "任务",
            1,
            "job-a",
        )
        self.assertEqual(removed, 1)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_retention_scans_date_folders_and_removes_empty_folder(self) -> None:
        output = self.temp_dir / "output"
        dated_output = output / "20260101备份"
        dated_output.mkdir(parents=True)
        backup = dated_output / "【20260101010101】 任务 - a.zip"
        with zipfile.ZipFile(backup, "w") as archive:
            archive.comment = b"DBA_JOB_ID=job-a"
            archive.writestr("备份说明.txt", "test")
        old_time = time.time() - 5 * 86400
        os.utime(backup, (old_time, old_time))

        removed = backup_engine.apply_retention(
            output,
            "任务",
            1,
            "job-a",
        )
        self.assertEqual(removed, 1)
        self.assertFalse(backup.exists())
        self.assertFalse(dated_output.exists())

    def test_file_source_does_not_include_other_date_folders(self) -> None:
        source = self.temp_dir / "source"
        source.mkdir()
        (source / "data.txt").write_text("content", encoding="utf-8")
        output_root = source / "backups"
        old_folder = output_root / "20250101备份"
        old_folder.mkdir(parents=True)
        (old_folder / "old.zip").write_bytes(b"old")

        result = backup_engine.run_backup_job(
            {
                "id": "file-job",
                "name": "文件夹备份",
                "job_type": "files",
                "file_source": str(source),
                "file_skip_unreadable": True,
                "output_dir": str(output_root),
                "retention_days": 0,
            }
        )
        with zipfile.ZipFile(result.path) as archive:
            self.assertFalse(
                any(name.startswith("backups/") for name in archive.namelist())
            )


class NotificationTests(unittest.TestCase):
    def test_notification_settings_are_merged_and_typed(self) -> None:
        settings = notifications.notification_settings(
            {
                "notify_on_failure": False,
                "email": {
                    "smtp_host": "smtp.example.com",
                    "smtp_port": "2525",
                },
            }
        )
        self.assertFalse(settings["notify_on_failure"])
        self.assertEqual(
            settings["email"]["smtp_host"],
            "smtp.example.com",
        )
        self.assertEqual(settings["email"]["smtp_port"], 2525)

    def test_notification_channels_are_selected_per_backup_scope(self) -> None:
        settings = notifications.notification_settings(
            {
                "backup": {
                    "email_enabled": True,
                    "dingtalk_enabled": False,
                },
                "email": {
                    "smtp_host": "smtp.example.com",
                    "sender": "sender@example.com",
                    "recipients": "receiver@example.com",
                },
            }
        )
        self.assertTrue(
            notifications.channel_is_configured(settings, "email")
        )
        self.assertEqual(
            notifications.notification_channels(settings, "backup"),
            {"email"},
        )
        self.assertEqual(
            notifications.notification_channels(settings, "windows_backup"),
            set(),
        )

    def test_email_uses_configured_smtp_and_recipients(self) -> None:
        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, **_kwargs):
                sent["server"] = (host, port)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def ehlo(self):
                return None

            def login(self, username, password):
                sent["login"] = (username, password)

            def send_message(self, message):
                sent["message"] = message

        settings = notifications.notification_settings(
            {
                "email": {
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 465,
                    "security": "ssl",
                    "username": "sender@example.com",
                    "sender": "sender@example.com",
                    "recipients": "a@example.com; b@example.com",
                },
                "notify_on_success": True,
            }
        )
        settings["email"]["password"] = "secret"
        with patch.object(notifications.smtplib, "SMTP_SSL", FakeSMTP):
            notifications.send_email(settings, "备份成功", "测试内容")

        self.assertEqual(sent["server"], ("smtp.example.com", 465))
        self.assertEqual(sent["login"], ("sender@example.com", "secret"))
        self.assertEqual(
            sent["message"]["To"],
            "a@example.com, b@example.com",
        )

    def test_dingtalk_webhook_is_signed_and_sent(self) -> None:
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"errcode":0,"errmsg":"ok"}'

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        settings = notifications.notification_settings(
            {
                "dingtalk": {
                    "at_mobiles": "13800000000",
                }
            }
        )
        settings["dingtalk"]["webhook"] = (
            "https://oapi.dingtalk.com/robot/send?access_token=test-token"
        )
        settings["dingtalk"]["secret"] = "test-secret"
        with (
            patch.object(
                notifications.time,
                "time",
                return_value=1700000000,
            ),
            patch.object(notifications, "urlopen", fake_urlopen),
        ):
            notifications.send_dingtalk(settings, "备份报告", "全部成功")

        self.assertIn("timestamp=1700000000000", captured["url"])
        self.assertIn("sign=", captured["url"])
        self.assertEqual(captured["payload"]["msgtype"], "markdown")
        self.assertEqual(
            captured["payload"]["at"]["atMobiles"],
            ["13800000000"],
        )
        self.assertTrue(
            captured["payload"]["markdown"]["text"].endswith(
                "@13800000000"
            )
        )

    def test_system_backup_task_parser(self) -> None:
        output = json.dumps(
            [
                {
                    "task_path": "\\Microsoft\\Windows\\Backup\\",
                    "task_name": "WindowsBackup",
                    "state": "Ready",
                    "last_run_time": "2026-11-09T23:30:00+08:00",
                    "next_run_time": "2026-11-10T23:30:00+08:00",
                    "last_task_result": "0",
                }
            ],
            ensure_ascii=False,
        )
        with patch.object(
            notifications,
            "_run_powershell",
            return_value=output,
        ):
            tasks = notifications.list_system_backup_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_name"], "WindowsBackup")
        self.assertEqual(tasks[0]["last_task_result"], "0")

    def test_windows_server_backup_reads_configured_target(self) -> None:
        output = json.dumps(
            {
                "provider": "windows_server_backup",
                "task_path": "",
                "task_name": "Windows Server Backup（系统计划）",
                "state": "NotRun",
                "last_run_time": "",
                "next_run_time": "2026-11-09T23:30:00+08:00",
                "last_task_result": "",
                "backup_target": "WIN-BBP 2026_11_09 13:33 DISK_01",
            },
            ensure_ascii=False,
        )
        captured: dict[str, str] = {}

        def fake_run_powershell(script: str, timeout: int = 30) -> str:
            del timeout
            captured["script"] = script
            return output

        with patch.object(
            notifications,
            "_run_powershell",
            side_effect=fake_run_powershell,
        ):
            status = notifications.get_windows_server_backup_status()

        self.assertIsNotNone(status)
        self.assertEqual(
            status["backup_target"],
            "WIN-BBP 2026_11_09 13:33 DISK_01",
        )
        self.assertIn("Get-WBPolicy", captured["script"])
        self.assertIn("Get-WBBackupTarget", captured["script"])
        self.assertIn("Get-WBDisk", captured["script"])

    def test_system_backup_detection_avoids_duplicate_and_running_notice(
        self,
    ) -> None:
        previous = {
            "last_observed_run_time": "2026-11-09T23:30:00+08:00",
            "last_observed_result": "0",
            "last_observed_state": "Ready",
        }
        current = {
            "last_run_time": "2026-11-10T23:30:00+08:00",
            "last_task_result": "0",
            "state": "Ready",
        }
        self.assertTrue(
            notifications.is_new_system_backup_result(previous, current)
        )
        self.assertFalse(
            notifications.is_new_system_backup_result(
                {
                    **previous,
                    "last_observed_run_time": current["last_run_time"],
                },
                current,
            )
        )
        self.assertFalse(
            notifications.is_new_system_backup_result(
                previous,
                {**current, "state": "Running"},
            )
        )
        self.assertTrue(
            notifications.is_new_system_backup_result(
                {**previous, "last_observed_state": "Running"},
                {
                    "last_run_time": previous["last_observed_run_time"],
                    "last_task_result": "0",
                    "state": "Ready",
                },
            )
        )
        self.assertTrue(
            notifications.is_new_system_backup_result(
                {
                    "baseline_initialized": True,
                    "last_observed_run_time": "",
                    "last_observed_result": "",
                    "last_observed_state": "NotRun",
                },
                current,
            )
        )

    def test_app_queues_new_system_backup_result_once(self) -> None:
        app = BackupApp.__new__(BackupApp)
        app.events = queue.Queue()
        app._last_system_backup_error = ""
        app.config = {
            "notifications": {
                "windows_backup": {
                    "enabled": True,
                    "email_enabled": True,
                    "dingtalk_enabled": False,
                    "task_path": "\\Microsoft\\Windows\\Backup\\",
                    "task_name": "WindowsBackup",
                    "baseline_initialized": True,
                    "last_observed_run_time": "2026-11-09T23:30:00+08:00",
                    "last_observed_result": "0",
                    "last_observed_state": "Ready",
                },
            }
        }
        status = {
            "task_path": "\\Microsoft\\Windows\\Backup\\",
            "task_name": "WindowsBackup",
            "state": "Ready",
            "last_run_time": "2026-11-10T23:30:00+08:00",
            "next_run_time": "2026-11-11T23:30:00+08:00",
            "last_task_result": "0",
        }
        with patch.object(
            app_module,
            "get_system_backup_status",
            return_value=status,
        ):
            app.check_system_backup_status()
        kind, payload = app.events.get_nowait()
        self.assertEqual(kind, "system_backup_result")
        self.assertEqual(payload["last_task_result"], "0")

    def test_backup_notification_message_and_trigger_policy(self) -> None:
        result = backup_engine.BackupResult(
            Path("C:/Backup/example.zip"),
            2048,
            "example",
        )
        subject, body = backup_notification_message(
            [("job-1", result, None)],
            {"job-1": {"name": "测试备份"}},
            1.5,
            auto=True,
        )
        self.assertIn("备份成功", subject)
        self.assertRegex(
            subject,
            r"^【数据备份】备份成功 - \d{4}-\d{2}-\d{2} "
            r"\d{2}:\d{2}:\d{2}$",
        )
        self.assertIn("备份状态：备份成功", body)
        self.assertIn("测试备份", body)
        self.assertIn("- 测试备份：备份成功", body)
        self.assertNotIn("example.zip", body)
        self.assertNotIn("2.0 KB", body)
        self.assertEqual(
            format_notification_time(
                "2026-11-09T23:30:32.0000000+08:00"
            ),
            "2026-11-09 23:30:32",
        )
        self.assertEqual(
            notification_subject(
                "Windows备份",
                "备份成功",
                "2026-11-10 02:05:12",
            ),
            "【Windows备份】备份成功 - 2026-11-10 02:05:12",
        )
        settings = notifications.notification_settings(
            {"notify_on_success": True}
        )
        self.assertFalse(should_send_notification(settings, True))
        settings["backup"]["email_enabled"] = True
        self.assertTrue(should_send_notification(settings, True))


class ConfigurationAndSchedulingTests(unittest.TestCase):
    def test_portable_config_and_log_paths(self) -> None:
        self.assertEqual(CONFIG_PATH, PROJECT_ROOT / "config" / "config.json")
        self.assertEqual(LOG_DIR, PROJECT_ROOT / "config" / "logs")

    def test_config_store_writes_to_specified_portable_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dba_portable_config_") as temp:
            config_path = Path(temp) / "config" / "config.json"
            store = ConfigStore(config_path)
            store.data["backup_jobs"] = []
            store.save()
            self.assertTrue(config_path.is_file())

    def test_frozen_application_uses_executable_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dba_frozen_path_") as temp:
            executable = Path(temp) / "数据自动备份助手.exe"
            with (
                patch.object(config_store.sys, "frozen", True, create=True),
                patch.object(config_store.sys, "executable", str(executable)),
            ):
                self.assertEqual(config_store.application_dir(), Path(temp))

    def test_duplicate_job_names_are_rejected(self) -> None:
        app = BackupApp.__new__(BackupApp)
        job = {
            "name": "同名任务",
            "job_type": "mysql",
            "output_dir": "C:/Backups",
            "backup_time": "02:00",
            "retention_days": 30,
            "host": "127.0.0.1",
            "port": 3306,
            "user": "backup",
            "database": "sample",
        }
        self.assertFalse(
            app._validate(
                {"backup_jobs": [dict(job), dict(job)]},
                show_dialog=False,
            )
        )

    def test_job_name_conflict_check_ignores_the_job_being_edited(self) -> None:
        app = BackupApp.__new__(BackupApp)
        app.backup_jobs = [{"id": "job-1", "name": "已有任务"}]
        jobs = [{"name": "已有任务"}]
        self.assertIsNotNone(app._job_name_validation_error(jobs))
        self.assertIsNone(
            app._job_name_validation_error(jobs, existing_id="job-1")
        )

    def test_editing_job_does_not_change_checked_state(self) -> None:
        app = BackupApp.__new__(BackupApp)
        app.backup_jobs = [
            {
                "id": "job-1",
                "name": "待编辑任务",
                "job_type": "files",
                "file_source": "D:/Source",
                "output_dir": "E:/Backup",
                "backup_time": "23:30",
                "retention_days": 30,
            },
            {
                "id": "job-2",
                "name": "已勾选任务",
                "job_type": "files",
                "file_source": "D:/Other",
                "output_dir": "E:/Backup",
                "backup_time": "23:30",
                "retention_days": 30,
            },
        ]
        app.job_checked = {"job-2"}
        refreshed: list[str | None] = []
        app._refresh_job_tree = lambda selected_id=None: refreshed.append(
            selected_id
        )
        app._store_backup_jobs(
            [
                {
                    "name": "编辑后的任务",
                    "job_type": "files",
                    "file_source": "D:/Source",
                    "output_dir": "E:/Backup",
                    "backup_time": "23:30",
                    "retention_days": 30,
                }
            ],
            existing_id="job-1",
        )

        self.assertEqual(app.job_checked, {"job-2"})
        self.assertEqual(refreshed, ["job-1"])

    def test_adding_job_does_not_auto_check_it(self) -> None:
        app = BackupApp.__new__(BackupApp)
        app.backup_jobs = [
            {
                "id": "job-1",
                "name": "已有任务",
                "job_type": "files",
                "file_source": "D:/Source",
                "output_dir": "E:/Backup",
                "backup_time": "23:30",
                "retention_days": 30,
            }
        ]
        app.job_checked = {"job-1"}
        refreshed: list[str | None] = []
        app._refresh_job_tree = lambda selected_id=None: refreshed.append(
            selected_id
        )
        app._store_backup_jobs(
            [
                {
                    "name": "新增任务",
                    "job_type": "files",
                    "file_source": "D:/New",
                    "output_dir": "E:/Backup",
                    "backup_time": "23:30",
                    "retention_days": 30,
                }
            ]
        )

        self.assertEqual(app.job_checked, {"job-1"})
        self.assertNotIn("job-1", refreshed)
        self.assertEqual(len(refreshed), 1)
        self.assertIsNotNone(refreshed[0])

    def test_legacy_config_is_migrated_to_independent_jobs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dba_config_") as temp:
            config_path = Path(temp) / "config.json"
            config_path.write_text(
                """
                {
                  "output_dir": "C:/Backups",
                  "backup_time": "03:10",
                  "retention_days": 15,
                  "database_targets": [
                    {
                      "name": "生产",
                      "db_type": "mysql",
                      "host": "127.0.0.1",
                      "port": 3306,
                      "user": "backup",
                      "password_enc": "",
                      "databases": ["a", "b"]
                    }
                  ]
                }
                """,
                encoding="utf-8",
            )
            jobs = ConfigStore(config_path).load()["backup_jobs"]

        self.assertEqual(len(jobs), 2)
        self.assertEqual({job["database"] for job in jobs}, {"a", "b"})
        self.assertTrue(all(job["output_dir"] == "C:/Backups" for job in jobs))

    def test_new_job_after_todays_time_does_not_run_immediately(self) -> None:
        now = backup_engine.dt.datetime.now()
        app = BackupApp.__new__(BackupApp)
        app._started_at = now - backup_engine.dt.timedelta(days=2)
        app.backup_jobs = [
            {
                "id": "new",
                "backup_time": (now - backup_engine.dt.timedelta(minutes=1)).strftime(
                    "%H:%M"
                ),
                "last_attempt_date": "",
                "schedule_set_at": now.isoformat(timespec="seconds"),
            }
        ]
        self.assertEqual(app.get_due_job_ids(), [])

    def test_invalid_collection_types_do_not_crash_config_load(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dba_invalid_config_") as temp:
            config_path = Path(temp) / "config.json"
            config_path.write_text(
                '{"backup_jobs": "bad", "database_targets": 123}',
                encoding="utf-8",
            )
            data = ConfigStore(config_path).load()
        self.assertEqual(data["backup_jobs"], [])
        self.assertEqual(data["database_targets"], [])

    def test_invalid_config_root_does_not_crash_load(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dba_invalid_root_") as temp:
            config_path = Path(temp) / "config.json"
            config_path.write_text("[]", encoding="utf-8")
            data = ConfigStore(config_path).load()
        self.assertEqual(data["backup_jobs"], [])

    def test_existing_job_does_not_catch_up_after_startup(self) -> None:
        now = backup_engine.dt.datetime.now()
        app = BackupApp.__new__(BackupApp)
        app._started_at = now
        app.backup_jobs = [
            {
                "id": "existing",
                "backup_time": (now - backup_engine.dt.timedelta(minutes=1)).strftime(
                    "%H:%M"
                ),
                "last_attempt_date": "",
                "schedule_set_at": (
                    now - backup_engine.dt.timedelta(days=1)
                ).isoformat(timespec="seconds"),
            }
        ]
        self.assertEqual(app.get_due_job_ids(), [])

    def test_job_runs_when_program_was_started_before_schedule(self) -> None:
        now = backup_engine.dt.datetime.now()
        app = BackupApp.__new__(BackupApp)
        app._started_at = now - backup_engine.dt.timedelta(minutes=2)
        app.backup_jobs = [
            {
                "id": "running",
                "backup_time": now.strftime("%H:%M"),
                "last_attempt_date": "",
                "schedule_set_at": (
                    now - backup_engine.dt.timedelta(days=1)
                ).isoformat(timespec="seconds"),
            }
        ]
        self.assertEqual(app.get_due_job_ids(), ["running"])


if __name__ == "__main__":
    unittest.main()
