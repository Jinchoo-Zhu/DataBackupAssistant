from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import logging
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog
import tkinter as tk
from tkinter import ttk
from typing import Callable

from backup_engine import (
    BackupResult,
    _format_size,
    apply_retention,
    run_backup_job,
    run_backup_tasks,
)
from config_store import (
    APP_NAME,
    APP_AUTHOR,
    APP_DISPLAY_NAME,
    APP_VERSION,
    LOG_DIR,
    ConfigStore,
    backup_jobs_with_passwords,
    protect_password,
    unprotect_password,
)
from backup_job_dialog import BackupJobDialog
from centered_dialogs import askyesno, showerror, showinfo
from windows_integration import set_autostart


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
        self._background = background
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


class PageTab(tk.Frame):
    """AnhuSimMate 风格的固定页签，选中时显示浅蓝背景和左侧强调条。"""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        text: str,
        icon: str,
        command: Callable[[], None],
    ) -> None:
        super().__init__(parent, bg="#FAFAFA", width=156, height=42, cursor="hand2")
        self.command = command
        self._active = False
        self._hovered = False
        self.pack_propagate(False)
        self.indicator = tk.Frame(self, bg="#FAFAFA", width=3)
        self.indicator.pack(side="left", fill="y")
        self.icon_label = tk.Label(
            self,
            text=icon,
            bg="#FAFAFA",
            fg=MUTED,
            width=2,
            anchor="center",
            font=("Segoe UI Symbol", 11),
            cursor="hand2",
        )
        self.icon_label.pack(side="left", padx=(10, 7), fill="y")
        self.text_label = tk.Label(
            self,
            text=text,
            bg="#FAFAFA",
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        )
        self.text_label.pack(side="left", fill="both", expand=True)
        for widget in (self, self.icon_label, self.text_label):
            widget.bind("<Button-1>", self._invoke)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)
        self.bind("<space>", self._invoke)
        self.bind("<Return>", self._invoke)

    def _invoke(self, _event: tk.Event | None = None) -> str:
        self.command()
        return "break"

    def _enter(self, _event: tk.Event | None = None) -> None:
        self._hovered = True
        self._draw()

    def _leave(self, _event: tk.Event | None = None) -> None:
        self._hovered = False
        self._draw()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._draw()

    def _draw(self) -> None:
        background = ACCENT_SOFT if self._active else (
            "#E8E8E8" if self._hovered else "#FAFAFA"
        )
        foreground = ACCENT_DARK if self._active else MUTED
        for widget in (self, self.icon_label, self.text_label):
            widget.configure(bg=background)
        self.icon_label.configure(fg=foreground)
        self.text_label.configure(
            fg=foreground,
            font=(
                "Microsoft YaHei UI",
                9,
                "bold" if self._active else "normal",
            ),
        )
        self.indicator.configure(bg=ACCENT if self._active else background)


def acquire_single_instance():
    if os.name != "nt":
        return True, None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\DataBackupAssistant")
    already_exists = kernel32.GetLastError() == 183
    return not already_exists, handle


class BackupScheduler(threading.Thread):
    def __init__(self, app: "BackupApp") -> None:
        super().__init__(name="backup-scheduler", daemon=True)
        self.app = app
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        time.sleep(3)
        while not self.stop_event.is_set():
            try:
                if not self.app.backup_running:
                    due_ids = self.app.get_due_job_ids()
                    if due_ids:
                        self.app.root.after(
                            0,
                            lambda ids=due_ids: self.app.start_jobs_by_id(
                                ids,
                                auto=True,
                            ),
                        )
            except Exception:
                self.app.logger.exception("定时调度检查失败")
            self.stop_event.wait(15)


class BackupApp:
    def __init__(self, root: tk.Tk, minimized: bool = False) -> None:
        self.root = root
        self.minimized = minimized
        self.store = ConfigStore()
        self.config = self.store.load()
        self.logger = configure_logging()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.backup_running = False
        self.tray_icon = None
        self._closing = False
        self._next_run_after_id = None
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
        self.add_log("程序已启动，定时任务已就绪。")

    def _build_variables(self) -> None:
        self.backup_jobs: list[dict] = []
        self.job_checked: set[str] = set()
        self.file_backup_name = tk.StringVar(value="文件备份")
        self.file_skip_unreadable = tk.BooleanVar(value=True)
        self.output_dir = tk.StringVar()
        self.backup_time = tk.StringVar()
        self.retention_days = tk.StringVar()
        self.start_with_windows = tk.BooleanVar()
        self.close_to_tray = tk.BooleanVar()
        self.status_text = tk.StringVar(value="准备就绪")
        self.next_run_text = tk.StringVar()
        self.connection_hint = tk.StringVar(value="尚未添加备份配置")

    def _load_checkbox_images(self) -> None:
        self.checkbox_checked_image: tk.PhotoImage | None = None
        self.checkbox_unchecked_image: tk.PhotoImage | None = None
        self.checkbox_checked_cell_image: tk.PhotoImage | None = None
        self.checkbox_unchecked_cell_image: tk.PhotoImage | None = None
        try:
            self.checkbox_checked_image = tk.PhotoImage(
                file=str(resource_path("checkbox-checked.png"))
            )
            self.checkbox_unchecked_image = tk.PhotoImage(
                file=str(resource_path("checkbox-unchecked.png"))
            )
            self.checkbox_checked_cell_image = self._tree_checkbox_image(
                self.checkbox_checked_image
            )
            self.checkbox_unchecked_cell_image = self._tree_checkbox_image(
                self.checkbox_unchecked_image
            )
        except tk.TclError:
            self.checkbox_checked_image = None
            self.checkbox_unchecked_image = None
            self.checkbox_checked_cell_image = None
            self.checkbox_unchecked_cell_image = None

    @staticmethod
    def _tree_checkbox_image(image: tk.PhotoImage) -> tk.PhotoImage:
        padded = tk.PhotoImage(width=image.width() + 10, height=image.height())
        padded.tk.call(padded.name, "copy", image.name, "-to", 10, 0)
        return padded

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
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", CARD)],
            foreground=[("readonly", TEXT)],
            bordercolor=[("focus", ACCENT), ("active", ACCENT)],
            lightcolor=[("focus", ACCENT), ("active", ACCENT)],
            darkcolor=[("focus", ACCENT), ("active", ACCENT)],
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
        style.configure(
            "TNotebook",
            background=BG,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background="#FAFAFA",
            foreground=MUTED,
            padding=(18, 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD), ("active", ACCENT_SOFT)],
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
            text="每个数据库、SQLite 文件、文件或文件夹都是独立任务，可分别设置保存目录、执行时间和保留天数。",
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
            "output": "保存目录",
            "time": "执行时间",
            "keep": "保留",
            "status": "上次结果",
        }
        widths = {
            "name": 130,
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
        for column in range(3):
            actions.grid_columnconfigure(column, weight=1)
        ttk.Button(
            actions,
            text="添加 MySQL",
            style="Secondary.TButton",
            command=lambda: self.add_backup_job("mysql"),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 5))
        ttk.Button(
            actions,
            text="添加 SQLite",
            style="Secondary.TButton",
            command=lambda: self.add_backup_job("sqlite"),
        ).grid(row=0, column=1, sticky="ew", padx=4, pady=(0, 5))
        ttk.Button(
            actions,
            text="添加文件/文件夹",
            style="Secondary.TButton",
            command=lambda: self.add_backup_job("files"),
        ).grid(row=0, column=2, sticky="ew", padx=(4, 0), pady=(0, 5))
        ttk.Button(
            actions,
            text="删除勾选任务",
            style="Secondary.TButton",
            command=self.remove_selected_jobs,
        ).grid(row=1, column=0, columnspan=3, sticky="ew")

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

    def _update_job_hint(self) -> None:
        self.connection_hint.set(f"共 {len(self.backup_jobs)} 个独立备份任务")

    def _refresh_job_tree(self, selected_id: str | None = None) -> None:
        valid_ids = {
            str(job.get("id", ""))
            for job in self.backup_jobs
            if job.get("id")
        }
        self.job_checked.intersection_update(valid_ids)
        if selected_id in valid_ids:
            self.job_checked = {selected_id}
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
        dialog = BackupJobDialog(self.root, editable)
        self.root.wait_window(dialog)
        if dialog.results:
            self._store_backup_jobs(dialog.results, job_id)

    def _store_backup_jobs(
        self,
        jobs: list[dict],
        existing_id: str | None = None,
    ) -> None:
        existing_names = {
            str(job.get("name", "")).casefold()
            for job in self.backup_jobs
            if not existing_id or job.get("id") != existing_id
        }
        proposed_names = [str(job.get("name", "")).strip() for job in jobs]
        if len({name.casefold() for name in proposed_names}) != len(
            proposed_names
        ):
            messagebox.showerror(
                "名称重复",
                "本次添加的备份任务存在重复名称。",
                parent=self.root,
            )
            return
        conflict = next(
            (
                name
                for name in proposed_names
                if name.casefold() in existing_names
            ),
            None,
        )
        if conflict:
            messagebox.showerror(
                "名称重复",
                f"备份任务名称“{conflict}”已存在，请换一个名称。",
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
        self.job_checked = set(new_ids)
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

            schedule_set_at = str(job.get("schedule_set_at", "")).strip()
            if schedule_set_at:
                try:
                    set_at = dt.datetime.fromisoformat(schedule_set_at)
                except ValueError:
                    set_at = None
                scheduled_today = dt.datetime.combine(now.date(), scheduled)
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
                except Exception as exc:
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

    def _build_connection_card(self) -> None:
        top = tk.Frame(self.connection_card, bg=CARD)
        top.pack(fill="x")
        ttk.Label(top, text="备份内容", style="Section.TLabel").pack(side="left")
        ttk.Label(
            top,
            textvariable=self.connection_hint,
            style="Muted.TLabel",
        ).pack(side="right")

        toggles = tk.Frame(self.connection_card, bg=CARD)
        toggles.pack(fill="x", pady=(12, 10))
        self._create_checkbutton(
            toggles,
            text="数据库备份",
            variable=self.database_backup_enabled,
            command=self._update_source_hint,
        ).pack(side="left", padx=(0, 22))
        self._create_checkbutton(
            toggles,
            text="文件与文件夹备份",
            variable=self.file_backup_enabled,
            command=self._update_source_hint,
        ).pack(side="left")

        self.source_tab_bar = tk.Frame(self.connection_card, bg="#FAFAFA")
        self.source_tab_bar.pack(anchor="w")
        self.database_tab_button = PageTab(
            self.source_tab_bar,
            text="数据库",
            icon="▤",
            command=lambda: self._select_source_page("database"),
        )
        self.database_tab_button.pack(side="left")
        self.file_tab_button = PageTab(
            self.source_tab_bar,
            text="文件与文件夹",
            icon="▦",
            command=lambda: self._select_source_page("files"),
        )
        self.file_tab_button.pack(side="left")
        self.source_tab_buttons = {
            "database": self.database_tab_button,
            "files": self.file_tab_button,
        }
        tk.Frame(self.connection_card, bg=BORDER, height=1, width=312).pack(
            anchor="w"
        )

        self.source_page_container = tk.Frame(self.connection_card, bg=CARD)
        self.source_page_container.pack(fill="both", expand=True)
        self.source_page_container.grid_rowconfigure(0, weight=1)
        self.source_page_container.grid_columnconfigure(0, weight=1)
        self.database_tab = tk.Frame(self.source_page_container, bg=CARD)
        self.file_tab = tk.Frame(self.source_page_container, bg=CARD)
        self.database_tab.grid(row=0, column=0, sticky="nsew")
        self.file_tab.grid(row=0, column=0, sticky="nsew")
        self.database_tab_button.set_active(True)

        database_page = tk.Frame(self.database_tab, bg=CARD)
        database_page.pack(fill="both", expand=True, padx=10, pady=10)
        ttk.Label(
            database_page,
            text="数据库连接配置",
            style="Section.TLabel",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            database_page,
            text="每组配置可使用不同主机、端口和账号，并可同时选择多个数据库。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 8))

        tree_frame = tk.Frame(
            database_page,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        tree_frame.pack(fill="both", expand=True)
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_scroll = ttk.Scrollbar(tree_frame)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.database_tree = ttk.Treeview(
            tree_frame,
            columns=("name", "type", "endpoint", "databases"),
            show=("tree", "headings"),
            height=6,
            selectmode="extended",
        )
        self.database_tree.heading(
            "#0",
            text="选择",
            anchor="center",
            command=self._toggle_all_database_checks,
        )
        self.database_tree.column(
            "#0",
            width=58,
            minwidth=58,
            stretch=False,
            anchor="center",
        )
        self.database_tree.heading("name", text="配置名称")
        self.database_tree.heading("type", text="类型")
        self.database_tree.heading("endpoint", text="连接")
        self.database_tree.heading("databases", text="数据库")
        self.database_tree.column("name", width=88, minwidth=78, anchor="w")
        self.database_tree.column("type", width=54, minwidth=50, anchor="center")
        self.database_tree.column("endpoint", width=112, minwidth=92, anchor="w")
        self.database_tree.column("databases", width=84, minwidth=70, anchor="w")
        self.database_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.config(command=self.database_tree.yview)
        self.database_tree.configure(yscrollcommand=tree_scroll.set)
        self.database_tree.bind("<Button-1>", self._handle_database_tree_click, add="+")
        self.database_tree.bind("<Motion>", self._update_database_tree_cursor, add="+")
        self.database_tree.bind(
            "<Leave>",
            lambda _event: self.database_tree.configure(cursor=""),
            add="+",
        )
        self.database_tree.bind("<Double-1>", self._handle_database_tree_double_click)

        database_actions = tk.Frame(database_page, bg=CARD)
        database_actions.pack(fill="x", pady=(8, 0))
        database_actions.grid_columnconfigure(0, weight=1)
        database_actions.grid_columnconfigure(1, weight=1)
        ttk.Button(
            database_actions,
            text="添加 MySQL",
            style="Secondary.TButton",
            command=lambda: self.add_database_target("mysql"),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 5))
        ttk.Button(
            database_actions,
            text="添加 SQLite",
            style="Secondary.TButton",
            command=lambda: self.add_database_target("sqlite"),
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 5))
        ttk.Button(
            database_actions,
            text="删除勾选的数据库配置",
            style="Secondary.TButton",
            command=self.remove_selected_database_targets,
        ).grid(row=1, column=0, columnspan=2, sticky="ew")

        file_form = tk.Frame(self.file_tab, bg=CARD)
        file_form.pack(fill="both", expand=True, padx=10, pady=12)
        file_form.grid_columnconfigure(1, weight=1)

        self._label(file_form, "备份名称", 0)
        ttk.Entry(file_form, textvariable=self.file_backup_name).grid(
            row=0,
            column=1,
            sticky="ew",
            pady=(0, 9),
        )

        self._label(file_form, "备份来源", 1)
        source_frame = tk.Frame(
            file_form,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        source_frame.grid(row=1, column=1, sticky="nsew", pady=(0, 8))
        source_frame.grid_columnconfigure(0, weight=1)
        source_scroll = ttk.Scrollbar(source_frame)
        source_scroll.grid(row=0, column=1, sticky="ns")
        source_x_scroll = ttk.Scrollbar(
            source_frame,
            orient="horizontal",
        )
        source_x_scroll.grid(row=1, column=0, sticky="ew")
        self.file_listbox = tk.Listbox(
            source_frame,
            height=5,
            selectmode=tk.EXTENDED,
            activestyle="none",
            bg=CARD,
            fg=TEXT,
            selectbackground="#DCE9FF",
            selectforeground=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
            yscrollcommand=source_scroll.set,
            xscrollcommand=source_x_scroll.set,
        )
        self.file_listbox.grid(row=0, column=0, sticky="nsew", padx=4, pady=3)
        source_scroll.config(command=self.file_listbox.yview)
        source_x_scroll.config(command=self.file_listbox.xview)

        source_actions = tk.Frame(file_form, bg=CARD)
        source_actions.grid(row=2, column=1, sticky="ew", pady=(0, 8))
        source_actions.grid_columnconfigure(0, weight=1)
        source_actions.grid_columnconfigure(1, weight=1)
        ttk.Button(
            source_actions,
            text="添加文件夹",
            style="Secondary.TButton",
            command=self.add_file_folders,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 5))
        ttk.Button(
            source_actions,
            text="添加文件",
            style="Secondary.TButton",
            command=self.add_files,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 5))
        ttk.Button(
            source_actions,
            text="移除选中",
            style="Secondary.TButton",
            command=self.remove_selected_files,
        ).grid(row=1, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(
            source_actions,
            text="清空",
            style="Secondary.TButton",
            command=self.clear_file_sources,
        ).grid(row=1, column=1, sticky="ew", padx=(4, 0))

        self._create_checkbutton(
            file_form,
            text="无法读取某个文件时跳过并记录（推荐）",
            variable=self.file_skip_unreadable,
        ).grid(row=3, column=1, sticky="w", pady=(0, 7))
        ttk.Button(
            file_form,
            text="检查来源路径",
            style="Secondary.TButton",
            command=self.check_file_sources,
        ).grid(row=4, column=1, sticky="w")

    def _label(self, parent, text, row, rowspan=1):
        return ttk.Label(parent, text=text, style="Muted.TLabel").grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 14),
            pady=(0, 9),
            rowspan=rowspan,
        )

    def _build_plan_card(self) -> None:
        ttk.Label(
            self.plan_card,
            text="备份计划",
            style="Section.TLabel",
        ).pack(anchor="w")
        form = tk.Frame(self.plan_card, bg=CARD)
        form.pack(fill="x", pady=(16, 0))
        form.grid_columnconfigure(1, weight=1)

        self._label(form, "保存目录", 0)
        out_row = tk.Frame(form, bg=CARD)
        out_row.grid(row=0, column=1, sticky="ew", pady=(0, 10))
        out_row.grid_columnconfigure(0, weight=1)
        ttk.Entry(out_row, textvariable=self.output_dir).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            out_row,
            text="浏览…",
            style="Secondary.TButton",
            command=self.browse_output,
        ).grid(row=0, column=1, padx=(8, 0))

        self._label(form, "每天备份时间", 1)
        time_row = tk.Frame(form, bg=CARD)
        time_row.grid(row=1, column=1, sticky="ew", pady=(0, 10))
        ttk.Combobox(
            time_row,
            textvariable=self.backup_time,
            values=[
                f"{hour:02d}:{minute:02d}"
                for hour in range(24)
                for minute in (0, 15, 30, 45)
            ],
            width=10,
        ).pack(side="left")
        ttk.Label(
            time_row,
            text="  24 小时制，例如 02:30",
            style="Muted.TLabel",
        ).pack(side="left")

        self._label(form, "保留天数", 2)
        keep_row = tk.Frame(form, bg=CARD)
        keep_row.grid(row=2, column=1, sticky="ew", pady=(0, 10))
        ttk.Entry(keep_row, textvariable=self.retention_days, width=10).pack(side="left")
        ttk.Label(
            keep_row,
            text="  0 表示永久保留",
            style="Muted.TLabel",
        ).pack(side="left")

        divider = tk.Frame(self.plan_card, bg=BORDER, height=1)
        divider.pack(fill="x", pady=16)
        ttk.Label(
            self.plan_card,
            text="运行方式",
            style="Section.TLabel",
        ).pack(anchor="w")
        self._create_checkbutton(
            self.plan_card,
            text="登录 Windows 后自动启动",
            variable=self.start_with_windows,
        ).pack(anchor="w", pady=(12, 8))
        self._create_checkbutton(
            self.plan_card,
            text="关闭窗口时隐藏到系统托盘",
            variable=self.close_to_tray,
        ).pack(anchor="w", pady=(0, 14))

        schedule_box = tk.Frame(self.plan_card, bg=ACCENT_SOFT, padx=14, pady=12)
        schedule_box.pack(fill="x", pady=(3, 0))
        tk.Label(
            schedule_box,
            text="下次自动备份",
            bg=ACCENT_SOFT,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w")
        tk.Label(
            schedule_box,
            textvariable=self.next_run_text,
            bg=ACCENT_SOFT,
            fg=ACCENT_DARK,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w", pady=(3, 0))

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
                    raise ValueError(f"{name} 未设置保存目录。")
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

    def get_schedule_time(self) -> dt.time | None:
        try:
            return dt.datetime.strptime(self.backup_time.get(), "%H:%M").time()
        except ValueError:
            return None

    def _refresh_next_run(self) -> None:
        if self._next_run_after_id is not None:
            try:
                self.root.after_cancel(self._next_run_after_id)
            except tk.TclError:
                pass
            self._next_run_after_id = None
        schedule = self.get_schedule_time()
        if schedule is None:
            self.next_run_text.set("等待有效的时间设置")
            return
        now = dt.datetime.now()
        target = dt.datetime.combine(now.date(), schedule)
        if target <= now:
            target += dt.timedelta(days=1)
        weekdays = "一二三四五六日"
        self.next_run_text.set(
            f"{target:%Y-%m-%d}（周{weekdays[target.weekday()]}） {target:%H:%M}"
        )
        self._next_run_after_id = self.root.after(60_000, self._refresh_next_run)

    def _select_source_page(self, page_name: str) -> None:
        page = self.file_tab if page_name == "files" else self.database_tab
        page.tkraise()
        for name, button in self.source_tab_buttons.items():
            button.set_active(name == page_name)

    def _update_source_hint(self) -> None:
        database_enabled = self.database_backup_enabled.get()
        file_enabled = self.file_backup_enabled.get()
        database_count = 0
        for target in self.database_targets:
            if target.get("db_type") == "sqlite":
                database_count += len(target.get("sqlite_paths", []))
            else:
                database_count += len(target.get("databases", []))
        if database_enabled and file_enabled:
            self.connection_hint.set(
                f"数据库 {database_count} 个 + 文件 {len(self.file_sources)} 项"
            )
        elif file_enabled:
            self.connection_hint.set(f"已启用文件备份，共 {len(self.file_sources)} 项")
        elif database_enabled:
            self.connection_hint.set(
                f"已启用数据库备份，共 {database_count} 个数据库"
            )
        else:
            self.connection_hint.set("尚未启用任何备份")

    def _refresh_database_targets(self, selected_index: int | None = None) -> None:
        valid_indexes = set(range(len(self.database_targets)))
        self.database_target_checked.intersection_update(valid_indexes)
        if selected_index is not None:
            self.database_target_checked = {selected_index}
        for item in self.database_tree.get_children():
            self.database_tree.delete(item)
        for index, target in enumerate(self.database_targets):
            if target.get("db_type") == "sqlite":
                endpoint = "本地文件"
                databases = [
                    Path(path).name
                    for path in target.get("sqlite_paths", [])
                ]
                db_type = "SQLite"
            else:
                endpoint = (
                    f"{target.get('host', '')}:{target.get('port', 3306)}"
                )
                databases = list(target.get("databases", []))
                db_type = "MySQL"
            database_text = "、".join(str(name) for name in databases)
            if len(database_text) > 28:
                database_text = database_text[:27] + "…"
            self.database_tree.insert(
                "",
                "end",
                iid=str(index),
                image=self._database_checkbox_image(
                    index in self.database_target_checked
                ),
                text="",
                values=(
                    target.get("name", ""),
                    db_type,
                    endpoint,
                    database_text,
                ),
            )
        if selected_index is not None and self.database_tree.exists(
            str(selected_index)
        ):
            self.database_tree.selection_set(str(selected_index))
            self.database_tree.focus(str(selected_index))
            self.database_tree.see(str(selected_index))
        self._update_source_hint()

    def _database_checkbox_image(self, checked: bool) -> tk.PhotoImage | None:
        return (
            self.checkbox_checked_cell_image
            if checked
            else self.checkbox_unchecked_cell_image
        )

    def _set_database_checkbox(self, iid: str, checked: bool) -> None:
        image = self._database_checkbox_image(checked)
        if image is not None:
            self.database_tree.item(iid, image=image, text="")
        else:
            self.database_tree.item(iid, text="✓" if checked else "□")

    def _handle_database_tree_click(self, event: tk.Event) -> str | None:
        column = self.database_tree.identify_column(event.x)
        region = self.database_tree.identify_region(event.x, event.y)
        if region == "separator" and column in {"#0", "#1"}:
            return "break"
        if column != "#0" or region not in {"tree", "cell"}:
            return None
        iid = self.database_tree.identify_row(event.y)
        if not iid:
            return None
        index = int(iid)
        if index in self.database_target_checked:
            self.database_target_checked.remove(index)
            checked = False
        else:
            self.database_target_checked.add(index)
            checked = True
        self._set_database_checkbox(iid, checked)
        return "break"

    def _update_database_tree_cursor(self, event: tk.Event) -> None:
        locked = (
            self.database_tree.identify_region(event.x, event.y) == "separator"
            and self.database_tree.identify_column(event.x) in {"#0", "#1"}
        )
        self.database_tree.configure(cursor="no" if locked else "")

    def _handle_database_tree_double_click(self, event: tk.Event) -> str | None:
        if self.database_tree.identify_column(event.x) == "#0":
            return "break"
        iid = self.database_tree.identify_row(event.y)
        if iid:
            self._open_database_target_editor(int(iid))
        return "break"

    def _toggle_all_database_checks(self) -> None:
        if len(self.database_target_checked) == len(self.database_targets):
            self.database_target_checked.clear()
        else:
            self.database_target_checked = set(
                range(len(self.database_targets))
            )
        for index in range(len(self.database_targets)):
            iid = str(index)
            if self.database_tree.exists(iid):
                self._set_database_checkbox(
                    iid,
                    index in self.database_target_checked,
                )

    def _target_name_exists(
        self,
        name: str,
        ignore_index: int | None = None,
    ) -> bool:
        for index, target in enumerate(self.database_targets):
            if index == ignore_index:
                continue
            if str(target.get("name", "")).strip().casefold() == name.casefold():
                return True
        return False

    def _store_database_target(self, target: dict, index: int | None) -> None:
        name = str(target.get("name", "")).strip()
        if self._target_name_exists(name, index):
            messagebox.showerror(
                "名称重复",
                f"数据库配置名称“{name}”已存在，请换一个名称。",
                parent=self.root,
            )
            return
        password = str(target.pop("password", ""))
        target["password_enc"] = protect_password(password)
        if index is None:
            self.database_targets.append(target)
            selected_index = len(self.database_targets) - 1
        else:
            self.database_targets[index] = target
            selected_index = index
        self._refresh_database_targets(selected_index)

    def add_database_target(self, db_type: str) -> None:
        target = {
            "db_type": "sqlite" if db_type == "sqlite" else "mysql",
            "port": 3306,
        }
        dialog = DatabaseTargetDialog(self.root, target)
        self.root.wait_window(dialog)
        if dialog.result is not None:
            self._store_database_target(dialog.result, None)

    def _open_database_target_editor(self, index: int) -> None:
        if not 0 <= index < len(self.database_targets):
            return
        target = dict(self.database_targets[index])
        target["password"] = unprotect_password(
            str(target.get("password_enc", ""))
        )
        dialog = DatabaseTargetDialog(self.root, target)
        self.root.wait_window(dialog)
        if dialog.result is not None:
            self._store_database_target(dialog.result, index)

    def remove_selected_database_targets(self) -> None:
        indexes = sorted(self.database_target_checked, reverse=True)
        if not indexes:
            messagebox.showinfo(
                "未勾选",
                "请先勾选需要删除的数据库配置。",
                parent=self.root,
            )
            return
        names = [
            str(self.database_targets[index].get("name", ""))
            for index in indexes
        ]
        if not messagebox.askyesno(
            "删除勾选的数据库配置",
            f"确定删除勾选的 {len(indexes)} 个数据库配置吗？\n\n"
            + "\n".join(names),
            parent=self.root,
        ):
            return
        for index in indexes:
            del self.database_targets[index]
        self.database_target_checked.clear()
        self._refresh_database_targets()

    def _refresh_file_sources(self) -> None:
        self.file_listbox.delete(0, "end")
        for source in self.file_sources:
            self.file_listbox.insert("end", source)
        self._update_source_hint()

    def _add_file_source_paths(self, paths: tuple[str, ...] | list[str]) -> None:
        existing = {os.path.normcase(item) for item in self.file_sources}
        for path in paths:
            text = str(Path(path))
            key = os.path.normcase(os.path.abspath(text))
            if key not in existing:
                self.file_sources.append(text)
                existing.add(key)
        self._refresh_file_sources()

    def add_file_folders(self) -> None:
        path = filedialog.askdirectory(
            parent=self.root,
            title="选择需要备份的文件夹",
        )
        if path:
            self._add_file_source_paths([path])

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="选择需要备份的文件",
            filetypes=[("所有文件", "*.*")],
        )
        if paths:
            self._add_file_source_paths(paths)

    def remove_selected_files(self) -> None:
        selected = sorted(self.file_listbox.curselection(), reverse=True)
        if not selected:
            messagebox.showinfo("未选择", "请先选择需要移除的来源。", parent=self.root)
            return
        for index in selected:
            del self.file_sources[index]
        self._refresh_file_sources()

    def clear_file_sources(self) -> None:
        if not self.file_sources:
            return
        if messagebox.askyesno(
            "清空来源",
            "确定清空全部文件备份来源吗？",
            parent=self.root,
        ):
            self.file_sources.clear()
            self._refresh_file_sources()

    def check_file_sources(self) -> None:
        try:
            message = test_file_sources(self.file_sources)
        except Exception as exc:
            messagebox.showerror("来源检查失败", str(exc), parent=self.root)
            return
        messagebox.showinfo("来源检查通过", message, parent=self.root)

    def browse_output(self) -> None:
        path = filedialog.askdirectory(
            parent=self.root,
            title="选择备份保存目录",
            initialdir=self.output_dir.get() or str(Path.home()),
        )
        if path:
            self.output_dir.set(path)

    def start_backup(self, auto: bool = False) -> None:
        if self.backup_running:
            if not auto:
                messagebox.showinfo("正在备份", "当前已有备份任务正在执行。", parent=self.root)
            return
        config = self._collect_config()
        if not self._validate(config, show_dialog=not auto):
            return
        config["database_targets"] = database_targets_with_passwords(config)
        if auto:
            today = dt.date.today().isoformat()
            self.config["last_auto_attempt_date"] = today
            self.store.data = self.config
            try:
                self.store.save()
            except OSError as exc:
                self.logger.warning("记录自动备份日期失败：%s", exc)
        self.backup_running = True
        self.status_text.set("自动备份中…" if auto else "正在备份…")
        self.status_dot.config(fg="#C19C00")
        self.add_log(("自动任务：" if auto else "手动任务：") + "开始备份。")
        self.logger.info("%s备份开始", "自动" if auto else "手动")

        def progress(message: str):
            self.events.put(("progress", message))

        def worker():
            started = time.monotonic()
            try:
                results = run_backup_tasks(config, progress=progress)
                elapsed = time.monotonic() - started
                self.events.put(("backup_ok", (elapsed, results)))
            except Exception as exc:
                self.logger.error("备份失败：%s\n%s", exc, traceback.format_exc())
                self.events.put(("backup_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self.add_log(str(payload), "muted")
                elif kind == "job_batch_done":
                    records, elapsed, auto = payload
                    self._finish_job_batch(records, float(elapsed), bool(auto))
                elif kind == "backup_ok":
                    elapsed, results = payload
                    self._finish_backup_success(float(elapsed), results)
                elif kind == "backup_error":
                    self._finish_backup_error(str(payload))
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
        self._refresh_job_tree()
        if error_messages and not auto:
            messagebox.showerror(
                "部分备份失败",
                "\n".join(error_messages),
                parent=self.root,
            )

    def _finish_backup_success(
        self,
        elapsed: float,
        results: list[BackupResult],
    ) -> None:
        self.backup_running = False
        self.status_text.set(f"备份成功（{len(results)} 项）")
        self.status_dot.config(fg=SUCCESS)
        now = dt.datetime.now()
        paths = [str(result.path) for result in results]
        self.config.update(
            {
                "last_backup_date": now.strftime("%Y-%m-%d %H:%M:%S"),
                "last_backup_status": "success",
                "last_backup_file": " | ".join(paths),
            }
        )
        self.store.data = self.config
        try:
            self.store.save()
        except OSError as exc:
            self.logger.error("保存备份结果失败：%s", exc)
        for result in results:
            self.add_log(
                f"备份成功：{result.path.name}"
                f"（{result.source_name}，共 {_format_size(result.bytes_written)}）",
                "success",
            )
        self.add_log(f"本次备份用时 {elapsed:.1f} 秒。", "success")
        self.last_result.config(text=f"上次成功：{now:%Y-%m-%d %H:%M:%S}", fg=SUCCESS)
        for path in paths:
            self.logger.info("备份成功：%s", path)
        self._refresh_next_run()

    def _finish_backup_error(self, message: str) -> None:
        self.backup_running = False
        self.status_text.set("备份失败")
        self.status_dot.config(fg=DANGER)
        self.config["last_backup_status"] = "error"
        self.store.data = self.config
        try:
            self.store.save()
        except OSError:
            pass
        self.add_log(f"备份失败：{message}", "error")
        self.last_result.config(text="上次备份失败，请查看运行记录", fg=DANGER)
        messagebox.showerror("备份失败", message, parent=self.root)

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
                    lambda _icon, _item: self.root.after(0, self.show_window),
                    default=True,
                ),
                pystray.MenuItem(
                    "立即备份",
                    lambda _icon, _item: self.root.after(
                        0, self.start_all_jobs
                    ),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "退出程序",
                    lambda _icon, _item: self.root.after(0, self.exit_app),
                ),
            )
            self.tray_icon = pystray.Icon(APP_NAME, image, APP_TITLE, menu)
            threading.Thread(
                target=self.tray_icon.run,
                name="tray-icon",
                daemon=True,
            ).start()
        except Exception as exc:
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
            except Exception:
                pass
        self.root.quit()
        self.root.destroy()


def self_test() -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="dbbap_test_"))
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
        except Exception:
            failed += 1
            logger.error(
                "命令行任务失败：%s\n%s",
                job.get("name", ""),
                traceback.format_exc(),
            )
    store.data = config
    store.save()
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
        except Exception:
            configure_logging().error("自检失败：%s", traceback.format_exc())
            return 1
    if args.backup_once:
        return backup_once()

    acquired, mutex = acquire_single_instance()
    if not acquired:
        ctypes.windll.user32.MessageBoxW(
            None,
            "数据自动备份助手已经在运行，请从系统托盘打开。",
            APP_TITLE,
            0x40,
        )
        return 0

    root = tk.Tk()
    BackupApp(root, minimized=args.minimized)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

