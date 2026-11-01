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
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

from backup_engine import (
    apply_retention,
    list_mysql_databases,
    run_backup,
    test_mysql,
    test_sqlite,
)
from config_store import (
    APP_NAME,
    APP_DISPLAY_NAME,
    APP_VERSION,
    LOG_DIR,
    ConfigStore,
    protect_password,
    unprotect_password,
)
from windows_integration import set_autostart


APP_TITLE = APP_DISPLAY_NAME
BG = "#F3F6FA"
CARD = "#FFFFFF"
TEXT = "#172033"
MUTED = "#6B778C"
ACCENT = "#176BFF"
ACCENT_DARK = "#0C55D8"
SUCCESS = "#14875B"
DANGER = "#C43B45"
BORDER = "#DFE5EE"


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
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
        stored = str(app.config.get("last_auto_attempt_date", ""))
        self.last_attempt_date = (
            stored if stored == dt.date.today().isoformat() else ""
        )

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        time.sleep(3)
        while not self.stop_event.is_set():
            try:
                if not self.app.backup_running:
                    now = dt.datetime.now()
                    configured = self.app.get_schedule_time()
                    if configured and now.time() >= configured:
                        today = now.date().isoformat()
                        if self.last_attempt_date != today:
                            self.last_attempt_date = today
                            self.app.root.after(
                                0,
                                lambda: self.app.start_backup(auto=True),
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
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.backup_running = False
        self.tray_icon = None
        self._closing = False
        self._next_run_after_id = None
        self._build_variables()
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
        self.db_type = tk.StringVar(value="mysql")
        self.host = tk.StringVar()
        self.port = tk.StringVar()
        self.user = tk.StringVar()
        self.password = tk.StringVar()
        self.mysql_database = tk.StringVar()
        self.sqlite_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.backup_time = tk.StringVar()
        self.retention_days = tk.StringVar()
        self.start_with_windows = tk.BooleanVar()
        self.close_to_tray = tk.BooleanVar()
        self.status_text = tk.StringVar(value="准备就绪")
        self.next_run_text = tk.StringVar()
        self.connection_hint = tk.StringVar(value="尚未测试连接")

    def _configure_window(self) -> None:
        self.root.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.root.geometry("1080x820")
        self.root.minsize(960, 720)
        self.root.configure(bg=BG)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
        try:
            icon_path = resource_path("app.ico")
            if icon_path.exists():
                self.root.iconbitmap(str(icon_path))
        except tk.TclError:
            pass

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
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
            fieldbackground="#FBFCFE",
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=(8, 7),
        )
        style.configure(
            "TCombobox",
            fieldbackground="#FBFCFE",
            background="#FBFCFE",
            arrowcolor=TEXT,
            bordercolor=BORDER,
            padding=(6, 6),
        )
        style.map("TCombobox", fieldbackground=[("readonly", "#FBFCFE")])
        style.configure(
            "Primary.TButton",
            background=ACCENT,
            foreground="#FFFFFF",
            borderwidth=0,
            padding=(18, 9),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", ACCENT_DARK), ("disabled", "#A6BEE9")],
        )
        style.configure(
            "Secondary.TButton",
            background="#FFFFFF",
            foreground=TEXT,
            bordercolor=BORDER,
            padding=(14, 8),
        )
        style.map("Secondary.TButton", background=[("active", "#F1F5FB")])
        style.configure(
            "TCheckbutton",
            background=CARD,
            foreground=TEXT,
            font=("Microsoft YaHei UI", 9),
        )
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure(
            "TNotebook",
            background=BG,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=BG,
            foreground=MUTED,
            padding=(18, 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD)],
            foreground=[("selected", TEXT)],
        )

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg="#102A43", height=112)
        header.pack(fill="x")
        header.pack_propagate(False)
        title_box = tk.Frame(header, bg="#102A43")
        title_box.pack(side="left", padx=34, pady=23)
        tk.Label(
            title_box,
            text=APP_TITLE,
            bg="#102A43",
            fg="#FFFFFF",
            font=("Microsoft YaHei UI", 21, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="自动、可靠地保护你的数据",
            bg="#102A43",
            fg="#AFC4D8",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(5, 0))

        status_box = tk.Frame(header, bg="#193B5C", padx=16, pady=9)
        status_box.pack(side="right", padx=34)
        self.status_dot = tk.Label(
            status_box,
            text="●",
            bg="#193B5C",
            fg="#5DD6A5",
            font=("Microsoft YaHei UI", 11),
        )
        self.status_dot.pack(side="left", padx=(0, 7))
        tk.Label(
            status_box,
            textvariable=self.status_text,
            bg="#193B5C",
            fg="#FFFFFF",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(side="bottom", fill="x", padx=30, pady=(0, 22))
        self.last_result = tk.Label(
            footer,
            text="",
            bg=BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        self.last_result.pack(side="left", fill="x", expand=True)
        ttk.Button(
            footer,
            text="立即备份",
            style="Secondary.TButton",
            command=lambda: self.start_backup(auto=False),
        ).pack(side="right", padx=(10, 0))
        self.save_button = ttk.Button(
            footer,
            text="保存设置",
            style="Primary.TButton",
            command=self.save_settings,
        )
        self.save_button.pack(side="right", padx=(10, 0))

        content = tk.Frame(self.root, bg=BG)
        content.pack(fill="both", expand=True, padx=28, pady=22)
        content.grid_columnconfigure(0, weight=1, uniform="card")
        content.grid_columnconfigure(1, weight=1, uniform="card")
        content.grid_rowconfigure(2, weight=1)

        self.connection_card = self._card(content, 0, 0, padx=(0, 8), pady=(0, 14))
        self.plan_card = self._card(content, 0, 1, padx=(8, 0), pady=(0, 14))
        self._build_connection_card()
        self._build_plan_card()

        log_card = self._card(content, 2, 0, columnspan=2, pady=0)
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
        inner.pack(fill="both", expand=True, padx=24, pady=20)
        return inner

    def _build_connection_card(self) -> None:
        top = tk.Frame(self.connection_card, bg=CARD)
        top.pack(fill="x")
        ttk.Label(top, text="数据库连接", style="Section.TLabel").pack(side="left")
        ttk.Label(
            top,
            textvariable=self.connection_hint,
            style="Muted.TLabel",
        ).pack(side="right")

        form = tk.Frame(self.connection_card, bg=CARD)
        form.pack(fill="x", pady=(16, 0))
        form.grid_columnconfigure(1, weight=1)

        self._label(form, "数据库类型", 0)
        self.type_combo = ttk.Combobox(
            form,
            textvariable=self.db_type,
            values=("MySQL / MariaDB", "SQLite"),
            state="readonly",
        )
        self.type_combo.grid(row=0, column=1, sticky="ew", pady=(0, 10))
        self.type_combo.bind("<<ComboboxSelected>>", self._on_type_change)

        self.mysql_fields = tk.Frame(form, bg=CARD)
        self.mysql_fields.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.mysql_fields.grid_columnconfigure(1, weight=1)

        self._label(self.mysql_fields, "服务器地址", 0)
        ttk.Entry(self.mysql_fields, textvariable=self.host).grid(
            row=0, column=1, sticky="ew", pady=(0, 9)
        )
        self._label(self.mysql_fields, "端口", 1, rowspan=1)
        ttk.Entry(self.mysql_fields, textvariable=self.port, width=8).grid(
            row=1, column=1, sticky="ew", pady=(0, 9)
        )
        self._label(self.mysql_fields, "用户名", 2)
        ttk.Entry(self.mysql_fields, textvariable=self.user).grid(
            row=2, column=1, sticky="ew", pady=(0, 9)
        )
        self._label(self.mysql_fields, "密码", 3)
        ttk.Entry(self.mysql_fields, textvariable=self.password, show="●").grid(
            row=3, column=1, sticky="ew", pady=(0, 9)
        )
        self._label(self.mysql_fields, "数据库", 4)
        row = tk.Frame(self.mysql_fields, bg=CARD)
        row.grid(row=4, column=1, sticky="ew", pady=(0, 9))
        row.grid_columnconfigure(0, weight=1)
        self.database_combo = ttk.Combobox(
            row,
            textvariable=self.mysql_database,
        )
        self.database_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            row,
            text="获取列表",
            style="Secondary.TButton",
            command=self.load_databases,
        ).grid(row=0, column=1, padx=(8, 0))

        self.sqlite_fields = tk.Frame(form, bg=CARD)
        self.sqlite_fields.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.sqlite_fields.grid_columnconfigure(1, weight=1)
        self._label(self.sqlite_fields, "数据库文件", 0)
        sqlite_row = tk.Frame(self.sqlite_fields, bg=CARD)
        sqlite_row.grid(row=0, column=1, sticky="ew", pady=(0, 9))
        sqlite_row.grid_columnconfigure(0, weight=1)
        ttk.Entry(sqlite_row, textvariable=self.sqlite_path).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            sqlite_row,
            text="浏览…",
            style="Secondary.TButton",
            command=self.browse_sqlite,
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(
            self.sqlite_fields,
            text="将使用 SQLite 在线备份接口生成一致性快照。",
            style="Muted.TLabel",
        ).grid(row=1, column=1, sticky="w")

        actions = tk.Frame(self.connection_card, bg=CARD)
        actions.pack(fill="x", pady=(4, 0))
        self.test_button = ttk.Button(
            actions,
            text="测试连接",
            style="Secondary.TButton",
            command=self.test_connection,
        )
        self.test_button.pack(side="left")

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
        ttk.Checkbutton(
            self.plan_card,
            text="登录 Windows 后自动启动",
            variable=self.start_with_windows,
        ).pack(anchor="w", pady=(12, 8))
        ttk.Checkbutton(
            self.plan_card,
            text="关闭窗口时隐藏到系统托盘",
            variable=self.close_to_tray,
        ).pack(anchor="w", pady=(0, 14))

        schedule_box = tk.Frame(self.plan_card, bg="#F1F6FF", padx=14, pady=12)
        schedule_box.pack(fill="x", pady=(3, 0))
        tk.Label(
            schedule_box,
            text="下次自动备份",
            bg="#F1F6FF",
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w")
        tk.Label(
            schedule_box,
            textvariable=self.next_run_text,
            bg="#F1F6FF",
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

        text_frame = tk.Frame(parent, bg="#0F1B2D", padx=1, pady=1)
        text_frame.pack(fill="both", expand=True, pady=(14, 0))
        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side="right", fill="y")
        self.log_text = tk.Text(
            text_frame,
            height=7,
            bg="#0F1B2D",
            fg="#D5E0EF",
            insertbackground="#FFFFFF",
            selectbackground="#244B76",
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=10,
            font=("Consolas", 9),
            state="disabled",
            yscrollcommand=scrollbar.set,
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.log_text.yview)
        self.log_text.tag_configure("success", foreground="#64D8A6")
        self.log_text.tag_configure("error", foreground="#FF8D96")
        self.log_text.tag_configure("muted", foreground="#92A4BA")

    def _load_into_ui(self) -> None:
        config = self.config
        label = "SQLite" if config.get("db_type") == "sqlite" else "MySQL / MariaDB"
        self.db_type.set(label)
        self.host.set(str(config.get("mysql_host", "")))
        self.port.set(str(config.get("mysql_port", 3306)))
        self.user.set(str(config.get("mysql_user", "")))
        self.password.set(self.store.get_password())
        self.mysql_database.set(str(config.get("mysql_database", "")))
        self.sqlite_path.set(str(config.get("sqlite_path", "")))
        self.output_dir.set(str(config.get("output_dir", "")))
        self.backup_time.set(str(config.get("backup_time", "02:00")))
        self.retention_days.set(str(config.get("retention_days", 30)))
        self.start_with_windows.set(bool(config.get("start_with_windows", False)))
        self.close_to_tray.set(bool(config.get("close_to_tray", True)))
        self._on_type_change()
        self._refresh_next_run()
        try:
            set_autostart(self.start_with_windows.get())
        except OSError as exc:
            self.logger.warning("更新开机启动项失败：%s", exc)
        if config.get("last_backup_status") == "success":
            self.last_result.config(
                text=f"上次成功：{config.get('last_backup_date', '')}  "
                f"{config.get('last_backup_file', '')}",
                fg=SUCCESS,
            )

    def _collect_config(self) -> dict:
        config = dict(self.config)
        config.update(
            {
                "db_type": "sqlite" if self.db_type.get() == "SQLite" else "mysql",
                "mysql_host": self.host.get().strip(),
                "mysql_port": self.port.get().strip(),
                "mysql_user": self.user.get().strip(),
                "mysql_database": self.mysql_database.get().strip(),
                "sqlite_path": self.sqlite_path.get().strip(),
                "output_dir": self.output_dir.get().strip(),
                "backup_time": self.backup_time.get().strip(),
                "retention_days": self.retention_days.get().strip(),
                "start_with_windows": self.start_with_windows.get(),
                "close_to_tray": self.close_to_tray.get(),
            }
        )
        return config

    def _validate(self, config: dict, show_dialog: bool = True) -> bool:
        try:
            if config["db_type"] == "mysql":
                if not config["mysql_host"]:
                    raise ValueError("请填写 MySQL 服务器地址。")
                port = int(config["mysql_port"])
                if not 1 <= port <= 65535:
                    raise ValueError("MySQL 端口必须在 1 到 65535 之间。")
                if not config["mysql_user"]:
                    raise ValueError("请填写 MySQL 用户名。")
                if not config["mysql_database"]:
                    raise ValueError("请选择要备份的 MySQL 数据库。")
            else:
                if not Path(config["sqlite_path"]).is_file():
                    raise ValueError("请选择有效的 SQLite 数据库文件。")
            if not config["output_dir"]:
                raise ValueError("请选择备份文件保存目录。")
            try:
                parsed = dt.datetime.strptime(config["backup_time"], "%H:%M")
            except ValueError as exc:
                raise ValueError("备份时间格式应为 HH:MM，例如 02:30。") from exc
            if not 0 <= int(config["retention_days"]) <= 3650:
                raise ValueError("保留天数应在 0 到 3650 之间。")
            config["backup_time"] = parsed.strftime("%H:%M")
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
        self.config["mysql_password_enc"] = self.store.data.get(
            "mysql_password_enc", ""
        )
        self.store.set_password(self.password.get())
        self.store.data = self.config
        try:
            self.store.save()
            Path(self.config["output_dir"]).mkdir(parents=True, exist_ok=True)
            set_autostart(bool(self.config["start_with_windows"]))
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法保存设置：{exc}", parent=self.root)
            return False
        self._refresh_next_run()
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

    def _on_type_change(self, _event=None) -> None:
        is_mysql = self.db_type.get() != "SQLite"
        if is_mysql:
            self.sqlite_fields.grid_remove()
            self.mysql_fields.grid()
        else:
            self.mysql_fields.grid_remove()
            self.sqlite_fields.grid()

    def browse_sqlite(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择 SQLite 数据库",
            filetypes=[("SQLite 数据库", "*.db *.sqlite *.sqlite3 *.db3"),
                       ("所有文件", "*.*")],
        )
        if path:
            self.sqlite_path.set(path)

    def browse_output(self) -> None:
        path = filedialog.askdirectory(
            parent=self.root,
            title="选择备份保存目录",
            initialdir=self.output_dir.get() or str(Path.home()),
        )
        if path:
            self.output_dir.set(path)

    def test_connection(self) -> None:
        config = self._collect_config()
        if config["db_type"] == "mysql":
            try:
                if not config["mysql_host"]:
                    raise ValueError("请填写 MySQL 服务器地址。")
                if not config["mysql_user"]:
                    raise ValueError("请填写 MySQL 用户名。")
                port = int(config["mysql_port"])
                if not 1 <= port <= 65535:
                    raise ValueError("MySQL 端口必须在 1 到 65535 之间。")
            except (ValueError, TypeError) as exc:
                messagebox.showerror("设置有误", str(exc), parent=self.root)
                return
        elif not Path(config["sqlite_path"]).is_file():
            messagebox.showerror(
                "设置有误",
                "请选择有效的 SQLite 数据库文件。",
                parent=self.root,
            )
            return
        self.connection_hint.set("正在连接…")
        self.test_button.config(state="disabled")
        db_type = config["db_type"]
        config["mysql_password"] = self.password.get()

        def worker():
            try:
                message = (
                    test_sqlite(config["sqlite_path"])
                    if db_type == "sqlite"
                    else test_mysql(config)
                )
                self.events.put(("connection_ok", message))
            except Exception as exc:
                self.events.put(("connection_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def load_databases(self) -> None:
        config = self._collect_config()
        if config["db_type"] != "mysql":
            return
        try:
            int(config["mysql_port"])
        except ValueError:
            messagebox.showerror("设置有误", "MySQL 端口必须是数字。", parent=self.root)
            return
        config["mysql_password"] = self.password.get()
        self.connection_hint.set("正在获取数据库列表…")

        def worker():
            try:
                names = list_mysql_databases(config)
                self.events.put(("databases", "\n".join(names)))
            except Exception as exc:
                self.events.put(("connection_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def start_backup(self, auto: bool = False) -> None:
        if self.backup_running:
            if not auto:
                messagebox.showinfo("正在备份", "当前已有备份任务正在执行。", parent=self.root)
            return
        config = self._collect_config()
        if not self._validate(config, show_dialog=not auto):
            return
        config["mysql_password"] = self.password.get()
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
        self.status_dot.config(fg="#FFD166")
        self.add_log(("自动任务：" if auto else "手动任务：") + "开始备份。")
        self.logger.info("%s备份开始", "自动" if auto else "手动")

        def progress(message: str):
            self.events.put(("progress", message))

        def worker():
            started = time.monotonic()
            try:
                result = run_backup(config, progress=progress)
                elapsed = time.monotonic() - started
                self.events.put(
                    ("backup_ok", f"{result.path}\n{elapsed:.1f}\n{result.database}")
                )
            except Exception as exc:
                self.logger.error("备份失败：%s\n%s", exc, traceback.format_exc())
                self.events.put(("backup_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self.add_log(payload, "muted")
                elif kind == "connection_ok":
                    self.connection_hint.set("连接正常")
                    self.test_button.config(state="normal")
                    self.add_log(payload, "success")
                elif kind == "connection_error":
                    self.connection_hint.set("连接失败")
                    self.test_button.config(state="normal")
                    self.add_log(payload, "error")
                    messagebox.showerror("连接失败", payload, parent=self.root)
                elif kind == "databases":
                    names = payload.splitlines() if payload else []
                    self.database_combo["values"] = names
                    self.connection_hint.set(f"已找到 {len(names)} 个数据库")
                    if names and self.mysql_database.get() not in names:
                        self.mysql_database.set(names[0])
                    self.add_log(f"数据库列表已更新，共 {len(names)} 个。", "success")
                elif kind == "backup_ok":
                    path, elapsed, database = payload.split("\n", 2)
                    self._finish_backup_success(path, elapsed, database)
                elif kind == "backup_error":
                    self._finish_backup_error(payload)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(150, self._poll_events)

    def _finish_backup_success(self, path: str, elapsed: str, database: str) -> None:
        self.backup_running = False
        self.status_text.set("备份成功")
        self.status_dot.config(fg="#5DD6A5")
        now = dt.datetime.now()
        self.config.update(
            {
                "last_backup_date": now.strftime("%Y-%m-%d %H:%M:%S"),
                "last_backup_status": "success",
                "last_backup_file": path,
            }
        )
        self.store.data = self.config
        try:
            self.store.save()
        except OSError as exc:
            self.logger.error("保存备份结果失败：%s", exc)
        self.add_log(
            f"备份成功：{Path(path).name}，用时 {elapsed} 秒。",
            "success",
        )
        self.last_result.config(text=f"上次成功：{now:%Y-%m-%d %H:%M:%S}", fg=SUCCESS)
        self.logger.info("备份成功：%s", path)
        self._refresh_next_run()

    def _finish_backup_error(self, message: str) -> None:
        self.backup_running = False
        self.status_text.set("备份失败")
        self.status_dot.config(fg="#FF8D96")
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
                        0, lambda: self.start_backup(auto=False)
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
    config = {
        "db_type": "sqlite",
        "sqlite_path": str(db_path),
        "output_dir": str(temp_dir / "backups"),
        "retention_days": 0,
    }
    result = run_backup(config)
    backup = sqlite3.connect(result.path)
    try:
        count = backup.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        backup.close()
    if count != 2:
        raise RuntimeError("SQLite 自检数据不一致")

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
    config["mysql_password"] = store.get_password()
    logger = configure_logging()
    try:
        result = run_backup(
            config,
            progress=lambda message: logger.info(message),
        )
        logger.info("命令行备份成功：%s", result.path)
        return 0
    except Exception:
        logger.error("命令行备份失败：%s", traceback.format_exc())
        return 1


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

