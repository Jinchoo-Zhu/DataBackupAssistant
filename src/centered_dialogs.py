from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from windows_integration import center_window_over_parent

BG = "#F7F8FA"
PANEL = "#FFFFFF"
BORDER = "#D0D0D0"
TEXT = "#1A1A1A"
MUTED = "#666666"
ACCENT = "#0078D4"
ACCENT_SOFT = "#E8F0FE"
DANGER = "#D13438"


class MessageDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc | None,
        title: str,
        message: str,
        *,
        kind: str = "info",
        ask: bool = False,
    ) -> None:
        owner = parent.winfo_toplevel() if parent is not None else None
        super().__init__(owner)
        self.withdraw()
        self.result = False
        self._owner = owner
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        if owner is not None:
            self.transient(owner)

        outer = tk.Frame(
            self,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        content = tk.Frame(outer, bg=PANEL)
        content.pack(fill="both", expand=True, padx=18, pady=(18, 12))

        icon_color = DANGER if kind == "error" else ACCENT
        icon_bg = "#FDECEC" if kind == "error" else ACCENT_SOFT
        icon = tk.Canvas(
            content,
            width=34,
            height=34,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
        )
        icon.grid(row=0, column=0, sticky="n", padx=(0, 14))
        icon.create_oval(2, 2, 32, 32, fill=icon_bg, outline="")
        icon.create_text(
            17,
            17,
            text="!" if kind == "error" else "i",
            fill=icon_color,
            font=("Segoe UI Symbol", 14, "bold"),
        )

        text_box = tk.Frame(content, bg=PANEL)
        text_box.grid(row=0, column=1, sticky="nsew")
        content.grid_columnconfigure(1, weight=1)
        tk.Label(
            text_box,
            text=title,
            bg=PANEL,
            fg=TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            text_box,
            text=message,
            bg=PANEL,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
            justify="left",
            anchor="w",
            wraplength=420,
        ).pack(fill="x", pady=(8, 0))

        buttons = tk.Frame(outer, bg=PANEL)
        buttons.pack(fill="x", padx=18, pady=(0, 12))
        if ask:
            ttk.Button(
                buttons,
                text="取消",
                style="Secondary.TButton",
                command=self._cancel,
            ).pack(side="right")
            ttk.Button(
                buttons,
                text="确定",
                style="Primary.TButton",
                command=self._accept,
            ).pack(side="right", padx=(0, 8))
        else:
            ttk.Button(
                buttons,
                text="确定",
                style="Primary.TButton",
                command=self._accept,
            ).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.after(60, self._center_and_show)

    def _accept(self) -> None:
        self.result = True
        self.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.destroy()

    def _center_and_show(self) -> None:
        if not self.winfo_exists():
            return
        self.update_idletasks()
        width = 520
        height = max(190, self.winfo_reqheight())
        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        if self._owner is not None and self._owner.winfo_viewable():
            center_window_over_parent(self, self._owner)
        else:
            x = (self.winfo_screenwidth() - width) // 2
            y = (self.winfo_screenheight() - height) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            pass



def _show(
    title: str,
    message: str,
    *,
    parent: tk.Misc | None = None,
    kind: str,
) -> None:
    dialog = MessageDialog(
        parent,
        title,
        message,
        kind=kind,
        ask=False,
    )
    if parent is not None:
        parent.wait_window(dialog)
    else:
        dialog.wait_window()


def showinfo(
    title: str,
    message: str,
    *,
    parent: tk.Misc | None = None,
    **_kwargs: object,
) -> None:
    _show(title, message, parent=parent, kind="info")


def showerror(
    title: str,
    message: str,
    *,
    parent: tk.Misc | None = None,
    **_kwargs: object,
) -> None:
    _show(title, message, parent=parent, kind="error")


def askyesno(
    title: str,
    message: str,
    *,
    parent: tk.Misc | None = None,
    **_kwargs: object,
) -> bool:
    dialog = MessageDialog(
        parent,
        title,
        message,
        kind="info",
        ask=True,
    )
    if parent is not None:
        parent.wait_window(dialog)
    else:
        dialog.wait_window()
    return dialog.result
