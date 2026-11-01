from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
from ctypes import wintypes
from pathlib import Path
from typing import Any


APP_NAME = "DataBackupAssistant"
APP_DISPLAY_NAME = "数据自动备份助手"
APP_VERSION = "1.1.0"
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
    "db_type": "mysql",
    "mysql_host": "127.0.0.1",
    "mysql_port": 3306,
    "mysql_user": "root",
    "mysql_password_enc": "",
    "mysql_database": "",
    "sqlite_path": "",
    "output_dir": str(Path.home() / "Documents" / "DatabaseBackups"),
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
    except Exception:
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
        return self.data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, self.path)

    def get_password(self) -> str:
        return unprotect_password(str(self.data.get("mysql_password_enc", "")))

    def set_password(self, password: str) -> None:
        self.data["mysql_password_enc"] = protect_password(password)

