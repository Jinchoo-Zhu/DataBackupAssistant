from __future__ import annotations

import datetime as dt
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, ttk
from typing import ClassVar

from backup_engine import BackupError, list_mysql_databases, test_sqlite
from centered_dialogs import askyesno, showerror, showinfo
from windows_integration import center_window_over_parent


class _CenteredMessageBox:
    showinfo = staticmethod(showinfo)
    showerror = staticmethod(showerror)
    askyesno = staticmethod(askyesno)


messagebox = _CenteredMessageBox()


BG = "#F7F8FA"
PANEL = "#FFFFFF"
PANEL_ALT = "#FAFAFA"
BORDER = "#D0D0D0"
TEXT = "#1A1A1A"
MUTED = "#666666"
ACCENT = "#0078D4"
ACCENT_DARK = "#106EBE"
ACCENT_SOFT = "#E8F0FE"
def _resource_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent.parent / "assets"
    return base / name


class ImageCheckbutton(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        text: str,
        variable: tk.BooleanVar,
        checked_image: tk.PhotoImage,
        unchecked_image: tk.PhotoImage,
    ) -> None:
        super().__init__(parent, bg=PANEL, cursor="hand2", takefocus=True)
        self.variable = variable
        self._checked_image = checked_image
        self._unchecked_image = unchecked_image
        self._image_label = tk.Label(
            self,
            image=unchecked_image,
            bg=PANEL,
            bd=0,
            cursor="hand2",
        )
        self._image_label.pack(side="left")
        self._text_label = tk.Label(
            self,
            text=text,
            bg=PANEL,
            fg=TEXT,
            font=("Microsoft YaHei UI", 9),
            bd=0,
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


class SourceKindRadio(tk.Frame):
    """无背景色的文件/文件夹单选选项。"""

    def __init__(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
    ) -> None:
        super().__init__(parent, bg=PANEL, bd=0, highlightthickness=0)
        self.variable = variable
        self._choices: dict[str, tuple[tk.Canvas, tk.Label]] = {}
        for index, (value, text) in enumerate(
            (("file", "文件"), ("folder", "文件夹"))
        ):
            option = tk.Frame(
                self,
                bg=PANEL,
                cursor="hand2",
                takefocus=False,
            )
            option.pack(side="left", padx=(0, 18 if index == 0 else 0))
            circle = tk.Canvas(
                option,
                width=18,
                height=18,
                bg=PANEL,
                bd=0,
                highlightthickness=0,
                cursor="hand2",
            )
            circle.pack(side="left")
            label = tk.Label(
                option,
                text=text,
                bg=PANEL,
                fg=TEXT,
                font=("Microsoft YaHei UI", 9),
                bd=0,
                padx=0,
                pady=0,
                cursor="hand2",
            )
            label.pack(side="left", padx=(4, 0))
            for widget in (option, circle, label):
                widget.bind(
                    "<Button-1>",
                    lambda _event, selected=value: self._select(selected),
                )
            self._choices[value] = (circle, label)
        self.variable.trace_add("write", self._draw)
        self._draw()

    def _select(self, value: str) -> str:
        self.variable.set(value)
        self._draw()
        return "break"

    def _draw(self, *_args: object) -> None:
        selected_value = self.variable.get()
        for value, (circle, label) in self._choices.items():
            selected = value == selected_value
            circle.delete("all")
            circle.create_oval(
                2,
                2,
                15,
                15,
                outline=ACCENT if selected else "#B8B8B8",
                width=2 if selected else 1,
            )
            if selected:
                circle.create_oval(
                    6,
                    6,
                    11,
                    11,
                    fill=ACCENT,
                    outline="",
                )
            label.configure(
                fg=ACCENT_DARK if selected else TEXT,
                font=(
                    "Microsoft YaHei UI",
                    9,
                    "bold" if selected else "normal",
                ),
            )


class BackupJobDialog(tk.Toplevel):
    TYPE_LABELS: ClassVar[dict[str, str]] = {
        "mysql": "MySQL / MariaDB",
        "sqlite": "SQLite",
        "files": "文件或文件夹",
    }
    LABEL_FONT = ("Microsoft YaHei UI", 9)
    LABEL_PADDING_X = (8, 14)
    ALIGNED_LABELS = (
        "任务名称",
        "任务类型",
        "服务器地址",
        "用户名",
        "密码",
        "数据库",
        "数据库文件",
        "来源类型",
        "备份来源",
    )

    def __init__(
        self,
        parent: tk.Misc,
        job: dict | None = None,
        *,
        submit_validator: Callable[[list[dict]], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self._shown = False
        self.result: dict | None = None
        self.results: list[dict] | None = None
        self.job = dict(job or {})
        self._editing = bool(self.job.get("id") or self.job.get("name"))
        self._owner = parent
        self._submit_validator = submit_validator
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.checkbox_checked_image: tk.PhotoImage | None = None
        self.checkbox_unchecked_image: tk.PhotoImage | None = None
        self.checkbox_checked_cell_image: tk.PhotoImage | None = None
        self.checkbox_unchecked_cell_image: tk.PhotoImage | None = None
        try:
            self.checkbox_checked_image = tk.PhotoImage(
                file=str(_resource_path("checkbox-checked.png"))
            )
            self.checkbox_unchecked_image = tk.PhotoImage(
                file=str(_resource_path("checkbox-unchecked.png"))
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
        self.saved_mysql_databases = [
            str(self.job.get("database", ""))
        ] if self.job.get("database") else []
        self.checked_mysql_databases = set(self.saved_mysql_databases)

        self.title("编辑备份任务" if self._editing else "添加备份任务")
        self.configure(bg=BG)
        self.geometry("780x400")
        self.minsize(720, 360)
        self.transient(parent)

        self.name_var = tk.StringVar(value=str(self.job.get("name", "")))
        self.type_var = tk.StringVar(
            value=self.TYPE_LABELS.get(
                str(self.job.get("job_type", "mysql")),
                self.TYPE_LABELS["mysql"],
            )
        )
        self.host_var = tk.StringVar(
            value=str(self.job.get("host", "127.0.0.1"))
        )
        self.port_var = tk.StringVar(value=str(self.job.get("port", 3306)))
        self.user_var = tk.StringVar(value=str(self.job.get("user", "root")))
        self.password_var = tk.StringVar(
            value=str(self.job.get("password", ""))
        )
        self.sqlite_path_var = tk.StringVar(
            value=str(self.job.get("sqlite_path", ""))
        )
        self.file_source_var = tk.StringVar(
            value=str(self.job.get("file_source", ""))
        )
        existing_source = Path(
            str(self.job.get("file_source", ""))
        )
        self.file_source_kind_var = tk.StringVar(
            value="folder"
            if existing_source.is_dir()
            else "file"
        )
        self.output_dir_var = tk.StringVar(
            value=str(self.job.get("output_dir", ""))
        )
        self.backup_time_var = tk.StringVar(
            value=str(self.job.get("backup_time", "02:00"))
        )
        self.retention_days_var = tk.StringVar(
            value=str(self.job.get("retention_days", 30))
        )
        self.skip_unreadable_var = tk.BooleanVar(
            value=bool(self.job.get("file_skip_unreadable", True))
        )
        self.status_var = tk.StringVar(value="")

        self._build()
        self._on_type_change()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.after(100, self._poll_events)
        self.after(60, self._resize_to_content)

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
            text="备份任务",
            bg=PANEL,
            fg=TEXT,
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="每个任务独立选择来源、保存目录、执行时间和保留天数。",
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(4, 0))

        form = tk.Frame(outer, bg=PANEL)
        form.pack(fill="both", expand=True, padx=20)
        form.grid_columnconfigure(1, weight=1)

        self._label(form, "任务名称", 0)
        ttk.Entry(form, textvariable=self.name_var).grid(
            row=0, column=1, sticky="ew", pady=(0, 8)
        )
        self._label(form, "任务类型", 1)
        self.type_combo = ttk.Combobox(
            form,
            textvariable=self.type_var,
            values=tuple(self.TYPE_LABELS.values()),
            state="readonly",
        )
        self.type_combo.grid(row=1, column=1, sticky="ew", pady=(0, 10))
        self.type_combo.bind("<<ComboboxSelected>>", self._on_type_change)
        self.type_combo.bind(
            "<FocusIn>",
            lambda _event: self.after_idle(
                self.type_combo.selection_clear
            ),
            add="+",
        )
        self.type_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.after_idle(
                self.type_combo.selection_clear
            ),
            add="+",
        )

        source_host = tk.Frame(form, bg=PANEL)
        source_host.grid(row=2, column=0, columnspan=2, sticky="nsew")
        source_host.grid_rowconfigure(0, weight=1)
        source_host.grid_columnconfigure(0, weight=1)

        self.mysql_page = tk.Frame(source_host, bg=PANEL)
        self.mysql_page.grid(row=0, column=0, sticky="nsew")
        self.mysql_page.grid_columnconfigure(1, weight=1)
        self._build_mysql_page()

        self.sqlite_page = tk.Frame(source_host, bg=PANEL)
        self.sqlite_page.grid(row=0, column=0, sticky="nsew")
        self.sqlite_page.grid_columnconfigure(1, weight=1)
        self._build_sqlite_page()

        self.files_page = tk.Frame(source_host, bg=PANEL)
        self.files_page.grid(row=0, column=0, sticky="nsew")
        self.files_page.grid_columnconfigure(1, weight=1)
        self._build_files_page()
        self._align_label_columns(
            form,
            self.mysql_page,
            self.sqlite_page,
            self.files_page,
        )

        plan = tk.Frame(
            form,
            bg=PANEL_ALT,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        plan.grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(10, 0),
        )
        plan.grid_columnconfigure(1, weight=1)
        self._label(plan, "保存目录", 0, row_pady=(6, 6))
        output_row = tk.Frame(plan, bg=PANEL_ALT)
        output_row.grid(row=0, column=1, sticky="ew", pady=(6, 6), padx=(0, 8))
        output_row.grid_columnconfigure(0, weight=1)
        ttk.Entry(output_row, textvariable=self.output_dir_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            output_row,
            text="浏览…",
            style="Secondary.TButton",
            command=self._browse_output,
        ).grid(row=0, column=1, padx=(8, 0))

        self._label(plan, "每天执行时间", 1, row_pady=6)
        time_row = tk.Frame(plan, bg=PANEL_ALT)
        time_row.grid(row=1, column=1, sticky="w", pady=(0, 6), padx=(0, 8))
        ttk.Combobox(
            time_row,
            textvariable=self.backup_time_var,
            values=[
                f"{hour:02d}:{minute:02d}"
                for hour in range(24)
                for minute in (0, 15, 30, 45)
            ],
            width=10,
        ).pack(side="left")
        tk.Label(
            time_row,
            text="  24 小时制",
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left")

        self._label(plan, "保留天数", 2, row_pady=8)
        keep_row = tk.Frame(plan, bg=PANEL_ALT)
        keep_row.grid(row=2, column=1, sticky="w", pady=(0, 8), padx=(0, 8))
        ttk.Entry(
            keep_row,
            textvariable=self.retention_days_var,
            width=10,
        ).pack(side="left")
        tk.Label(
            keep_row,
            text="  0 表示永久保留",
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left")

        tk.Frame(outer, bg=BORDER, height=1).pack(
            fill="x",
            padx=20,
            pady=(12, 0),
        )
        footer = tk.Frame(outer, bg=PANEL)
        footer.pack(fill="x", padx=20, pady=(12, 18))
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

    def _build_mysql_page(self) -> None:
        self._label(self.mysql_page, "服务器地址", 0)
        row = tk.Frame(self.mysql_page, bg=PANEL)
        row.grid(row=0, column=1, sticky="ew", pady=(0, 7))
        row.grid_columnconfigure(0, weight=1)
        ttk.Entry(row, textvariable=self.host_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(row, text="端口", style="Muted.TLabel").grid(
            row=0, column=1, padx=(10, 6)
        )
        ttk.Entry(row, textvariable=self.port_var, width=9).grid(
            row=0, column=2
        )
        self._label(self.mysql_page, "用户名", 1)
        ttk.Entry(self.mysql_page, textvariable=self.user_var).grid(
            row=1, column=1, sticky="ew", pady=(0, 7)
        )
        self._label(self.mysql_page, "密码", 2)
        ttk.Entry(
            self.mysql_page,
            textvariable=self.password_var,
            show="●",
        ).grid(row=2, column=1, sticky="ew", pady=(0, 7))
        self._label(self.mysql_page, "数据库", 3)
        db_frame = tk.Frame(
            self.mysql_page,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        db_frame.grid(row=3, column=1, sticky="ew", pady=(0, 4))
        db_frame.grid_rowconfigure(0, weight=1)
        db_frame.grid_columnconfigure(0, weight=1)
        db_scroll = ttk.Scrollbar(db_frame, orient="vertical")
        db_scroll.grid(row=0, column=1, sticky="ns")
        self.database_tree = ttk.Treeview(
            db_frame,
            columns=("database",),
            show=("tree", "headings"),
            height=3,
            selectmode="extended",
        )
        self.database_tree.heading(
            "#0",
            text="选择",
            anchor="center",
            command=self._toggle_all_mysql_databases,
        )
        self.database_tree.column(
            "#0",
            width=76,
            minwidth=76,
            stretch=False,
            anchor="center",
        )
        self.database_tree.heading(
            "database",
            text="数据库名称",
            anchor="w",
        )
        self.database_tree.column(
            "database",
            width=520,
            minwidth=140,
            stretch=True,
            anchor="w",
        )
        self.database_tree.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=3,
            pady=3,
        )
        db_scroll.config(command=self.database_tree.yview)
        self._bind_database_scrollbar(self.database_tree, db_scroll)
        self.database_tree.bind(
            "<ButtonPress-1>",
            self._block_database_checkbox_resize,
            add="+",
        )
        self.database_tree.bind(
            "<Motion>",
            self._update_database_resize_cursor,
            add="+",
        )
        self.database_tree.bind(
            "<Leave>",
            lambda _event: self.database_tree.configure(cursor=""),
            add="+",
        )
        self.database_tree.bind(
            "<ButtonRelease-1>",
            self._handle_mysql_database_click,
        )
        self._populate_mysql_databases(self.saved_mysql_databases)
        self.load_button = ttk.Button(
            self.mysql_page,
            text="连接并读取",
            style="Secondary.TButton",
            command=self._load_mysql_databases,
        )
        self.load_button.grid(row=4, column=1, sticky="w", pady=(0, 3))
        ttk.Label(
            self.mysql_page,
            text="勾选要备份的数据库，保存后会为每个勾选项生成独立任务。",
            style="Muted.TLabel",
        ).grid(row=5, column=1, sticky="w")

    def _build_sqlite_page(self) -> None:
        self._label(self.sqlite_page, "数据库文件", 0)
        row = tk.Frame(self.sqlite_page, bg=PANEL)
        row.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        row.grid_columnconfigure(0, weight=1)
        ttk.Entry(row, textvariable=self.sqlite_path_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            row,
            text="浏览…",
            style="Secondary.TButton",
            command=self._browse_sqlite,
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(
            self.sqlite_page,
            text="备份完成后会压缩为 ZIP。",
            style="Muted.TLabel",
        ).grid(row=1, column=1, sticky="w")

    def _build_files_page(self) -> None:
        self._label(self.files_page, "来源类型", 0)
        kind_row = tk.Frame(self.files_page, bg=PANEL)
        kind_row.grid(row=0, column=1, sticky="w", pady=(0, 8))
        self.source_kind_radio = SourceKindRadio(
            kind_row,
            self.file_source_kind_var,
        )
        self.source_kind_radio.pack(side="left")
        self.file_source_kind_var.trace_add(
            "write",
            self._source_kind_changed,
        )

        self._label(self.files_page, "备份来源", 1)
        source_row = tk.Frame(self.files_page, bg=PANEL)
        source_row.grid(row=1, column=1, sticky="ew", pady=(0, 8))
        source_row.grid_columnconfigure(0, weight=1)
        ttk.Entry(source_row, textvariable=self.file_source_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            source_row,
            text="浏览…",
            style="Secondary.TButton",
            command=self._browse_source,
        ).grid(row=0, column=1, padx=(6, 0))
        self._create_checkbutton(
            self.files_page,
            text="无法读取某个文件时跳过并记录",
            variable=self.skip_unreadable_var,
        ).grid(row=2, column=1, sticky="w", pady=(0, 8))
        ttk.Label(
            self.files_page,
            text="文件或整个文件夹会压缩为 ZIP 备份。",
            style="Muted.TLabel",
        ).grid(row=3, column=1, sticky="w")

    def _label(
        self,
        parent: tk.Misc,
        text: str,
        row: int,
        row_pady: int | tuple[int, int] = 8,
    ) -> None:
        label_pady = (
            row_pady
            if isinstance(row_pady, tuple)
            else (0, row_pady)
        )
        tk.Label(
            parent,
            text=text,
            bg=parent.cget("bg"),
            fg=MUTED,
            font=self.LABEL_FONT,
        ).grid(
            row=row,
            column=0,
            sticky="w",
            padx=self.LABEL_PADDING_X,
            pady=label_pady,
        )

    def _align_label_columns(self, *parents: tk.Misc) -> None:
        labels = [
            child
            for parent in parents
            for child in parent.winfo_children()
            if isinstance(child, tk.Label)
            and str(child.cget("text")) in self.ALIGNED_LABELS
        ]
        if not labels:
            return

        column_width = max(label.winfo_reqwidth() for label in labels)
        column_width += sum(self.LABEL_PADDING_X)
        for parent in parents:
            parent.grid_columnconfigure(0, minsize=column_width)

    @staticmethod
    def _tree_checkbox_image(image: tk.PhotoImage) -> tk.PhotoImage:
        # Treeview reserves indentation at the left of #0. Offset the image
        # so the visible indicator is centered inside the selection column.
        padded = tk.PhotoImage(width=image.width() + 10, height=image.height())
        padded.tk.call(padded.name, "copy", image.name, "-to", 10, 0)
        return padded

    def _set_mysql_database_checkbox(
        self,
        item: str,
        checked: bool,
    ) -> None:
        image = (
            self.checkbox_checked_cell_image
            if checked
            else self.checkbox_unchecked_cell_image
        )
        if image is not None:
            self.database_tree.item(item, image=image, text="")
        else:
            self.database_tree.item(item, text="✓" if checked else "□")

    def _handle_mysql_database_click(
        self,
        event: tk.Event,
    ) -> None:
        if (
            self.database_tree.identify_region(event.x, event.y)
            not in {"tree", "cell"}
            or self.database_tree.identify_column(event.x) != "#0"
        ):
            return
        item = self.database_tree.identify_row(event.y)
        if not item:
            return
        name = self.database_tree.set(item, "database")
        if name in self.checked_mysql_databases:
            self.checked_mysql_databases.remove(name)
        else:
            self.checked_mysql_databases.add(name)
        self._set_mysql_database_checkbox(
            item,
            name in self.checked_mysql_databases,
        )

    def _toggle_all_mysql_databases(self) -> None:
        items = self.database_tree.get_children()
        if not items:
            return
        names = {
            self.database_tree.set(item, "database")
            for item in items
        }
        select_all = not names.issubset(self.checked_mysql_databases)
        if select_all:
            self.checked_mysql_databases.update(names)
        else:
            self.checked_mysql_databases.difference_update(names)
        for item in items:
            name = self.database_tree.set(item, "database")
            self._set_mysql_database_checkbox(
                item,
                name in self.checked_mysql_databases,
            )

    def _block_database_checkbox_resize(self, event: tk.Event) -> str | None:
        if (
            self.database_tree.identify_region(event.x, event.y)
            == "separator"
            and self.database_tree.identify_column(event.x) in {"#0", "#1"}
        ):
            return "break"
        return None

    def _update_database_resize_cursor(self, event: tk.Event) -> None:
        locked = (
            self.database_tree.identify_region(event.x, event.y)
            == "separator"
            and self.database_tree.identify_column(event.x) in {"#0", "#1"}
        )
        self.database_tree.configure(cursor="no" if locked else "")

    def _bind_database_scrollbar(
        self,
        tree: ttk.Treeview,
        scrollbar: ttk.Scrollbar,
    ) -> None:
        visible = True

        def sync(first: str, last: str) -> None:
            nonlocal visible
            scrollbar.set(first, last)
            needs_scrollbar = bool(tree.get_children()) and not (
                float(first) <= 0.0 and float(last) >= 1.0
            )
            if needs_scrollbar == visible:
                return
            if needs_scrollbar:
                scrollbar.grid()
            else:
                scrollbar.grid_remove()
            visible = needs_scrollbar

        self._database_scroll_sync = sync
        tree.configure(yscrollcommand=sync)
        tree.bind(
            "<Configure>",
            lambda _event: self.after_idle(self._sync_database_scrollbar),
            add="+",
        )

    def _sync_database_scrollbar(self) -> None:
        if not self.winfo_exists():
            return
        first, last = self.database_tree.yview()
        self._database_scroll_sync(str(first), str(last))

    def _create_checkbutton(
        self,
        parent: tk.Misc,
        *,
        text: str,
        variable: tk.BooleanVar,
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

    def _selected_type(self) -> str:
        label = self.type_var.get()
        for key, value in self.TYPE_LABELS.items():
            if value == label:
                return key
        return "mysql"

    def _on_type_change(self, _event: tk.Event | None = None) -> None:
        job_type = self._selected_type()
        default_names = {
            "mysql": "MySQL 数据库备份",
            "sqlite": "SQLite 备份",
            "files": "文件备份",
        }
        self.mysql_page.grid_remove()
        self.sqlite_page.grid_remove()
        self.files_page.grid_remove()
        if job_type == "mysql":
            self.mysql_page.grid()
        elif job_type == "sqlite":
            self.sqlite_page.grid()
        else:
            self.files_page.grid()
        if _event is not None or (
            not self._editing and not self.name_var.get().strip()
        ):
            self.name_var.set(default_names[job_type])
        self.after(60, self._resize_to_content)

    def _resize_to_content(self) -> None:
        if not self.winfo_exists():
            return
        self.update_idletasks()
        height = max(360, self.winfo_reqheight())
        self.geometry(f"780x{height}")
        self.update_idletasks()
        self._center_on_parent()
        if not self._shown:
            self.deiconify()
            self.lift()
            self.focus_force()
            try:
                self.grab_set()
            except tk.TclError:
                pass
            self._shown = True

    def _center_on_parent(self) -> None:
        if self.winfo_exists():
            center_window_over_parent(self, self._owner)

    def _load_mysql_databases(self) -> None:
        try:
            port = int(self.port_var.get())
            if not 1 <= port <= 65535:
                raise ValueError("端口必须在 1 到 65535 之间")
        except ValueError as exc:
            messagebox.showerror("设置有误", str(exc), parent=self)
            return
        config = {
            "mysql_host": self.host_var.get().strip(),
            "mysql_port": port,
            "mysql_user": self.user_var.get().strip(),
            "mysql_password": self.password_var.get(),
        }
        self.load_button.config(state="disabled")
        self.status_var.set("正在连接并读取数据库…")
        selected = self._selected_mysql_databases()

        def worker() -> None:
            try:
                names = list_mysql_databases(config)
                self.events.put(("databases", (names, selected)))
            except Exception as exc:  # noqa: BLE001
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "databases":
                    names, _selected = payload
                    names = list(names)
                    self._populate_mysql_databases(names)
                    self.status_var.set(
                        f"连接成功，共读取到 {len(names)} 个数据库"
                    )
                    self.load_button.config(state="normal")
                elif kind == "error":
                    self.status_var.set("连接失败")
                    self.load_button.config(state="normal")
                    messagebox.showerror("连接失败", str(payload), parent=self)
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _selected_mysql_databases(self) -> list[str]:
        selected: list[str] = []
        for item in self.database_tree.get_children():
            name = self.database_tree.set(item, "database")
            if name in self.checked_mysql_databases:
                selected.append(name)
        return selected

    def _populate_mysql_databases(self, names: list[str]) -> None:
        selected = set(self.checked_mysql_databases)
        all_names = list(dict.fromkeys([*names, *selected]))
        for item in self.database_tree.get_children():
            self.database_tree.delete(item)
        for index, name in enumerate(all_names, 1):
            item = f"database-{index}"
            self.database_tree.insert(
                "",
                "end",
                iid=item,
                values=(name,),
                tags=("row_odd" if index % 2 == 0 else "row_even",),
            )
            self._set_mysql_database_checkbox(
                item,
                name in self.checked_mysql_databases,
            )
        self.database_tree.tag_configure("row_even", background=PANEL)
        self.database_tree.tag_configure("row_odd", background=PANEL_ALT)
        self.after_idle(self._sync_database_scrollbar)

    def _browse_sqlite(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="选择 SQLite 数据库",
            filetypes=[
                ("SQLite 数据库", "*.db *.sqlite *.sqlite3 *.db3"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.sqlite_path_var.set(path)
            if not self.name_var.get() or self.name_var.get() == "SQLite 备份":
                self.name_var.set(f"{Path(path).stem} SQLite 备份")

    def _source_kind_changed(self, *_args: object) -> None:
        self.file_source_var.set("")

    def _browse_source(self) -> None:
        if self.file_source_kind_var.get() == "folder":
            path = filedialog.askdirectory(
                parent=self,
                title="选择需要备份的文件夹",
                mustexist=True,
            )
            if path:
                self.file_source_var.set(path)
                if not self.name_var.get() or self.name_var.get() == "文件备份":
                    self.name_var.set(f"{Path(path).name} 文件夹备份")
            return

        path = filedialog.askopenfilename(
            parent=self,
            title="选择需要备份的文件",
            filetypes=[("所有文件", "*.*")],
        )
        if path:
            self.file_source_var.set(path)
            if not self.name_var.get() or self.name_var.get() == "文件备份":
                self.name_var.set(f"{Path(path).stem} 文件备份")

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(
            parent=self,
            title="选择该任务的备份保存目录",
            initialdir=self.output_dir_var.get() or str(Path.home()),
        )
        if path:
            self.output_dir_var.set(path)

    def _show_save_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self)
        if not self.winfo_exists():
            return
        self.deiconify()
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _save(self) -> None:
        self.result = None
        self.results = None
        name = self.name_var.get().strip()
        output_dir = self.output_dir_var.get().strip()
        if not name:
            self._show_save_error("设置有误", "请填写任务名称。")
            return
        if not output_dir:
            self._show_save_error("设置有误", "请选择该任务的保存目录。")
            return
        try:
            parsed_time = dt.datetime.strptime(self.backup_time_var.get(), "%H:%M")
            retention_days = int(self.retention_days_var.get())
            if not 0 <= retention_days <= 3650:
                raise ValueError("保留天数必须在 0 到 3650 之间")
        except ValueError as exc:
            self._show_save_error("设置有误", str(exc))
            return

        result = dict(self.job)
        result.update(
            {
                "name": name,
                "output_dir": output_dir,
                "backup_time": parsed_time.strftime("%H:%M"),
                "retention_days": retention_days,
            }
        )
        job_type = self._selected_type()
        result["job_type"] = job_type
        if job_type == "mysql":
            try:
                port = int(self.port_var.get())
                if not self.host_var.get().strip():
                    raise ValueError("请填写服务器地址")
                if not self.user_var.get().strip():
                    raise ValueError("请填写用户名")
                databases = self._selected_mysql_databases()
                if not databases:
                    raise ValueError("请先读取并选择至少一个数据库")
            except ValueError as exc:
                self._show_save_error("设置有误", str(exc))
                return
            result.update(
                {
                    "host": self.host_var.get().strip(),
                    "port": port,
                    "user": self.user_var.get().strip(),
                    "password": self.password_var.get(),
                }
            )
            self.results = []
            for database in databases:
                job = dict(result)
                job["database"] = database
                if len(databases) > 1:
                    job["name"] = f"{name} - {database}"
                self.results.append(job)
        elif job_type == "sqlite":
            sqlite_path = self.sqlite_path_var.get().strip()
            if not Path(sqlite_path).is_file():
                self._show_save_error(
                    "设置有误",
                    "请选择有效的 SQLite 数据库文件。",
                )
                return
            try:
                test_sqlite(sqlite_path)
            except BackupError as exc:
                self._show_save_error("SQLite 检查失败", str(exc))
                return
            result["sqlite_path"] = sqlite_path
            self.results = [result]
        else:
            source = self.file_source_var.get().strip()
            source_path = Path(source)
            if not source or not source_path.exists():
                self._show_save_error(
                    "设置有误",
                    "请选择有效的文件或文件夹。",
                )
                return
            if (
                self.file_source_kind_var.get() == "folder"
                and not source_path.is_dir()
            ):
                self._show_save_error(
                    "设置有误",
                    "来源类型是文件夹，但选择的路径不是文件夹。",
                )
                return
            if (
                self.file_source_kind_var.get() == "file"
                and not source_path.is_file()
            ):
                self._show_save_error(
                    "设置有误",
                    "来源类型是文件，但选择的路径不是文件。",
                )
                return
            result["file_source"] = source
            result["file_source_kind"] = self.file_source_kind_var.get()
            result["file_skip_unreadable"] = self.skip_unreadable_var.get()
            self.results = [result]

        if self._submit_validator is not None and self.results is not None:
            validation_error = self._submit_validator(self.results)
            if validation_error:
                self.result = None
                self.results = None
                self._show_save_error("无法保存", validation_error)
                return
        self.result = self.results[0] if self.results else None
        self.destroy()
