from __future__ import annotations

import queue
import socket
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk
from typing import Any

from backup_job_dialog import ImageCheckbutton
from centered_dialogs import showerror, showinfo
from config_store import protect_password, unprotect_password
from notifications import (
    channel_is_configured,
    list_system_backup_tasks,
    notification_settings,
    send_notifications,
)
from windows_integration import center_window_over_parent

BG = "#F7F8FA"
PANEL = "#FFFFFF"
BORDER = "#D0D0D0"
TEXT = "#1A1A1A"
MUTED = "#666666"
ACCENT = "#0078D4"
ACCENT_SOFT = "#E8F0FE"


def _resource_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent.parent / "assets"
    return base / name


class _TabbedDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, size: str) -> None:
        super().__init__(parent)
        self.withdraw()
        self._owner = parent
        self._tab_buttons: list[tuple[tk.Button, tk.Frame]] = []
        self._tab_pages: list[tk.Frame] = []
        self._active_tab = -1
        self._checked_image: tk.PhotoImage | None = None
        self._unchecked_image: tk.PhotoImage | None = None
        try:
            self._checked_image = tk.PhotoImage(
                file=str(_resource_path("checkbox-checked.png"))
            )
            self._unchecked_image = tk.PhotoImage(
                file=str(_resource_path("checkbox-unchecked.png"))
            )
        except tk.TclError:
            pass
        self.title(title)
        self.configure(bg=BG)
        self.geometry(size)
        self.minsize(720, 600)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

    def _checkbutton(
        self,
        parent: tk.Misc,
        *,
        text: str,
        variable: tk.BooleanVar,
    ) -> tk.Widget:
        if self._checked_image is not None and self._unchecked_image is not None:
            return ImageCheckbutton(
                parent,
                text=text,
                variable=variable,
                checked_image=self._checked_image,
                unchecked_image=self._unchecked_image,
            )
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            bg=PANEL,
            fg=TEXT,
            activebackground=PANEL,
            activeforeground=TEXT,
            selectcolor=PANEL,
            font=("Microsoft YaHei UI", 9),
            bd=0,
            highlightthickness=0,
            anchor="w",
            cursor="hand2",
        )

    def _create_tabs(
        self,
        parent: tk.Misc,
        titles: tuple[str, ...],
    ) -> tk.Frame:
        bar = tk.Frame(parent, bg=PANEL)
        bar.pack(fill="x")
        content = tk.Frame(parent, bg=PANEL)
        content.pack(fill="both", expand=True)
        for index, title in enumerate(titles):
            holder = tk.Frame(bar, bg=BORDER, padx=1, pady=1)
            holder.pack(side="left", padx=(0, 1))
            button = tk.Button(
                holder,
                text=title,
                command=lambda selected=index: self._select_tab(selected),
                bg="#F5F5F5",
                fg=MUTED,
                activebackground=ACCENT_SOFT,
                activeforeground=ACCENT,
                relief="flat",
                bd=0,
                highlightthickness=0,
                padx=18,
                pady=8,
                font=("Microsoft YaHei UI", 9),
                cursor="hand2",
            )
            button.pack(fill="x")
            indicator = tk.Frame(holder, height=2, bg="#F5F5F5")
            indicator.pack(fill="x")
            page = tk.Frame(
                content,
                bg=PANEL,
                highlightbackground=BORDER,
                highlightthickness=1,
                padx=16,
                pady=14,
            )
            self._tab_buttons.append((button, indicator))
            self._tab_pages.append(page)
        self._select_tab(0)
        return content

    def _select_tab(self, index: int) -> None:
        if not 0 <= index < len(self._tab_pages):
            return
        self._active_tab = index
        for page_index, page in enumerate(self._tab_pages):
            if page_index == index:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        for item_index, (button, indicator) in enumerate(self._tab_buttons):
            selected = item_index == index
            button.configure(
                bg=PANEL if selected else "#F5F5F5",
                fg=ACCENT if selected else MUTED,
                font=(
                    "Microsoft YaHei UI",
                    9,
                    "bold" if selected else "normal",
                ),
            )
            indicator.configure(bg=ACCENT if selected else "#F5F5F5")

    def _show_centered(self) -> None:
        if not self.winfo_exists():
            return
        self.update_idletasks()
        width = min(
            max(800, self.winfo_reqwidth() + 20),
            self.winfo_screenwidth() - 80,
        )
        height = min(
            max(650, self.winfo_reqheight() + 20),
            self.winfo_screenheight() - 100,
        )
        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        center_window_over_parent(self, self._owner)
        self.deiconify()
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            pass


class NotificationChannelDialog(_TabbedDialog):
    def __init__(
        self,
        parent: tk.Misc,
        settings: dict[str, Any],
    ) -> None:
        super().__init__(parent, "通知渠道设置", "800x650")
        self.result: dict[str, Any] | None = None
        self._settings = notification_settings(settings)
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._test_running: set[str] = set()
        self.test_button: ttk.Button | None = None
        self._build()
        self.after(50, self._show_centered)
        self.after(100, self._poll_events)

    def _build(self) -> None:
        outer = tk.Frame(
            self,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        outer.pack(fill="both", expand=True, padx=14, pady=14)
        header = tk.Frame(outer, bg=PANEL)
        header.pack(fill="x", padx=20, pady=(16, 10))
        tk.Label(
            header,
            text="通知渠道设置",
            bg=PANEL,
            fg=TEXT,
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="配置邮件服务器和钉钉群机器人，保存后可分别测试发送。",
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(4, 0))

        footer = tk.Frame(outer, bg=PANEL)
        footer.pack(side="bottom", fill="x", padx=20, pady=(12, 16))
        self.status_var = tk.StringVar()
        ttk.Label(
            footer,
            textvariable=self.status_var,
            style="Muted.TLabel",
        ).pack(side="left")
        ttk.Button(
            footer,
            text="保存",
            style="Primary.TButton",
            command=self._save,
        ).pack(side="right")
        ttk.Button(
            footer,
            text="取消",
            style="Secondary.TButton",
            command=self.destroy,
        ).pack(side="right", padx=(0, 10))
        self.test_button = ttk.Button(
            footer,
            text="测试发送",
            style="Secondary.TButton",
            command=self._test_current,
        )
        self.test_button.pack(side="right", padx=(0, 10))

        self._create_tabs(outer, ("邮件", "钉钉"))
        self._build_email_tab(self._tab_pages[0])
        self._build_dingtalk_tab(self._tab_pages[1])

    def _build_email_tab(self, tab: tk.Frame) -> None:
        email = self._settings["email"]
        self.smtp_host_var = tk.StringVar(value=str(email["smtp_host"]))
        self.smtp_port_var = tk.StringVar(value=str(email["smtp_port"]))
        self.smtp_security_var = tk.StringVar(value=str(email["security"]))
        self.smtp_username_var = tk.StringVar(value=str(email["username"]))
        self.smtp_password_var = tk.StringVar(
            value=unprotect_password(str(email.get("password_enc", "")))
        )
        self.mail_sender_var = tk.StringVar(value=str(email["sender"]))
        self.mail_recipients_var = tk.StringVar(
            value=str(email["recipients"])
        )
        tab.grid_columnconfigure(1, weight=1)
        fields = (
            ("SMTP 服务器", self.smtp_host_var),
            ("SMTP 端口", self.smtp_port_var),
            ("登录用户名", self.smtp_username_var),
            ("授权密码", self.smtp_password_var),
            ("发件人邮箱", self.mail_sender_var),
            ("收件人邮箱", self.mail_recipients_var),
        )
        for row, (label, variable) in enumerate(fields, 0):
            ttk.Label(tab, text=label, style="Muted.TLabel").grid(
                row=row,
                column=0,
                sticky="w",
                padx=(0, 14),
                pady=5,
            )
            entry = ttk.Entry(tab, textvariable=variable)
            if label == "授权密码":
                entry.configure(show="●")
            entry.grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Label(tab, text="连接方式", style="Muted.TLabel").grid(
            row=6,
            column=0,
            sticky="w",
            padx=(0, 14),
            pady=5,
        )
        ttk.Combobox(
            tab,
            textvariable=self.smtp_security_var,
            values=("ssl", "starttls", "none"),
            state="readonly",
            width=14,
        ).grid(row=6, column=1, sticky="w", pady=5)
        ttk.Label(
            tab,
            text="多个收件人可用英文逗号或分号分隔。",
            style="Muted.TLabel",
        ).grid(row=7, column=1, sticky="w", pady=(8, 0))
    def _build_dingtalk_tab(self, tab: tk.Frame) -> None:
        dingtalk = self._settings["dingtalk"]
        self.dingtalk_webhook_var = tk.StringVar(
            value=unprotect_password(str(dingtalk.get("webhook_enc", "")))
        )
        self.dingtalk_secret_var = tk.StringVar(
            value=unprotect_password(str(dingtalk.get("secret_enc", "")))
        )
        self.dingtalk_mobiles_var = tk.StringVar(
            value=str(dingtalk["at_mobiles"])
        )
        tab.grid_columnconfigure(1, weight=1)
        fields = (
            ("机器人 Webhook", self.dingtalk_webhook_var),
            ("加签密钥", self.dingtalk_secret_var),
            ("提醒手机号", self.dingtalk_mobiles_var),
        )
        for row, (label, variable) in enumerate(fields, 0):
            ttk.Label(tab, text=label, style="Muted.TLabel").grid(
                row=row,
                column=0,
                sticky="nw",
                padx=(0, 14),
                pady=6,
            )
            ttk.Entry(tab, textvariable=variable).grid(
                row=row,
                column=1,
                sticky="ew",
                pady=6,
            )
        ttk.Label(
            tab,
            text=(
                "在钉钉群中添加“自定义机器人”，安全设置建议使用"
                "“加签”。Webhook 和密钥会使用 Windows DPAPI 加密保存。\n\n"
                "多个提醒手机号可用英文逗号或分号分隔；留空则不 @ 人。"
            ),
            style="Muted.TLabel",
            wraplength=610,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))

    def _channel_settings(self, *, encrypt: bool) -> dict[str, Any]:
        return {
            "email": {
                "smtp_host": self.smtp_host_var.get().strip(),
                "smtp_port": self.smtp_port_var.get().strip(),
                "security": self.smtp_security_var.get(),
                "username": self.smtp_username_var.get().strip(),
                "password_enc": (
                    protect_password(self.smtp_password_var.get())
                    if encrypt
                    else self.smtp_password_var.get()
                ),
                "sender": self.mail_sender_var.get().strip(),
                "recipients": self.mail_recipients_var.get().strip(),
            },
            "dingtalk": {
                "webhook_enc": (
                    protect_password(self.dingtalk_webhook_var.get().strip())
                    if encrypt
                    else self.dingtalk_webhook_var.get().strip()
                ),
                "secret_enc": (
                    protect_password(self.dingtalk_secret_var.get().strip())
                    if encrypt
                    else self.dingtalk_secret_var.get().strip()
                ),
                "at_mobiles": self.dingtalk_mobiles_var.get().strip(),
            },
        }

    def _save(self) -> None:
        self.result = self._channel_settings(encrypt=True)
        self.destroy()

    def _test_channel(self, channel: str) -> None:
        if channel in self._test_running:
            return
        channels = self._channel_settings(encrypt=False)
        settings = notification_settings({**self._settings, **channels})
        settings["email"]["password"] = self.smtp_password_var.get()
        settings["dingtalk"]["webhook"] = self.dingtalk_webhook_var.get().strip()
        settings["dingtalk"]["secret"] = self.dingtalk_secret_var.get().strip()
        channel_name = "邮件" if channel == "email" else "钉钉"
        computer_name = socket.gethostname()
        self._test_running.add(channel)
        self.test_button.configure(state="disabled")
        self.status_var.set(f"正在测试{channel_name}…")

        def worker() -> None:
            try:
                results = send_notifications(
                    settings,
                    f"数据自动备份助手{channel_name}通知测试",
                    f"{channel_name}通知配置测试。\n\n主机：{computer_name}",
                    channels={channel},
                )
                self._events.put(("test_done", (channel, results)))
            except Exception as exc:  # noqa: BLE001
                self._events.put(("test_error", (channel, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def _test_current(self) -> None:
        channel = "email" if self._active_tab == 0 else "dingtalk"
        self._test_channel(channel)

    def _poll_events(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind in {"test_done", "test_error"}:
                    channel, value = payload
                    self._test_running.discard(channel)
                    self.test_button.configure(state="normal")
                    channel_name = "邮件" if channel == "email" else "钉钉"
                    if kind == "test_error":
                        self.status_var.set(f"{channel_name}测试失败")
                        showerror(
                            f"{channel_name}测试失败",
                            str(value),
                            parent=self,
                        )
                        continue
                    results = value
                    lines = [
                        f"{item.channel}：{item.message}" for item in results
                    ]
                    failed = any(not item.success for item in results)
                    self.status_var.set(
                        f"{channel_name}测试完成"
                        if not failed
                        else f"{channel_name}测试失败"
                    )
                    (showerror if failed else showinfo)(
                        f"{channel_name}测试结果",
                        "\n".join(lines) or "通知渠道没有返回结果。",
                        parent=self,
                    )
        except queue.Empty:
            pass
        self.after(120, self._poll_events)


class NotificationSettingsDialog(_TabbedDialog):
    def __init__(
        self,
        parent: tk.Misc,
        settings: dict[str, Any],
    ) -> None:
        super().__init__(parent, "通知设置", "800x650")
        self.result: dict[str, Any] | None = None
        self._settings = notification_settings(settings)
        self._task_options: dict[str, dict[str, Any]] = {}
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._build_variables()
        self._build()
        self.after(50, self._show_centered)
        self.after(100, self._poll_events)
        self.after(150, self._refresh_tasks)

    def _build_variables(self) -> None:
        settings = self._settings
        backup = settings["backup"]
        windows = settings["windows_backup"]
        self.notify_success_var = tk.BooleanVar(
            value=settings["notify_on_success"]
        )
        self.notify_failure_var = tk.BooleanVar(
            value=settings["notify_on_failure"]
        )
        self.backup_email_var = tk.BooleanVar(
            value=backup["email_enabled"]
        )
        self.backup_dingtalk_var = tk.BooleanVar(
            value=backup["dingtalk_enabled"]
        )
        self.windows_enabled_var = tk.BooleanVar(value=windows["enabled"])
        self.windows_email_var = tk.BooleanVar(
            value=windows["email_enabled"]
        )
        self.windows_dingtalk_var = tk.BooleanVar(
            value=windows["dingtalk_enabled"]
        )
        self.system_backup_task_var = tk.StringVar()
        self.system_backup_status_var = tk.StringVar(
            value="正在读取 Windows 计划任务…"
        )
        saved_task = str(windows.get("task_name", ""))
        if saved_task and windows.get("task_path"):
            saved_task += f"  [{windows['task_path']}]"
        self._saved_task_label = saved_task
        self.system_backup_task_var.set(saved_task)
        self.status_var = tk.StringVar()

    def _build(self) -> None:
        outer = tk.Frame(
            self,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        outer.pack(fill="both", expand=True, padx=14, pady=14)
        header = tk.Frame(outer, bg=PANEL)
        header.pack(fill="x", padx=20, pady=(16, 10))
        title_box = tk.Frame(header, bg=PANEL)
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_box,
            text="通知设置",
            bg=PANEL,
            fg=TEXT,
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="分别设置程序备份和 Windows备份的通知方式。",
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(4, 0))
        ttk.Button(
            header,
            text="通知渠道设置",
            style="Secondary.TButton",
            command=self._open_channel_settings,
        ).pack(side="right", padx=(12, 0))

        triggers = tk.Frame(outer, bg=PANEL)
        triggers.pack(fill="x", padx=20, pady=(0, 8))
        self._checkbutton(
            triggers,
            text="成功时通知",
            variable=self.notify_success_var,
        ).pack(side="left")
        self._checkbutton(
            triggers,
            text="失败时通知",
            variable=self.notify_failure_var,
        ).pack(side="left", padx=(24, 0))

        footer = tk.Frame(outer, bg=PANEL)
        footer.pack(side="bottom", fill="x", padx=20, pady=(12, 16))
        ttk.Label(
            footer,
            textvariable=self.status_var,
            style="Muted.TLabel",
        ).pack(side="left")
        ttk.Button(
            footer,
            text="保存",
            style="Primary.TButton",
            command=self._save,
        ).pack(side="right")
        ttk.Button(
            footer,
            text="取消",
            style="Secondary.TButton",
            command=self.destroy,
        ).pack(side="right", padx=(0, 10))

        self._create_tabs(outer, ("程序备份", "Windows备份"))
        self._build_backup_tab(self._tab_pages[0])
        self._build_windows_tab(self._tab_pages[1])

    def _build_backup_tab(self, tab: tk.Frame) -> None:
        tk.Label(
            tab,
            text="数据自动备份助手",
            bg=PANEL,
            fg=TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w")
        tk.Label(
            tab,
            text="当前程序完成数据库、SQLite 或文件备份后发送通知。",
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(4, 14))
        channels = tk.Frame(tab, bg=PANEL)
        channels.pack(fill="x")
        self._checkbutton(
            channels,
            text="邮件通知",
            variable=self.backup_email_var,
        ).pack(side="left")
        self._checkbutton(
            channels,
            text="钉钉通知",
            variable=self.backup_dingtalk_var,
        ).pack(side="left", padx=(24, 0))
        tk.Label(
            tab,
            text=(
                "邮件服务器、收件人和钉钉机器人需要在"
                "“通知渠道设置”中配置。"
            ),
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(18, 0))

    def _build_windows_tab(self, tab: tk.Frame) -> None:
        tab.grid_columnconfigure(1, weight=1)
        self._checkbutton(
            tab,
            text="启用 Windows 系统备份通知",
            variable=self.windows_enabled_var,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        tk.Label(
            tab,
            text="通知方式",
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).grid(row=1, column=0, sticky="w", padx=(0, 14))
        channel_row = tk.Frame(tab, bg=PANEL)
        channel_row.grid(row=1, column=1, columnspan=2, sticky="w")
        self._checkbutton(
            channel_row,
            text="邮件通知",
            variable=self.windows_email_var,
        ).pack(side="left")
        self._checkbutton(
            channel_row,
            text="钉钉通知",
            variable=self.windows_dingtalk_var,
        ).pack(side="left", padx=(24, 0))

        tk.Label(
            tab,
            text="备份任务",
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).grid(row=2, column=0, sticky="w", padx=(0, 14), pady=(12, 0))
        self.task_combo = ttk.Combobox(
            tab,
            textvariable=self.system_backup_task_var,
            state="readonly",
        )
        self.task_combo.grid(row=2, column=1, sticky="ew", pady=(12, 0))
        self.task_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._update_task_detail(),
        )
        self.task_combo.bind(
            "<FocusIn>",
            lambda _event: self.after_idle(
                self.task_combo.selection_clear
            ),
            add="+",
        )
        self.task_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.after_idle(
                self.task_combo.selection_clear
            ),
            add="+",
        )
        ttk.Button(
            tab,
            text="刷新",
            style="Secondary.TButton",
            command=self._refresh_tasks,
        ).grid(row=2, column=2, padx=(8, 0), pady=(12, 0))
        ttk.Button(
            tab,
            text="绑定",
            style="Secondary.TButton",
            command=self._bind_selected_task,
        ).grid(row=2, column=3, padx=(8, 0), pady=(12, 0))
        status_holder = tk.Frame(tab, bg=PANEL, height=48)
        status_holder.grid(
            row=3,
            column=0,
            columnspan=4,
            sticky="nsew",
            pady=(8, 0),
        )
        status_holder.grid_propagate(False)
        tk.Label(
            status_holder,
            textvariable=self.system_backup_status_var,
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
            anchor="nw",
            justify="left",
            wraplength=620,
        ).pack(fill="both", expand=True)
        tab.grid_rowconfigure(3, minsize=48)
        ttk.Separator(tab, orient="horizontal").grid(
            row=4,
            column=0,
            columnspan=4,
            sticky="ew",
            pady=(8, 12),
        )
        tk.Label(
            tab,
            text=(
                "程序会定期读取所选任务的最近运行时间和退出代码。"
                "任务完成后，根据成功或失败状态发送通知。\n\n"
                "该功能要求程序正在运行。如果电脑未登录，需要另外配置"
                "Windows 任务计划事件触发。\n\n"
                "下拉列表会筛选名称、路径或命令中包含 Backup、wbadmin "
                "的计划任务，并自动探测 Windows Server Backup 系统计划。"
            ),
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
            justify="left",
            wraplength=620,
            anchor="nw",
        ).grid(row=5, column=0, columnspan=4, sticky="nw")

    def _open_channel_settings(self) -> None:
        dialog = NotificationChannelDialog(self, self._settings)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        self._settings.update(dialog.result)

    def _save(self) -> None:
        if not self.notify_success_var.get() and not self.notify_failure_var.get():
            showerror("设置有误", "成功和失败通知不能同时关闭。", parent=self)
            return
        if self.backup_email_var.get() and not channel_is_configured(
            self._settings,
            "email",
        ):
            showerror(
                "设置有误",
                "备份任务选择了邮件通知，请先在通知渠道设置中填写邮件参数。",
                parent=self,
            )
            return
        if self.backup_dingtalk_var.get() and not channel_is_configured(
            self._settings,
            "dingtalk",
        ):
            showerror(
                "设置有误",
                "备份任务选择了钉钉通知，请先在通知渠道设置中填写钉钉参数。",
                parent=self,
            )
            return
        windows = dict(self._settings["windows_backup"])
        windows["enabled"] = self.windows_enabled_var.get()
        windows["email_enabled"] = self.windows_email_var.get()
        windows["dingtalk_enabled"] = self.windows_dingtalk_var.get()
        selected = self._task_options.get(self.system_backup_task_var.get())
        if windows["enabled"]:
            if selected is None:
                showerror("设置有误", "请刷新并绑定 Windows备份任务。", parent=self)
                return
            if (
                windows["email_enabled"]
                and not channel_is_configured(self._settings, "email")
            ):
                showerror(
                    "设置有误",
                    "Windows备份选择了邮件通知，请先在通知渠道设置中填写邮件参数。",
                    parent=self,
                )
                return
            if (
                windows["dingtalk_enabled"]
                and not channel_is_configured(self._settings, "dingtalk")
            ):
                showerror(
                    "设置有误",
                    "Windows备份选择了钉钉通知，请先在通知渠道设置中填写钉钉参数。",
                    parent=self,
                )
                return
            changed = (
                str(selected.get("provider", "scheduled_task"))
                != str(windows.get("provider", "scheduled_task"))
                or str(selected.get("task_path", ""))
                != str(windows.get("task_path", ""))
                or str(selected.get("task_name", ""))
                != str(windows.get("task_name", ""))
            )
            windows["provider"] = str(
                selected.get("provider", "scheduled_task")
            )
            windows["task_path"] = str(selected.get("task_path", ""))
            windows["task_name"] = str(selected.get("task_name", ""))
            if changed:
                windows["last_observed_run_time"] = str(
                    selected.get("last_run_time", "")
                )
                windows["last_observed_result"] = str(
                    selected.get("last_task_result", "")
                )
                windows["last_observed_state"] = str(
                    selected.get("state", "")
                )
                windows["baseline_initialized"] = True
        self._settings["notify_on_success"] = self.notify_success_var.get()
        self._settings["notify_on_failure"] = self.notify_failure_var.get()
        self._settings["backup"] = {
            "email_enabled": self.backup_email_var.get(),
            "dingtalk_enabled": self.backup_dingtalk_var.get(),
        }
        self._settings["windows_backup"] = windows
        self.result = self._settings
        self.destroy()

    def _refresh_tasks(self) -> None:
        self.system_backup_status_var.set("正在读取 Windows 计划任务…")
        self.task_combo.configure(state="disabled")

        def worker() -> None:
            try:
                self._events.put(("tasks", list_system_backup_tasks()))
            except Exception as exc:  # noqa: BLE001
                self._events.put(("tasks_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "tasks":
                    self._apply_tasks(list(payload))
                elif kind == "tasks_error":
                    self.system_backup_status_var.set(f"读取失败：{payload}")
                    self.task_combo.configure(state="readonly")
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _apply_tasks(self, tasks: list[dict[str, Any]]) -> None:
        self._task_options.clear()
        options: list[str] = []
        for task in tasks:
            label = str(task.get("task_name", ""))
            if task.get("task_path"):
                label += f"  [{task['task_path']}]"
            self._task_options[label] = task
            options.append(label)
        if (
            self._saved_task_label
            and self._saved_task_label not in self._task_options
        ):
            options.insert(0, self._saved_task_label)
        self.task_combo.configure(values=options, state="readonly")
        current = self.system_backup_task_var.get()
        if current not in options:
            current = self._saved_task_label or (options[0] if options else "")
            self.system_backup_task_var.set(current)
        self.after_idle(self.task_combo.selection_clear)
        self.system_backup_status_var.set(
            f"已找到 {len(tasks)} 个候选备份任务。"
            if tasks
            else "未找到 Backup 或 wbadmin 相关计划任务。"
        )
        self._update_task_detail()

    def _bind_selected_task(self) -> None:
        task = self._task_options.get(self.system_backup_task_var.get())
        if task is None:
            showerror("无法绑定", "请先刷新并选择系统备份任务。", parent=self)
            return
        self.windows_enabled_var.set(True)
        self._update_task_detail()
        self.system_backup_status_var.set(
            "已绑定，点击“保存”后开始监听。\n"
            + self.system_backup_status_var.get()
        )

    def _update_task_detail(self) -> None:
        task = self._task_options.get(self.system_backup_task_var.get())
        if task is None:
            return
        last_run = str(task.get("last_run_time", "")) or "尚未运行"
        next_run = str(task.get("next_run_time", "")) or "未知"
        try:
            last_run = datetime.fromisoformat(last_run).strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            pass
        try:
            next_run = datetime.fromisoformat(next_run).strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            pass
        result = str(task.get("last_task_result", "")) or "未知"
        details = (
            f"候选任务 {len(self._task_options)} 个  |  "
            f"状态 {task.get('state', '未知')}  |  "
            f"下次备份 {next_run}  |  "
            f"上次运行 {last_run}  |  退出码 {result}"
        )
        if task.get("provider") == "windows_server_backup":
            target = str(task.get("backup_target", "")).strip()
            if target:
                details += f"\n备份目标：{target}"
        self.system_backup_status_var.set(details)
