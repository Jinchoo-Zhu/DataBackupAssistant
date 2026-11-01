from __future__ import annotations

import os
import sys
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "DataBackupAssistant"
LEGACY_RUN_VALUE_NAME = "DatabaseBackupAssistant"


def executable_command() -> str:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
    else:
        executable = Path(sys.executable).with_name("pythonw.exe")
        if not executable.exists():
            executable = Path(sys.executable)
    command = f'"{executable}"'
    if not getattr(sys, "frozen", False):
        command += f' "{Path(sys.argv[0]).resolve()}"'
    return command + " --minimized"


def is_autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(
                key,
                RUN_VALUE_NAME,
                0,
                winreg.REG_SZ,
                executable_command(),
            )
        for value_name in (RUN_VALUE_NAME, LEGACY_RUN_VALUE_NAME):
            if enabled and value_name == RUN_VALUE_NAME:
                continue
            try:
                winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass

