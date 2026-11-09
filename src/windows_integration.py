from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "DataBackupAssistant"
LEGACY_RUN_VALUE_NAME = "DatabaseBackupAssistant"
SPI_GETWORKAREA = 0x0030


def center_window_on_screen(window: Any) -> None:
    window.update_idletasks()
    geometry = window.geometry()
    try:
        size = geometry.split("+", 1)[0]
        width, height = (int(value) for value in size.split("x", 1))
    except (TypeError, ValueError):
        width = max(window.winfo_width(), window.winfo_reqwidth())
        height = max(window.winfo_height(), window.winfo_reqheight())

    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            window_handle = int(window.wm_frame(), 16)
            window_rect = wintypes.RECT()
            if user32.GetWindowRect(
                window_handle,
                ctypes.byref(window_rect),
            ):
                outer_width = window_rect.right - window_rect.left
                outer_height = window_rect.bottom - window_rect.top
                if outer_width > 0 and outer_height > 0:
                    width = outer_width
                    height = outer_height
            work_area = wintypes.RECT()
            if user32.SystemParametersInfoW(
                SPI_GETWORKAREA,
                0,
                ctypes.byref(work_area),
                0,
            ):
                area_width = work_area.right - work_area.left
                area_height = work_area.bottom - work_area.top
                x = work_area.left + (area_width - width) // 2
                y = work_area.top + (area_height - height) // 2
                if user32.SetWindowPos(
                    window_handle,
                    0,
                    x,
                    y,
                    0,
                    0,
                    0x0015,
                ):
                    return
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    x = (window.winfo_screenwidth() - width) // 2
    y = (window.winfo_screenheight() - height) // 2
    window.geometry(f"+{max(0, x)}+{max(0, y)}")


def center_window_over_parent(window: Any, parent: Any) -> None:
    window.update_idletasks()
    parent.update_idletasks()
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            window_handle = int(window.wm_frame(), 16)
            parent_handle = int(parent.winfo_toplevel().wm_frame(), 16)
            window_rect = wintypes.RECT()
            parent_rect = wintypes.RECT()
            if (
                user32.GetWindowRect(
                    window_handle,
                    ctypes.byref(window_rect),
                )
                and user32.GetWindowRect(
                    parent_handle,
                    ctypes.byref(parent_rect),
                )
            ):
                parent_width = parent_rect.right - parent_rect.left
                parent_height = parent_rect.bottom - parent_rect.top
                window_width = window_rect.right - window_rect.left
                window_height = window_rect.bottom - window_rect.top
                x = parent_rect.left + (parent_width - window_width) // 2
                y = parent_rect.top + (parent_height - window_height) // 2
                if user32.SetWindowPos(
                    window_handle,
                    0,
                    x,
                    y,
                    0,
                    0,
                    0x0015,
                ):
                    return
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    width = max(window.winfo_width(), window.winfo_reqwidth())
    height = max(window.winfo_height(), window.winfo_reqheight())
    x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
    window.geometry(f"+{max(0, x)}+{max(0, y)}")


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

