from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import logging
import os
import queue
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import uuid
import zipfile
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import ttk

from backup_engine import (
    BackupResult,
    _format_size,
    apply_retention,
    run_backup_job,
)
from backup_job_dialog import BackupJobDialog
from centered_dialogs import askyesno, showerror, showinfo
from config_store import (
    APP_AUTHOR,
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_VERSION,
    LOG_DIR,
    ConfigStore,
    backup_jobs_with_passwords,
    protect_password,
    unprotect_password,
)
from notification_dialog import NotificationSettingsDialog
from notifications import (
    DeliveryResult,
    get_system_backup_status,
    is_new_system_backup_result,
    notification_channels,
    notification_settings,
    send_notifications,
)
from windows_integration import center_window_on_screen, set_autostart


class _CenteredMessageBox:
    showinfo = staticmethod(showinfo)
    showerror = staticmethod(showerror)
    askyesno = staticmethod(askyesno)


messagebox = _CenteredMessageBox()


APP_TITLE = APP_DISPLAY_NAME
BG = "#F7F8FA"
CARD = "#FFFFFF"
TEXT = "#1A1A1A"
MUTED = "#666666"
ACCENT = "#0078D4"
ACCENT_DARK = "#106EBE"
ACCENT_SOFT = "#E8F0FE"
SUCCESS = "#107C10"
DANGER = "#D13438"
BORDER = "#D0D0D0"


def resource_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent.parent / "assets"
    return base / name


def configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_DIR / "backup.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(handler)
    return logger


def runtime_notification_settings(config: dict) -> dict:
    settings = notification_settings(config.get("notifications"))
    settings["email"]["password"] = unprotect_password(
        str(settings["email"].get("password_enc", ""))
    )
    settings["dingtalk"]["webhook"] = unprotect_password(
        str(settings["dingtalk"].get("webhook_enc", ""))
    )
    settings["dingtalk"]["secret"] = unprotect_password(
        str(settings["dingtalk"].get("secret_enc", ""))
    )
    return settings


def should_send_notification(
    settings: dict,
    success: bool,
    scope: str = "backup",
) -> bool:
    key = "notify_on_success" if success else "notify_on_failure"
    return bool(notification_channels(settings, scope)) and bool(
        settings.get(key)
    )


def format_notification_time(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def notification_subject(
    name: str,
    outcome: str,
    timestamp: str,
) -> str:
    return f"【{name}】{outcome} - {timestamp}"


def backup_notification_message(
    records: list[tuple[str, object, str | None]],
    jobs_by_id: dict[str, dict],
    elapsed: float,
    *,
    auto: bool,
) -> tuple[str, str] | None:
    if not records:
        return None
    failure_count = sum(1 for _, _, error in records if error)
    success_count = len(records) - failure_count
    if success_count == len(records):
        outcome = "备份成功"
    elif success_count:
        outcome = "部分备份失败"
    else:
        outcome = "备份失败"
    now_text = format_notification_time(dt.datetime.now())
    host = socket.gethostname()
    subject = notification_subject("数据备份", outcome, now_text)
    lines = [
        f"- 主机：{host}",
        f"- 执行方式：{'自动备份' if auto else '手动备份'}",
        f"- 执行时间：{now_text}",
        f"- 备份状态：{outcome}",
        f"- 任务结果：成功 {success_count}，失败 {failure_count}",
        f"- 总耗时：{elapsed:.1f} 秒",
        "",
        "**任务明细**",
    ]
    for job_id, result, error in records:
        job = jobs_by_id.get(str(job_id), {})
        name = str(job.get("name", job_id or "备份任务"))
        if error:
            lines.append(f"- {name}：备份失败")
        elif isinstance(result, BackupResult):
            lines.append(f"- {name}：备份成功")
        else:
            lines.append(f"- {name}：完成")
    return subject, "\n".join(lines)


class ImageCheckbutton(tk.Frame):
    """固定尺寸的 Fluent 勾选框，切换状态时不会引起布局移动。"""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        text: str,
        variable: tk.BooleanVar,
        checked_image: tk.PhotoImage,
        unchecked_image: tk.PhotoImage,
        command: Callable[[], None] | None = None,
        background: str = CARD,
    ) -> None:
        super().__init__(parent, bg=background, cursor="hand2", takefocus=True)
        self.variable = variable
        self.command = command
        self._checked_image = checked_image
        self._unchecked_image = unchecked_image
        self._image_label = tk.Label(
            self,
            image=unchecked_image,
            bg=background,
            bd=0,
            padx=0,
            pady=0,
            cursor="hand2",
        )
        self._image_label.pack(side="left")
        self._text_label = tk.Label(
            self,
            text=text,
            bg=background,
            fg=TEXT,
            font=("Microsoft YaHei UI", 9),
            bd=0,
            padx=0,
            pady=0,
            cursor="hand2",
        )
        self._text_label.pack(side="left", padx=(8, 0))
        self._trace_id = variable.trace_add("write", self._draw)
        for widget in (self, self._image_label, self._text_label):
            widget.bind("<Button-1>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._draw()

    def _toggle(self, _event: tk.Event | None = None) -> str:
        self.variable.set(not self.variable.get())
        if self.command is not None:
            self.command()
        return "break"

    def _draw(self, *_args: object) -> None:
        self._image_label.configure(
            image=(
                self._checked_image
                if self.variable.get()
                else self._unchecked_image
            )
        )

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            try:
                self.variable.trace_remove("write", self._trace_id)
            except tk.TclError:
                pass




def acquire_single_instance():
    if os.name != "nt":
        return True, None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\DataBackupAssistant")
    already_exists = kernel32.GetLastError() == 183
    return not already_exists, handle


class BackupScheduler(threading.Thread):
    def __init__(self, app: BackupApp) -> None:
        super().__init__(name="backup-scheduler", daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self._next_system_backup_check = 0.0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        time.sleep(3)
        while not self.stop_event.is_set():
            try:
                if not self.app.backup_running:
                    due_ids = self.app.get_due_job_ids()
                    if due_ids:
                        self.app.events.put(("jobs_due", due_ids))
                now = time.monotonic()
                if now >= self._next_system_backup_check:
                    self._next_system_backup_check = now + 30
                    self.app.check_system_backup_status()
            except Exception:
                self.app.logger.exception("定时调度检查失败")
            self.stop_event.wait(15)


class BackupApp:
    def __init__(self, root: tk.Tk, minimized: bool = False) -> None:
        self.root = root
        self.root.withdraw()
        self.minimized = minimized
        self.store = ConfigStore()
        self.config = self.store.load()
        self.logger = configure_logging()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.backup_running = False
        self.tray_icon = None
        self._closing = False
        self._last_system_backup_error = ""
        self._started_at = dt.datetime.now()
        self._build_variables()
        self._load_checkbox_images()
        self._configure_window()
        self._build_ui()
        self._load_into_ui()
        self._start_tray()
        self.scheduler = BackupScheduler(self)
        self.scheduler.start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(150, self._poll_events)
        if minimized:
            self.root.withdraw()
        else:
            center_window_on_screen(self.root)
            self.root.deiconify()
        self.add_log("程序已启动，定时任务已就绪。")

    def _build_variables(self) -> None:
        self.backup_jobs: list[dict] = []
        self.job_checked: set[str] = set()
        self.start_with_windows = tk.BooleanVar()
        self.close_to_tray = tk.BooleanVar()
        self.status_text = tk.StringVar(value="准备就绪")
        self.connection_hint = tk.StringVar(value="尚未添加备份配置")

    def _load_checkbox_images(self) -> None:
        self.checkbox_checked_image: tk.PhotoImage | None = None
        self.checkbox_unchecked_image: tk.PhotoImage | None = None
        try:
            self.checkbox_checked_image = tk.PhotoImage(
                file=str(resource_path("checkbox-checked.png"))
            )
            self.checkbox_unchecked_image = tk.PhotoImage(
                file=str(resource_path("checkbox-unchecked.png"))
            )
        except tk.TclError:
            self.checkbox_checked_image = None
            self.checkbox_unchecked_image = None

    def _create_checkbutton(
        self,
        parent: tk.Misc,
        *,
        text: str,
        variable: tk.BooleanVar,
        command: Callable[[], None] | None = None,
        background: str = CARD,
    ) -> tk.Widget:
        if (
            self.checkbox_checked_image is not None
            and self.checkbox_unchecked_image is not None
        ):
            return ImageCheckbutton(
                parent,
                text=text,
                variable=variable,
                checked_image=self.checkbox_checked_image,
                unchecked_image=self.checkbox_unchecked_image,
                command=command,
                background=background,
            )
        options: dict[str, object] = {
            "text": text,
            "variable": variable,
            "bg": background,
            "fg": TEXT,
            "activebackground": background,
            "activeforeground": TEXT,
            "selectcolor": background,
            "font": ("Microsoft YaHei UI", 9),
            "bd": 0,
            "relief": "flat",
            "highlightthickness": 0,
            "anchor": "w",
            "cursor": "hand2",
        }
        if command is not None:
            options["command"] = command
        return tk.Checkbutton(parent, **options)

    def _configure_window(self) -> None:
        self.root.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.root.geometry("1080x900")
        self.root.minsize(980, 760)
        self.root.configure(bg=BG)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
        try:
            icon_path = resource_path("app.ico")
            if icon_path.exists():
                self.root.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # Remove ttk's dotted keyboard-focus ring from every button style.
        style.layout(
            "TButton",
            [
                (
                    "Button.border",
                    {
                        "sticky": "nswe",
                        "border": "1",
                        "children": [
                            (
                                "Button.padding",
                                {
                                    "sticky": "nswe",
                                    "children": [
                                        ("Button.label", {"sticky": "nswe"})
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure(
            "TLabel",
            background=CARD,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Muted.TLabel",
            background=CARD,
            foreground=MUTED,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Section.TLabel",
            background=CARD,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        style.configure(
            "TEntry",
            fieldbackground=CARD,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=7,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=CARD,
            background=CARD,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            arrowsize=24,
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", CARD)],
            foreground=[("readonly", TEXT)],
        )
        style.configure(
            "Primary.TButton",
            background=ACCENT,
            foreground="#FFFFFF",
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            borderwidth=1,
            padding=(16, 8),
            font=("Microsoft YaHei UI", 9, "bold"),
            relief="solid",
            focuscolor=ACCENT,
        )
        style.map(
            "Primary.TButton",
            background=[("active", ACCENT_DARK), ("disabled", "#8AB9DF")],
            bordercolor=[("active", ACCENT_DARK), ("disabled", "#8AB9DF")],
            lightcolor=[("active", ACCENT_DARK), ("disabled", "#8AB9DF")],
            darkcolor=[("active", ACCENT_DARK), ("disabled", "#8AB9DF")],
        )
        style.configure(
            "Secondary.TButton",
            background="#F7F7F7",
            foreground=TEXT,
            bordercolor="#B8B8B8",
            lightcolor="#B8B8B8",
            darkcolor="#B8B8B8",
            padding=(12, 7),
            relief="solid",
            borderwidth=1,
            focuscolor="#F7F7F7",
        )
        style.map(
            "Secondary.TButton",
            background=[
                ("pressed", ACCENT_SOFT),
                ("active", "#E8E8E8"),
                ("disabled", "#F5F5F5"),
            ],
            foreground=[
                ("pressed", ACCENT_DARK),
                ("active", TEXT),
                ("disabled", "#AAAAAA"),
            ],
            bordercolor=[
                ("pressed", ACCENT),
                ("active", ACCENT),
                ("disabled", BORDER),
            ],
            lightcolor=[
                ("pressed", ACCENT),
                ("active", ACCENT),
                ("disabled", BORDER),
            ],
            darkcolor=[
                ("pressed", ACCENT),
                ("active", ACCENT),
                ("disabled", BORDER),
            ],
        )
        style.configure(
            "TCheckbutton",
            background=CARD,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 9),
        )
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure(
            "Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=TEXT,
            rowheight=28,
            font=("Microsoft YaHei UI", 9),
            borderwidth=1,
            relief="solid",
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT_SOFT)],
            foreground=[("selected", TEXT)],
        )
        style.configure(
            "Treeview.Heading",
            background="#FAFAFA",
            foreground=MUTED,
            font=("Microsoft YaHei UI", 8, "bold"),
            padding=(5, 6),
            relief="solid",
            borderwidth=1,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#F0F0F0")],
            foreground=[("active", TEXT)],
            bordercolor=[("active", ACCENT)],
            lightcolor=[("active", ACCENT)],
            darkcolor=[("active", ACCENT)],
        )
        for orientation in ("Vertical", "Horizontal"):
            scrollbar_layout = (
                [
                    (
                        "Vertical.Scrollbar.trough",
                        {
                            "sticky": "ns",
                            "children": [
                                (
                                    "Vertical.Scrollbar.thumb",
                                    {"expand": "1", "sticky": "nswe"},
                                )
                            ],
                        },
                    )
                ]
                if orientation == "Vertical"
                else [
                    (
                        "Horizontal.Scrollbar.trough",
                        {
                            "sticky": "we",
                            "children": [
                                (
                                    "Horizontal.Scrollbar.thumb",
                                    {"expand": "1", "sticky": "nswe"},
                                )
                            ],
                        },
                    )
                ]
            )
            style.layout(f"{orientation}.TScrollbar", scrollbar_layout)
            style.configure(
                f"{orientation}.TScrollbar",
                width=10,
                background="#C8C8C8",
                troughcolor="#FAFAFA",
                bordercolor="#FAFAFA",
                lightcolor="#C8C8C8",
                darkcolor="#C8C8C8",
                arrowcolor=MUTED,
                gripcount=0,
                borderwidth=0,
            )
            style.map(
                f"{orientation}.TScrollbar",
                background=[
                    ("active", "#A8A8A8"),
                    ("pressed", "#8A8A8A"),
                ],
                lightcolor=[
                    ("active", "#A8A8A8"),
                    ("pressed", "#8A8A8A"),
                ],
                darkcolor=[
                    ("active", "#A8A8A8"),
                    ("pressed", "#8A8A8A"),
                ],
            )
        style.configure(
            "TNotebook",
            background=BG,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.layout(
            "TNotebook",
            [("Notebook.client", {"sticky": "nswe"})],
        )
        style.layout(
            "Clean.TNotebook",
            [("Notebook.client", {"sticky": "nswe"})],
        )
        style.configure(
            "TNotebook.Tab",
            background=CARD,
            foreground=MUTED,
            borderwidth=0,
            padding=(18, 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", CARD),
                ("active", ACCENT_SOFT),
            ],
            foreground=[("selected", ACCENT_DARK), ("active", TEXT)],
        )

    def _build_ui(self) -> None:
        tk.Frame(self.root, bg=ACCENT, height=2).pack(fill="x", side="top")
        header = tk.Frame(
            self.root,
            bg=CARD,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            highlightthickness=1,
        )
        header.pack(fill="x")
        header_inner = tk.Frame(header, bg=CARD)
        header_inner.pack(fill="x", padx=24, pady=11)

        try:
            self.header_logo_image = tk.PhotoImage(
                file=str(resource_path("app-48.png"))
            )
            tk.Label(
                header_inner,
                image=self.header_logo_image,
                bg=CARD,
                bd=0,
            ).pack(side="left", padx=(0, 12))
        except tk.TclError:
            self.header_logo_image = None

        title_box = tk.Frame(header_inner, bg=CARD)
        title_box.pack(side="left")
        tk.Label(
            title_box,
            text=APP_TITLE,
            bg=CARD,
            fg=TEXT,
            font=("Microsoft YaHei UI", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text=f"作者：{APP_AUTHOR}",
            bg=CARD,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(4, 0))
        tk.Label(
            title_box,
            text=f"版本 V{APP_VERSION}",
            bg=CARD,
            fg=ACCENT_DARK,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(1, 0))

        status_box = tk.Frame(header_inner, bg=ACCENT_SOFT, padx=13, pady=7)
        status_box.pack(side="right")
        self.status_dot = tk.Label(
            status_box,
            text="●",
            bg=ACCENT_SOFT,
            fg=SUCCESS,
            font=("Microsoft YaHei UI", 11),
        )
        self.status_dot.pack(side="left", padx=(0, 7))
        tk.Label(
            status_box,
            textvariable=self.status_text,
            bg=ACCENT_SOFT,
            fg=ACCENT_DARK,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(side="bottom", fill="x", padx=30, pady=(0, 16))
        self.last_result = tk.Label(
            footer,
            text="",
            bg=BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        ttk.Button(
            footer,
            text="备份勾选任务",
            style="Secondary.TButton",
            takefocus=False,
            command=self.start_selected_jobs,
        ).pack(side="right", padx=(10, 0))
        self.save_button = ttk.Button(
            footer,
            text="保存设置",
            style="Primary.TButton",
            command=self.save_settings,
        )
        self.save_button.pack(side="right", padx=(10, 0))
        self.last_result.pack(side="left", fill="x", expand=True)

        content = tk.Frame(self.root, bg=BG)
        content.pack(fill="both", expand=True, padx=28, pady=18)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)

        self.task_card = self._card(
            content,
            0,
            0,
            pady=(0, 14),
        )
        self._build_task_card()

        log_card = self._card(content, 1, 0, pady=0)
        self._build_log_card(log_card)

    def _card(self, parent, row, column, columnspan=1, padx=0, pady=0):
        outer = tk.Frame(
            parent,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        outer.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="nsew",
            padx=padx,
            pady=pady,
        )
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill="both", expand=True, padx=24, pady=17)
        return inner

    def _build_task_card(self) -> None:
        top = tk.Frame(self.task_card, bg=CARD)
        top.pack(fill="x")
        ttk.Label(
            top,
            text="备份任务",
            style="Section.TLabel",
        ).pack(side="left")
        ttk.Label(
            top,
            textvariable=self.connection_hint,
            style="Muted.TLabel",
        ).pack(side="right")

        ttk.Label(
            self.task_card,
            text="每个数据库、SQLite 文件、文件或文件夹都是独立任务，可分别设置保存根目录、执行时间和保留天数。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 10))

        tree_frame = tk.Frame(
            self.task_card,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        tree_frame.pack(fill="both", expand=True)
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        y_scroll = ttk.Scrollbar(tree_frame)
        x_scroll = ttk.Scrollbar(tree_frame, orient="horizontal")

        self.job_tree = ttk.Treeview(
            tree_frame,
            columns=("name", "type", "source", "output", "time", "keep", "status"),
            show=("tree", "headings"),
            height=8,
            selectmode="extended",
        )
        self.job_tree.heading(
            "#0",
            text="选择",
            anchor="center",
            command=self._toggle_all_jobs,
        )
        self.job_tree.column(
            "#0",
            width=58,
            minwidth=58,
            stretch=False,
            anchor="center",
        )
        headings = {
            "name": "任务名称",
            "type": "类型",
            "source": "备份来源",
            "output": "保存根目录",
            "time": "执行时间",
            "keep": "保留",
            "status": "上次结果",
        }
        widths = {
            "name": 195,
            "type": 68,
            "source": 220,
            "output": 225,
            "time": 68,
            "keep": 52,
            "status": 82,
        }
        for column, text in headings.items():
            self.job_tree.heading(
                column,
                text=text,
                anchor="w" if column == "name" else "center",
            )
            self.job_tree.column(
                column,
                width=widths[column],
                minwidth=widths[column],
                anchor=(
                    "w"
                    if column in {"name", "source", "output"}
                    else "center"
                ),
                stretch=False,
            )
        self.job_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.config(command=self.job_tree.yview)
        x_scroll.config(command=self.job_tree.xview)
        y_scroll.place(
            relx=1.0,
            rely=0.0,
            anchor="ne",
            relheight=1.0,
            width=11,
        )
        x_scroll.place(
            relx=0.0,
            rely=1.0,
            anchor="sw",
            relwidth=1.0,
            height=11,
        )
        y_scroll.lift()
        x_scroll.lift()
        self._bind_auto_scrollbars(
            self.job_tree,
            x_scroll,
            y_scroll,
        )
        self.job_tree.bind("<Button-1>", self._handle_job_tree_click, add="+")
        self.job_tree.bind("<Motion>", self._update_job_tree_cursor, add="+")
        self.job_tree.bind(
            "<Leave>",
            lambda _event: self.job_tree.configure(cursor=""),
            add="+",
        )
        self.job_tree.bind("<Double-1>", self._handle_job_tree_double_click)

        actions = tk.Frame(self.task_card, bg=CARD)
        actions.pack(fill="x", pady=(8, 8))
        for column in range(2):
            actions.grid_columnconfigure(column, weight=1)
        ttk.Button(
            actions,
            text="添加备份任务",
            style="Secondary.TButton",
            command=lambda: self.add_backup_job("mysql"),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(
            actions,
            text="删除选中任务",
            style="Secondary.TButton",
            command=self.remove_selected_jobs,
        ).grid(row=0, column=1, sticky="ew", padx=(5, 0))

        settings = tk.Frame(self.task_card, bg=CARD)
        settings.pack(fill="x")
        self._create_checkbutton(
            settings,
            text="登录 Windows 后自动启动",
            variable=self.start_with_windows,
        ).pack(side="left")
        self._create_checkbutton(
            settings,
            text="关闭窗口时隐藏到系统托盘",
            variable=self.close_to_tray,
        ).pack(side="left", padx=(24, 0))
        ttk.Button(
            settings,
            text="通知设置",
            style="Secondary.TButton",
            command=self.open_notification_settings,
        ).pack(side="right")

    def _update_job_hint(self) -> None:
        self.connection_hint.set(f"共 {len(self.backup_jobs)} 个独立备份任务")

    def _refresh_job_tree(self, selected_id: str | None = None) -> None:
        valid_ids = {
            str(job.get("id", ""))
            for job in self.backup_jobs
            if job.get("id")
        }
        self.job_checked.intersection_update(valid_ids)
        for item in self.job_tree.get_children():
            self.job_tree.delete(item)

        type_labels = {
            "mysql": "MySQL",
            "sqlite": "SQLite",
            "files": "文件",
        }
        for job in self.backup_jobs:
            job_id = str(job.get("id", ""))
            if not job_id:
                continue
            job_type = str(job.get("job_type", ""))
            if job_type == "mysql":
                source = (
                    f"{job.get('host', '')}:{job.get('port', 3306)}"
                    f" / {job.get('database', '')}"
                )
            elif job_type == "sqlite":
                source = str(job.get("sqlite_path", ""))
            else:
                source = str(job.get("file_source", ""))
            status = {
                "success": "成功",
                "partial": "部分失败",
                "error": "失败",
            }.get(str(job.get("last_status", "")), "未执行")
            self.job_tree.insert(
                "",
                "end",
                iid=job_id,
                image=self._job_checkbox_image(job_id in self.job_checked),
                text="",
                values=(
                    job.get("name", ""),
                    type_labels.get(job_type, job_type),
                    source,
                    job.get("output_dir", ""),
                    job.get("backup_time", "02:00"),
                    f"{job.get('retention_days', 30)} 天",
                    status,
                ),
            )
        if selected_id and self.job_tree.exists(selected_id):
            self.job_tree.focus(selected_id)
            self.job_tree.see(selected_id)
        self._update_job_hint()

    @staticmethod
    def _bind_auto_scrollbars(
        widget: ttk.Treeview,
        x_scroll: ttk.Scrollbar,
        y_scroll: ttk.Scrollbar,
    ) -> None:
        x_visible = True
        y_visible = True
        update_pending = False

        def set_visibility(
            scrollbar: ttk.Scrollbar,
            visible: bool,
            current: bool,
            show: Callable[[], None],
            hide: Callable[[], None],
        ) -> bool:
            if visible == current:
                return current
            if visible:
                show()
                scrollbar.lift()
            else:
                hide()
            return visible

        def sync_x(first: str, last: str) -> None:
            nonlocal x_visible
            x_scroll.set(first, last)
            x_visible = set_visibility(
                x_scroll,
                not (float(first) <= 0.0 and float(last) >= 1.0),
                x_visible,
                lambda: x_scroll.place(
                    relx=0.0,
                    rely=1.0,
                    anchor="sw",
                    relwidth=1.0,
                    height=11,
                ),
                x_scroll.place_forget,
            )

        def sync_y(first: str, last: str) -> None:
            nonlocal y_visible
            y_scroll.set(first, last)
            y_visible = set_visibility(
                y_scroll,
                not (
                    not widget.get_children()
                    or (float(first) <= 0.0 and float(last) >= 1.0)
                ),
                y_visible,
                lambda: y_scroll.place(
                    relx=1.0,
                    rely=0.0,
                    anchor="ne",
                    relheight=1.0,
                    width=11,
                ),
                y_scroll.place_forget,
            )

        widget.configure(
            xscrollcommand=sync_x,
            yscrollcommand=sync_y,
        )

        def refresh_initial() -> None:
            nonlocal update_pending
            update_pending = False
            sync_x(*map(str, widget.xview()))
            sync_y(*map(str, widget.yview()))

        def schedule_refresh() -> None:
            nonlocal update_pending
            if update_pending:
                return
            update_pending = True
            widget.after_idle(refresh_initial)

        widget.bind(
            "<Configure>",
            lambda _event: schedule_refresh(),
            add="+",
        )
        schedule_refresh()

    def _job_checkbox_image(self, checked: bool) -> tk.PhotoImage | None:
        return (
            self.checkbox_checked_image
            if checked
            else self.checkbox_unchecked_image
        )

    def _set_job_checkbox(self, iid: str, checked: bool) -> None:
        image = self._job_checkbox_image(checked)
        if image is not None:
            self.job_tree.item(iid, image=image, text="")
        else:
            self.job_tree.item(iid, text="✓" if checked else "□")

    def _handle_job_tree_click(self, event: tk.Event) -> str | None:
        column = self.job_tree.identify_column(event.x)
        region = self.job_tree.identify_region(event.x, event.y)
        if self._is_job_checkbox_separator(event):
            return "break"
        if column != "#0" or region not in {"tree", "cell"}:
            return None
        iid = self.job_tree.identify_row(event.y)
        if not iid:
            return None
        if iid in self.job_checked:
            self.job_checked.remove(iid)
            checked = False
        else:
            self.job_checked.add(iid)
            checked = True
        self._set_job_checkbox(iid, checked)
        return "break"

    def _update_job_tree_cursor(self, event: tk.Event) -> None:
        locked = self._is_job_checkbox_separator(event)
        self.job_tree.configure(cursor="no" if locked else "")

    def _is_job_checkbox_separator(self, event: tk.Event) -> bool:
        if self.job_tree.identify_region(event.x, event.y) != "separator":
            return False
        checkbox_width = int(self.job_tree.column("#0", "width"))
        return abs(event.x - checkbox_width) <= 3

    def _handle_job_tree_double_click(self, event: tk.Event) -> str | None:
        if self.job_tree.identify_column(event.x) == "#0":
            return "break"
        iid = self.job_tree.identify_row(event.y)
        if iid:
            self.edit_backup_job(iid)
        return "break"

    def _toggle_all_jobs(self) -> None:
        all_ids = {
            str(job.get("id", ""))
            for job in self.backup_jobs
            if job.get("id")
        }
        if self.job_checked == all_ids:
            self.job_checked.clear()
        else:
            self.job_checked = set(all_ids)
        for job_id in all_ids:
            self._set_job_checkbox(job_id, job_id in self.job_checked)

    def add_backup_job(self, job_type: str) -> None:
        dialog = BackupJobDialog(
            self.root,
            {"job_type": job_type},
            submit_validator=self._job_name_validation_error,
        )
        self.root.wait_window(dialog)
        if dialog.results:
            self._store_backup_jobs(dialog.results)

    def edit_backup_job(self, job_id: str) -> None:
        job = next(
            (item for item in self.backup_jobs if item.get("id") == job_id),
            None,
        )
        if job is None:
            return
        editable = dict(job)
        editable["password"] = unprotect_password(
            str(editable.get("password_enc", ""))
        )
        dialog = BackupJobDialog(
            self.root,
            editable,
            submit_validator=lambda jobs: self._job_name_validation_error(
                jobs,
                job_id,
            ),
        )
        self.root.wait_window(dialog)
        if dialog.results:
            self._store_backup_jobs(dialog.results, job_id)

    def _job_name_validation_error(
        self,
        jobs: list[dict],
        existing_id: str | None = None,
    ) -> str | None:
        proposed_names = [str(job.get("name", "")).strip() for job in jobs]
        if len({name.casefold() for name in proposed_names}) != len(
            proposed_names
        ):
            return "本次添加的备份任务存在重复名称。"

        existing_names = {
            str(job.get("name", "")).casefold()
            for job in self.backup_jobs
            if not existing_id or job.get("id") != existing_id
        }
        conflict = next(
            (
                name
                for name in proposed_names
                if name.casefold() in existing_names
            ),
            None,
        )
        if conflict:
            return f"备份任务名称“{conflict}”已存在，请换一个名称。"
        return None

    def _store_backup_jobs(
        self,
        jobs: list[dict],
        existing_id: str | None = None,
    ) -> None:
        validation_error = self._job_name_validation_error(jobs, existing_id)
        if validation_error:
            messagebox.showerror(
                "名称重复",
                validation_error,
                parent=self.root,
            )
            return

        if existing_id:
            index = next(
                (
                    index
                    for index, item in enumerate(self.backup_jobs)
                    if item.get("id") == existing_id
                ),
                None,
            )
            if index is None:
                return
            del self.backup_jobs[index]

        new_ids: list[str] = []
        for index, raw_job in enumerate(jobs):
            job = dict(raw_job)
            job.pop("id", None)
            if "password" in job:
                job["password_enc"] = protect_password(
                    str(job.pop("password"))
                )
            job_type = str(job.get("job_type", ""))
            if job_type == "mysql":
                for key in ("sqlite_path", "file_source", "file_skip_unreadable"):
                    job.pop(key, None)
            elif job_type == "sqlite":
                for key in (
                    "host",
                    "port",
                    "user",
                    "password_enc",
                    "database",
                    "file_source",
                    "file_skip_unreadable",
                ):
                    job.pop(key, None)
            else:
                for key in (
                    "host",
                    "port",
                    "user",
                    "password_enc",
                    "database",
                    "sqlite_path",
                ):
                    job.pop(key, None)
            job["last_attempt_date"] = ""
            job["last_backup_date"] = ""
            job["last_status"] = ""
            job["last_backup_file"] = ""
            job["schedule_set_at"] = dt.datetime.now().isoformat(
                timespec="seconds"
            )
            job["id"] = (
                existing_id
                if existing_id and index == 0
                else uuid.uuid4().hex
            )
            self.backup_jobs.append(job)
            new_ids.append(job["id"])
        self._refresh_job_tree(new_ids[0] if new_ids else None)

    def remove_selected_jobs(self) -> None:
        if not self.job_checked:
            messagebox.showinfo(
                "未勾选",
                "请先勾选需要删除的备份任务。",
                parent=self.root,
            )
            return
        selected_jobs = [
            job for job in self.backup_jobs if job.get("id") in self.job_checked
        ]
        if not messagebox.askyesno(
            "删除备份任务",
            f"确定删除勾选的 {len(selected_jobs)} 个备份任务吗？\n\n"
            + "\n".join(str(job.get("name", "")) for job in selected_jobs),
            parent=self.root,
        ):
            return
        self.backup_jobs = [
            job for job in self.backup_jobs if job.get("id") not in self.job_checked
        ]
        self.job_checked.clear()
        self._refresh_job_tree()

    def get_due_job_ids(self) -> list[str]:
        now = dt.datetime.now()
        today = now.date().isoformat()
        due: list[str] = []
        for job in self.backup_jobs:
            try:
                scheduled = dt.datetime.strptime(
                    str(job.get("backup_time", "02:00")),
                    "%H:%M",
                ).time()
            except ValueError:
                continue
            if now.time() < scheduled:
                continue
            if str(job.get("last_attempt_date", "")) == today:
                continue
            scheduled_today = dt.datetime.combine(now.date(), scheduled)
            if scheduled_today < self._started_at:
                continue

            schedule_set_at = str(job.get("schedule_set_at", "")).strip()
            if schedule_set_at:
                try:
                    set_at = dt.datetime.fromisoformat(schedule_set_at)
                except ValueError:
                    set_at = None
                if set_at is not None and scheduled_today <= set_at:
                    continue
            due.append(str(job.get("id", "")))
        return [job_id for job_id in due if job_id]

    def start_selected_jobs(self) -> None:
        if not self.job_checked:
            messagebox.showinfo(
                "未勾选",
                "请先勾选需要立即备份的任务。",
                parent=self.root,
            )
            return
        self.start_jobs_by_id(list(self.job_checked), auto=False)

    def start_all_jobs(self) -> None:
        job_ids = [
            str(job.get("id", ""))
            for job in self.backup_jobs
            if job.get("id")
        ]
        if not job_ids:
            messagebox.showinfo(
                "暂无任务",
                "请先添加备份任务。",
                parent=self.root,
            )
            return
        self.start_jobs_by_id(job_ids, auto=False)

    def start_jobs_by_id(self, job_ids: list[str], auto: bool = False) -> None:
        if self.backup_running:
            return
        selected = [
            dict(job)
            for job in self.backup_jobs
            if job.get("id") in set(job_ids)
        ]
        if not selected:
            return

        if auto:
            today = dt.date.today().isoformat()
            selected_ids = {str(job.get("id", "")) for job in selected}
            for job in self.backup_jobs:
                if job.get("id") in selected_ids:
                    job["last_attempt_date"] = today
            self.config.update(self._collect_config())
            self.store.data = self.config
            try:
                self.store.save()
            except OSError as exc:
                self.logger.warning("保存自动任务执行日期失败：%s", exc)

        runtime_jobs: list[dict] = []
        for job in selected:
            runtime = dict(job)
            runtime["password"] = unprotect_password(
                str(runtime.get("password_enc", ""))
            )
            runtime_jobs.append(runtime)

        self.backup_running = True
        self.status_text.set("自动备份中…" if auto else "正在备份任务…")
        self.status_dot.config(fg="#C19C00")
        self.add_log(
            ("自动任务：" if auto else "手动任务：")
            + f"开始执行 {len(runtime_jobs)} 个备份任务。"
        )

        def worker() -> None:
            started = time.monotonic()
            records: list[tuple[str, object, str | None]] = []
            for job in runtime_jobs:
                job_id = str(job.get("id", ""))
                job_name = str(job.get("name", "备份任务"))
                self.events.put(("progress", f"开始任务：{job_name}"))
                try:
                    result = run_backup_job(
                        job,
                        progress=lambda message: self.events.put(
                            ("progress", message)
                        ),
                    )
                    records.append((job_id, result, None))
                except Exception as exc:  # noqa: BLE001
                    self.logger.error(
                        "任务失败 %s：%s\n%s",
                        job_name,
                        exc,
                        traceback.format_exc(),
                    )
                    records.append((job_id, None, str(exc)))
            self.events.put(
                ("job_batch_done", (records, time.monotonic() - started, auto))
            )

        threading.Thread(target=worker, daemon=True).start()




    def _build_log_card(self, parent) -> None:
        top = tk.Frame(parent, bg=CARD)
        top.pack(fill="x")
        ttk.Label(top, text="运行记录", style="Section.TLabel").pack(side="left")
        ttk.Button(
            top,
            text="打开日志目录",
            style="Secondary.TButton",
            command=lambda: self.open_folder(LOG_DIR),
        ).pack(side="right")
        ttk.Button(
            top,
            text="清空显示",
            style="Secondary.TButton",
            command=self.clear_log,
        ).pack(side="right", padx=(0, 8))

        text_frame = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        text_frame.pack(fill="both", expand=True, pady=(14, 0))
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)
        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text = tk.Text(
            text_frame,
            height=7,
            bg="#FAFAFA",
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=ACCENT_SOFT,
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=10,
            font=("Consolas", 9),
            state="disabled",
            yscrollcommand=scrollbar.set,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.config(command=self.log_text.yview)
        self._bind_text_auto_scrollbar(self.log_text, scrollbar)
        self.log_text.tag_configure("success", foreground=SUCCESS)
        self.log_text.tag_configure("error", foreground=DANGER)
        self.log_text.tag_configure("muted", foreground=MUTED)

    @staticmethod
    def _bind_text_auto_scrollbar(
        text: tk.Text,
        scrollbar: ttk.Scrollbar,
    ) -> None:
        visible = True
        pending = False

        def sync(first: str, last: str) -> None:
            nonlocal visible
            scrollbar.set(first, last)
            needed = not (
                not text.get("1.0", "end-1c").strip()
                or (float(first) <= 0.0 and float(last) >= 1.0)
            )
            if needed == visible:
                return
            if needed:
                scrollbar.grid()
            else:
                scrollbar.grid_remove()
            visible = needed

        text.configure(yscrollcommand=sync)

        def refresh() -> None:
            nonlocal pending
            pending = False
            sync(*map(str, text.yview()))

        def schedule() -> None:
            nonlocal pending
            if pending:
                return
            pending = True
            text.after_idle(refresh)

        text.bind("<Configure>", lambda _event: schedule(), add="+")
        schedule()

    def _load_into_ui(self) -> None:
        config = self.config
        raw_jobs = config.get("backup_jobs", [])
        self.backup_jobs = [
            dict(job) for job in raw_jobs if isinstance(job, dict)
        ]
        self.job_checked.clear()
        self._refresh_job_tree()
        self.start_with_windows.set(bool(config.get("start_with_windows", False)))
        self.close_to_tray.set(bool(config.get("close_to_tray", True)))
        try:
            set_autostart(self.start_with_windows.get())
        except OSError as exc:
            self.logger.warning("更新开机启动项失败：%s", exc)

    def _collect_config(self) -> dict:
        config = dict(self.config)
        config.update(
            {
                "backup_jobs": [dict(job) for job in self.backup_jobs],
                "start_with_windows": self.start_with_windows.get(),
                "close_to_tray": self.close_to_tray.get(),
            }
        )
        return config

    def _validate(self, config: dict, show_dialog: bool = True) -> bool:
        try:
            jobs = config["backup_jobs"]
            if not jobs:
                raise ValueError("请至少添加一个备份任务。")
            names: set[str] = set()
            for job in jobs:
                name = str(job.get("name", "")).strip()
                if not name:
                    raise ValueError("任务名称不能为空。")
                if name.casefold() in names:
                    raise ValueError(f"任务名称重复：{name}")
                names.add(name.casefold())
                if not str(job.get("output_dir", "")).strip():
                    raise ValueError(f"{name} 未设置保存根目录。")
                try:
                    dt.datetime.strptime(str(job.get("backup_time", "")), "%H:%M")
                except ValueError as exc:
                    raise ValueError(f"{name} 的执行时间格式不正确。") from exc
                if not 0 <= int(job.get("retention_days", 30)) <= 3650:
                    raise ValueError(f"{name} 的保留天数应在 0 到 3650 之间。")
                job_type = str(job.get("job_type", ""))
                if job_type == "mysql":
                    if not str(job.get("host", "")).strip():
                        raise ValueError(f"{name} 未填写服务器地址。")
                    if not str(job.get("user", "")).strip():
                        raise ValueError(f"{name} 未填写用户名。")
                    if not 1 <= int(job.get("port", 0)) <= 65535:
                        raise ValueError(f"{name} 的端口无效。")
                    if not str(job.get("database", "")).strip():
                        raise ValueError(f"{name} 未选择数据库。")
                elif job_type == "sqlite":
                    if not Path(str(job.get("sqlite_path", ""))).is_file():
                        raise ValueError(f"{name} 的 SQLite 文件不存在。")
                elif job_type == "files":
                    if not Path(str(job.get("file_source", ""))).exists():
                        raise ValueError(f"{name} 的文件或文件夹不存在。")
                else:
                    raise ValueError(f"{name} 的任务类型无效。")
            return True
        except (ValueError, TypeError) as exc:
            if show_dialog:
                messagebox.showerror("设置有误", str(exc), parent=self.root)
            return False

    def save_settings(self, silent: bool = False) -> bool:
        config = self._collect_config()
        if not self._validate(config, show_dialog=not silent):
            return False
        self.config.update(config)
        self.store.data = self.config
        try:
            self.store.save()
            for job in self.config.get("backup_jobs", []):
                Path(job["output_dir"]).mkdir(parents=True, exist_ok=True)
            set_autostart(bool(self.config["start_with_windows"]))
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法保存设置：{exc}", parent=self.root)
            return False
        self._refresh_job_tree()
        self.add_log("设置已保存。")
        self.logger.info("设置已保存")
        if not silent:
            messagebox.showinfo("已保存", "备份设置已保存并立即生效。", parent=self.root)
        return True

    def open_notification_settings(self) -> None:
        dialog = NotificationSettingsDialog(
            self.root,
            notification_settings(self.config.get("notifications")),
        )
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        self.config["notifications"] = dialog.result
        self.store.data = self.config
        try:
            self.store.save()
        except OSError as exc:
            messagebox.showerror(
                "保存失败",
                f"无法保存通知设置：{exc}",
                parent=self.root,
            )
            return
        self.add_log("通知设置已保存。")

    def _send_notification_async(
        self,
        subject: str,
        body: str,
        channels: set[str],
    ) -> None:
        settings = runtime_notification_settings(self.config)
        if not channels:
            return

        def worker() -> None:
            results = send_notifications(
                settings,
                subject,
                body,
                channels=channels,
            )
            self.events.put(("notification_done", results))

        threading.Thread(target=worker, daemon=True).start()

    def _notify_backup_batch(
        self,
        records: list[tuple[str, object, str | None]],
        elapsed: float,
        auto: bool,
    ) -> None:
        settings = runtime_notification_settings(self.config)
        failure_count = sum(1 for _, _, error in records if error)
        success = not failure_count
        channels = notification_channels(settings, "backup")
        if not should_send_notification(settings, success, "backup"):
            return
        jobs_by_id = {
            str(job.get("id", "")): job for job in self.backup_jobs
        }
        message = backup_notification_message(
            records,
            jobs_by_id,
            elapsed,
            auto=auto,
        )
        if message is not None:
            self._send_notification_async(*message, channels)

    def check_system_backup_status(self) -> None:
        settings = notification_settings(self.config.get("notifications"))
        binding = settings["windows_backup"]
        channels = notification_channels(settings, "windows_backup")
        if not binding.get("enabled") or not channels:
            self._last_system_backup_error = ""
            return
        task_path = str(binding.get("task_path", "")).strip()
        task_name = str(binding.get("task_name", "")).strip()
        provider = str(binding.get("provider", "scheduled_task"))
        if not task_name or (provider != "windows_server_backup" and not task_path):
            self._last_system_backup_error = ""
            return
        try:
            status = get_system_backup_status(binding)
        except Exception as exc:  # noqa: BLE001
            message = f"读取系统备份任务失败：{exc}"
            if message != self._last_system_backup_error:
                self._last_system_backup_error = message
                self.events.put(("notification_error", message))
            return
        self._last_system_backup_error = ""

        last_run = str(status.get("last_run_time", "")).strip()
        baseline_initialized = bool(
            binding.get("baseline_initialized")
        )
        if not baseline_initialized:
            self.events.put(("system_backup_baseline", status))
            return
        if not last_run:
            return

        if is_new_system_backup_result(binding, status):
            self.events.put(("system_backup_result", status))

    def _update_system_backup_baseline(self, status: object) -> None:
        if not isinstance(status, dict):
            return
        settings = notification_settings(self.config.get("notifications"))
        binding = settings["windows_backup"]
        binding["last_observed_run_time"] = str(
            status.get("last_run_time", "")
        )
        binding["last_observed_result"] = str(
            status.get("last_task_result", "")
        )
        binding["last_observed_state"] = str(status.get("state", ""))
        binding["baseline_initialized"] = True
        self.config["notifications"] = settings
        self.store.data = self.config
        try:
            self.store.save()
        except OSError as exc:
            self.logger.warning("保存系统备份通知状态失败：%s", exc)

    def _notify_system_backup(self, status: object) -> None:
        if not isinstance(status, dict):
            return
        settings = runtime_notification_settings(self.config)
        result_code = str(status.get("last_task_result", "")).strip()
        success = result_code == "0"
        channels = notification_channels(settings, "windows_backup")
        if not should_send_notification(settings, success, "windows_backup"):
            return
        host = socket.gethostname()
        outcome = "备份成功" if success else "备份失败"
        task_name = str(status.get("task_name", "Windows 系统备份"))
        task_path = str(status.get("task_path", ""))
        now_text = format_notification_time(dt.datetime.now())
        last_run = (
            format_notification_time(status.get("last_run_time", ""))
            or "未知"
        )
        next_run = (
            format_notification_time(status.get("next_run_time", ""))
            or "未知"
        )
        subject = notification_subject("Windows备份", outcome, now_text)
        body_lines = [
            f"- 主机：{host}",
            f"- 备份任务：{task_name}",
            f"- 任务路径：{task_path}",
            f"- 执行结果：{outcome}",
            f"- 最近运行：{last_run}",
            f"- 下次运行：{next_run}",
            f"- 退出代码：{result_code or '未知'}",
        ]
        backup_target = str(status.get("backup_target", "")).strip()
        if backup_target:
            body_lines.append(f"- 备份目标：{backup_target}")
        body = "\n".join(body_lines)
        self._send_notification_async(subject, body, channels)


























    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self.add_log(str(payload), "muted")
                elif kind == "job_batch_done":
                    records, elapsed, auto = payload
                    self._finish_job_batch(records, float(elapsed), bool(auto))
                elif kind == "jobs_due":
                    self.start_jobs_by_id(list(payload), auto=True)
                elif kind == "system_backup_baseline":
                    self._update_system_backup_baseline(payload)
                elif kind == "system_backup_result":
                    self._update_system_backup_baseline(payload)
                    self._notify_system_backup(payload)
                elif kind == "notification_done":
                    for item in payload:
                        if isinstance(item, DeliveryResult):
                            self.add_log(
                                f"通知发送（{item.channel}）：{item.message}",
                                "muted" if item.success else "error",
                            )
                            if not item.success:
                                self.logger.warning(
                                    "通知发送失败（%s）：%s",
                                    item.channel,
                                    item.message,
                                )
                elif kind == "notification_error":
                    self.add_log(str(payload), "error")
                elif kind == "show_window":
                    self.show_window()
                elif kind == "start_all":
                    self.start_all_jobs()
                elif kind == "exit":
                    self.exit_app()
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(150, self._poll_events)

    def _finish_job_batch(
        self,
        records: list[tuple[str, object, str | None]],
        elapsed: float,
        auto: bool,
    ) -> None:
        self.backup_running = False
        success_count = 0
        error_messages: list[str] = []
        now_text = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        jobs_by_id = {
            str(job.get("id", "")): job for job in self.backup_jobs
        }
        for job_id, result, error in records:
            job = jobs_by_id.get(str(job_id))
            if job is None:
                continue
            if error:
                job["last_status"] = "error"
                job["last_backup_date"] = now_text
                error_messages.append(
                    f"{job.get('name', '备份任务')}：{error}"
                )
                self.add_log(
                    f"任务失败：{job.get('name', '')}：{error}",
                    "error",
                )
            elif isinstance(result, BackupResult):
                success_count += 1
                job["last_status"] = "success"
                job["last_backup_date"] = now_text
                job["last_backup_file"] = str(result.path)
                self.add_log(
                    f"任务成功：{job.get('name', '')}"
                    f" → {result.path.name}"
                    f"（{_format_size(result.bytes_written)}）",
                    "success",
                )

        if success_count == len(records):
            self.status_text.set(f"备份成功（{success_count} 项）")
            self.status_dot.config(fg=SUCCESS)
        elif success_count:
            self.status_text.set(
                f"部分成功（成功 {success_count}，失败 {len(records) - success_count}）"
            )
            self.status_dot.config(fg="#C19C00")
        else:
            self.status_text.set("备份失败")
            self.status_dot.config(fg=DANGER)

        self.add_log(f"本批任务用时 {elapsed:.1f} 秒。", "muted")
        self.last_result.config(
            text=f"最近执行：{now_text}  成功 {success_count}，失败 {len(records) - success_count}",
            fg=SUCCESS if success_count == len(records) else DANGER,
        )
        self.config.update(self._collect_config())
        self.store.data = self.config
        try:
            self.store.save()
        except OSError as exc:
            self.logger.error("保存任务执行结果失败：%s", exc)
        self._notify_backup_batch(records, elapsed, auto)
        self._refresh_job_tree()
        if error_messages and not auto:
            messagebox.showerror(
                "部分备份失败",
                "\n".join(error_messages),
                parent=self.root,
            )



    def add_log(self, message: str, tag: str = "") -> None:
        def append():
            self.log_text.config(state="normal")
            stamp = dt.datetime.now().strftime("%H:%M:%S")
            self.log_text.insert("end", f"[{stamp}] {message}\n", tag)
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > 500:
                self.log_text.delete("1.0", "100.0")
            self.log_text.see("end")
            self.log_text.config(state="disabled")

        if threading.current_thread() is threading.main_thread():
            append()
        else:
            self.root.after(0, append)

    def clear_log(self) -> None:
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def open_folder(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("无法打开", str(exc), parent=self.root)

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image

            image_path = resource_path("app.png")
            if image_path.exists():
                image = Image.open(image_path)
            else:
                image = Image.new("RGB", (64, 64), "#176BFF")
            menu = pystray.Menu(
                pystray.MenuItem(
                    "打开主界面",
                    lambda _icon, _item: self.events.put(("show_window", "")),
                    default=True,
                ),
                pystray.MenuItem(
                    "立即备份",
                    lambda _icon, _item: self.events.put(("start_all", "")),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "退出程序",
                    lambda _icon, _item: self.events.put(("exit", "")),
                ),
            )
            self.tray_icon = pystray.Icon(APP_NAME, image, APP_TITLE, menu)
            threading.Thread(
                target=self.tray_icon.run,
                name="tray-icon",
                daemon=True,
            ).start()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("系统托盘不可用：%s", exc)
            self.tray_icon = None

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def on_close(self) -> None:
        if self.close_to_tray.get() and self.tray_icon is not None:
            self.root.withdraw()
            self.add_log("程序已隐藏到系统托盘，定时备份继续运行。")
            return
        if messagebox.askyesno(
            "退出程序",
            "退出后定时备份将停止。确定退出吗？",
            parent=self.root,
        ):
            self.exit_app()

    def exit_app(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.scheduler.stop()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("停止系统托盘失败：%s", exc)
        self.root.quit()
        self.root.destroy()


def self_test() -> int:
    temp_context = tempfile.TemporaryDirectory(prefix="dbbap_test_")
    temp_dir = Path(temp_context.name)
    db_path = temp_dir / "sample.sqlite"
    db_path_2 = temp_dir / "sample_2.sqlite"
    source = sqlite3.connect(db_path)
    try:
        source.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)")
        source.executemany(
            "INSERT INTO users(name) VALUES(?)",
            [("张三",), ("Alice",)],
        )
        source.commit()
    finally:
        source.close()

    source_2 = sqlite3.connect(db_path_2)
    try:
        source_2.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
        source_2.execute("INSERT INTO items(value) VALUES('第二个数据库')")
        source_2.commit()
    finally:
        source_2.close()

    file_source = temp_dir / "file_source"
    nested = file_source / "nested"
    empty_dir = file_source / "empty"
    nested.mkdir(parents=True)
    empty_dir.mkdir()
    (file_source / "说明.txt").write_text("文件备份自检", encoding="utf-8")
    (nested / "data.json").write_text('{"ok": true}', encoding="utf-8")

    jobs = [
        {
            "id": "db1",
            "name": "自检库一",
            "job_type": "sqlite",
            "sqlite_path": str(db_path),
            "output_dir": str(temp_dir / "backups" / "db1"),
            "backup_time": "02:00",
            "retention_days": 0,
        },
        {
            "id": "db2",
            "name": "自检库二",
            "job_type": "sqlite",
            "sqlite_path": str(db_path_2),
            "output_dir": str(temp_dir / "backups" / "db2"),
            "backup_time": "02:10",
            "retention_days": 0,
        },
        {
            "id": "files1",
            "name": "自检文件",
            "job_type": "files",
            "file_source": str(file_source),
            "file_skip_unreadable": True,
            "output_dir": str(temp_dir / "backups" / "files"),
            "backup_time": "02:20",
            "retention_days": 0,
        },
    ]
    results = [run_backup_job(job) for job in jobs]
    if len(results) != 3:
        raise RuntimeError("独立任务自检未生成三个备份结果")
    if any(result.path.suffix != ".zip" for result in results):
        raise RuntimeError("数据库和文件备份未全部生成 ZIP")

    extract_root = temp_dir / "extract"
    extract_root.mkdir()
    with zipfile.ZipFile(results[0].path) as archive:
        archive.extractall(extract_root / "db1")
    restored_db = next((extract_root / "db1").glob("*.sqlite"))
    backup = sqlite3.connect(restored_db)
    try:
        count = backup.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        backup.close()
    if count != 2:
        raise RuntimeError("SQLite 自检数据不一致")
    with zipfile.ZipFile(results[1].path) as archive:
        archive.extractall(extract_root / "db2")
    restored_db_2 = next((extract_root / "db2").glob("*.sqlite"))
    backup_2 = sqlite3.connect(restored_db_2)
    try:
        second_count = backup_2.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        backup_2.close()
    if second_count != 1:
        raise RuntimeError("第二个 SQLite 自检数据不一致")
    with zipfile.ZipFile(results[2].path) as archive:
        names = set(archive.namelist())
        required = {
            "file_source/说明.txt",
            "file_source/nested/data.json",
            "file_source/empty/",
        }
        if not required.issubset(names):
            raise RuntimeError("文件备份自检内容缺失")

    protected = protect_password("self-test-password")
    if unprotect_password(protected) != "self-test-password":
        raise RuntimeError("Windows 密码加密自检失败")

    old_backup = temp_dir / "sample_20200101_000000.sql"
    other_backup = temp_dir / "sample_extra_20200101_000000.sql"
    old_backup.write_text("old", encoding="utf-8")
    other_backup.write_text("other", encoding="utf-8")
    old_time = time.time() - 90 * 86400
    os.utime(old_backup, (old_time, old_time))
    os.utime(other_backup, (old_time, old_time))
    apply_retention(temp_dir, "sample", 30)
    if old_backup.exists() or not other_backup.exists():
        raise RuntimeError("备份保留策略自检失败")
    return 0


def backup_once() -> int:
    store = ConfigStore()
    config = store.load()
    logger = configure_logging()
    jobs = backup_jobs_with_passwords(config)
    if not jobs:
        logger.error("命令行备份失败：没有备份任务")
        return 1
    failed = 0
    started = time.monotonic()
    records: list[tuple[str, object, str | None]] = []
    jobs_by_id = {
        str(job.get("id", "")): job for job in jobs
    }
    for job in jobs:
        job_id = str(job.get("id", ""))
        try:
            result = run_backup_job(
                job,
                progress=lambda message: logger.info(message),
            )
            logger.info("命令行备份成功：%s", result.path)
            for saved_job in config.get("backup_jobs", []):
                if saved_job.get("id") == job_id:
                    saved_job["last_status"] = "success"
                    saved_job["last_backup_file"] = str(result.path)
                    saved_job["last_backup_date"] = dt.datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    break
            records.append((job_id, result, None))
        except Exception:  # noqa: BLE001
            failed += 1
            error = traceback.format_exc()
            logger.error(
                "命令行任务失败：%s\n%s",
                job.get("name", ""),
                error,
            )
            records.append((job_id, None, "命令行备份失败，详情见日志。"))
    store.data = config
    store.save()
    settings = runtime_notification_settings(config)
    success = failed == 0
    channels = notification_channels(settings, "backup")
    if should_send_notification(settings, success, "backup"):
        message = backup_notification_message(
            records,
            jobs_by_id,
            time.monotonic() - started,
            auto=False,
        )
        if message is not None:
            for delivery in send_notifications(
                settings,
                *message,
                channels=channels,
            ):
                (logger.info if delivery.success else logger.warning)(
                    "通知发送（%s）：%s",
                    delivery.channel,
                    delivery.message,
                )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--backup-once", action="store_true")
    args, _unknown = parser.parse_known_args()

    if args.self_test:
        try:
            return self_test()
        except Exception:  # noqa: BLE001
            configure_logging().error("自检失败：%s", traceback.format_exc())
            return 1
    if args.backup_once:
        return backup_once()

    acquired, _mutex_handle = acquire_single_instance()
    if not acquired:
        ctypes.windll.user32.MessageBoxW(
            None,
            "数据自动备份助手已经在运行，请从系统托盘打开。",
            APP_TITLE,
            0x40,
        )
        return 0

    try:
        root = tk.Tk()
        BackupApp(root, minimized=args.minimized)
        root.mainloop()
    except OSError as exc:
        message = (
            "无法创建或读取程序目录下的 config 文件夹。\n"
            f"{exc}\n\n"
            "请将程序放到有写入权限的目录后重试。"
        )
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)
        else:
            print(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

