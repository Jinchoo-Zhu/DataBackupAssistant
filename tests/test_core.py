from __future__ import annotations

import os
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

import backup_engine
from app import BackupApp
from config_store import ConfigStore


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


class ConfigurationAndSchedulingTests(unittest.TestCase):
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

    def test_existing_job_can_catch_up_missed_today(self) -> None:
        now = backup_engine.dt.datetime.now()
        app = BackupApp.__new__(BackupApp)
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
        self.assertEqual(app.get_due_job_ids(), ["existing"])


if __name__ == "__main__":
    unittest.main()
