from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

APP_NAME = "DataBackupAssistant"
APP_DISPLAY_NAME = "数据自动备份助手"
APP_VERSION = "1.0.0"
APP_AUTHOR = "Jinchoo"
LEGACY_APP_NAME = "DatabaseBackupAssistant"


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME}"


def legacy_app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / LEGACY_APP_NAME
    return Path.home() / f".{LEGACY_APP_NAME}"


CONFIG_PATH = app_data_dir() / "config.json"
LOG_DIR = app_data_dir() / "logs"


DEFAULT_CONFIG: dict[str, Any] = {
    "backup_jobs": [],
    "database_backup_enabled": True,
    "file_backup_enabled": False,
    "database_targets": [],
    "db_type": "mysql",
    "mysql_host": "127.0.0.1",
    "mysql_port": 3306,
    "mysql_user": "root",
    "mysql_password_enc": "",
    "mysql_database": "",
    "sqlite_path": "",
    "file_backup_name": "文件备份",
    "file_sources": [],
    "file_skip_unreadable": True,
    "output_dir": str(Path.home() / "Documents" / "DataBackups"),
    "backup_time": "02:00",
    "retention_days": 30,
    "start_with_windows": False,
    "close_to_tray": True,
    "last_backup_date": "",
    "last_backup_status": "",
    "last_backup_file": "",
    "last_auto_attempt_date": "",
}


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _dpapi_transform(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        # Development fallback only. Windows builds use the current-user DPAPI key.
        return base64.b64encode(data) if protect else base64.b64decode(data)

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in, _buffer = _blob_from_bytes(data)
    blob_out = _DataBlob()
    description = ctypes.c_wchar_p(APP_NAME)
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    result = function(
        ctypes.byref(blob_in),
        description,
        None,
        None,
        None,
        flags,
        ctypes.byref(blob_out),
    )
    if not result:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect_password(password: str) -> str:
    if not password:
        return ""
    encrypted = _dpapi_transform(password.encode("utf-8"), protect=True)
    return base64.b64encode(encrypted).decode("ascii")


def unprotect_password(value: str) -> str:
    if not value:
        return ""
    try:
        encrypted = base64.b64decode(value.encode("ascii"))
        return _dpapi_transform(encrypted, protect=False).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


class ConfigStore:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self.data = dict(DEFAULT_CONFIG)

    def load(self) -> dict[str, Any]:
        loaded: dict[str, Any] = {}
        legacy_config = legacy_app_data_dir() / "config.json"
        if not self.path.exists() and legacy_config.is_file():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_config, self.path)
            except OSError:
                pass
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
        self.data = {**DEFAULT_CONFIG, **loaded}
        raw_targets = self.data.get("database_targets") or []
        self.data["database_targets"] = [
            dict(target)
            for target in raw_targets
            if isinstance(target, dict)
        ] if isinstance(raw_targets, list) else []
        raw_jobs = self.data.get("backup_jobs") or []
        self.data["backup_jobs"] = [
            dict(job) for job in raw_jobs if isinstance(job, dict)
        ] if isinstance(raw_jobs, list) else []
        if "database_targets" not in loaded:
            legacy_target = self._legacy_database_target()
            if legacy_target is not None:
                self.data["database_targets"] = [legacy_target]
        if "backup_jobs" not in loaded:
            self.data["backup_jobs"] = self._legacy_backup_jobs()
        seen_ids: set[str] = set()
        for job in self.data["backup_jobs"]:
            job_id = str(job.get("id", "")).strip()
            if not job_id or job_id in seen_ids:
                job_id = uuid.uuid4().hex
                job["id"] = job_id
            seen_ids.add(job_id)
        return self.data

    def _legacy_database_target(self) -> dict[str, Any] | None:
        db_type = str(self.data.get("db_type", "mysql")).lower()
        if db_type == "mysql" and str(self.data.get("mysql_database", "")).strip():
            host = str(self.data.get("mysql_host", "127.0.0.1")).strip()
            try:
                port = int(self.data.get("mysql_port", 3306))
            except (TypeError, ValueError):
                port = 3306
            return {
                "name": f"MySQL {host}:{port}",
                "db_type": "mysql",
                "host": host,
                "port": port,
                "user": str(self.data.get("mysql_user", "root")).strip(),
                "password_enc": str(self.data.get("mysql_password_enc", "")),
                "databases": [str(self.data["mysql_database"]).strip()],
            }
        sqlite_path = str(self.data.get("sqlite_path", "")).strip()
        if db_type == "sqlite" and sqlite_path:
            return {
                "name": f"SQLite {Path(sqlite_path).stem}",
                "db_type": "sqlite",
                "sqlite_paths": [sqlite_path],
            }
        return None

    def _legacy_backup_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        output_dir = str(self.data.get("output_dir", "")).strip()
        backup_time = str(self.data.get("backup_time", "02:00")).strip()
        retention_days = self.data.get("retention_days", 30)
        last_attempt_date = str(self.data.get("last_auto_attempt_date", ""))
        names: set[str] = set()

        def unique_name(base: str) -> str:
            candidate = base
            suffix = 2
            while candidate.casefold() in names:
                candidate = f"{base} ({suffix})"
                suffix += 1
            names.add(candidate.casefold())
            return candidate

        for target in self.data.get("database_targets", []):
            target_name = str(target.get("name", "")).strip() or "数据库"
            if target.get("db_type") == "sqlite":
                sqlite_paths = target.get("sqlite_paths", [])
                if isinstance(sqlite_paths, str):
                    sqlite_paths = [sqlite_paths]
                if not isinstance(sqlite_paths, list):
                    sqlite_paths = []
                for path in sqlite_paths:
                    jobs.append(
                        {
                            "id": uuid.uuid4().hex,
                            "name": unique_name(
                                f"{target_name} - {Path(path).stem}"
                            ),
                            "job_type": "sqlite",
                            "sqlite_path": str(path),
                            "output_dir": output_dir,
                            "backup_time": backup_time,
                            "retention_days": retention_days,
                            "last_attempt_date": last_attempt_date,
                            "last_backup_date": "",
                            "last_status": "",
                            "last_backup_file": "",
                        }
                    )
            else:
                databases = target.get("databases", [])
                if isinstance(databases, str):
                    databases = [databases]
                if not isinstance(databases, list):
                    databases = []
                for database in databases:
                    jobs.append(
                        {
                            "id": uuid.uuid4().hex,
                            "name": unique_name(
                                f"{target_name} - {database}"
                            ),
                            "job_type": "mysql",
                            "host": str(
                                target.get("host", "127.0.0.1")
                            ),
                            "port": _safe_port(target.get("port", 3306)),
                            "user": str(target.get("user", "root")),
                            "password_enc": str(
                                target.get("password_enc", "")
                            ),
                            "database": str(database),
                            "output_dir": output_dir,
                            "backup_time": backup_time,
                            "retention_days": retention_days,
                            "last_attempt_date": last_attempt_date,
                            "last_backup_date": "",
                            "last_status": "",
                            "last_backup_file": "",
                        }
                    )

        if bool(self.data.get("file_backup_enabled", False)):
            file_name = str(
                self.data.get("file_backup_name", "文件备份")
            ).strip()
            file_sources = self.data.get("file_sources", [])
            if isinstance(file_sources, str):
                file_sources = [file_sources]
            if not isinstance(file_sources, list):
                file_sources = []
            for source in file_sources:
                jobs.append(
                    {
                        "id": uuid.uuid4().hex,
                        "name": unique_name(
                            f"{file_name} - {Path(source).name or '文件'}"
                        ),
                        "job_type": "files",
                        "file_source": str(source),
                        "file_skip_unreadable": bool(
                            self.data.get("file_skip_unreadable", True)
                        ),
                        "output_dir": output_dir,
                        "backup_time": backup_time,
                        "retention_days": retention_days,
                        "last_attempt_date": last_attempt_date,
                        "last_backup_date": "",
                        "last_status": "",
                        "last_backup_file": "",
                    }
                )
        return jobs

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, self.path)

def backup_jobs_with_passwords(config: dict[str, Any]) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    for raw_job in config.get("backup_jobs", []):
        if not isinstance(raw_job, dict):
            continue
        job = dict(raw_job)
        job["password"] = unprotect_password(
            str(job.get("password_enc", ""))
        )
        hydrated.append(job)
    return hydrated


def _safe_port(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 3306

