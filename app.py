"""NFA Decompiler - drag-and-drop GUI.

Drop an .exe onto the window (or click Browse) and it prints the embedded
string as ``<name>----<token>``, ready to copy.

Drag-and-drop into the window uses ``tkinterdnd2`` when it's installed. If it
isn't, the app still works fully via the Browse button and by dropping a file
onto the .exe's icon in Explorer (Windows passes the path as an argument).
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import filedialog, ttk

from extractor import ExtractionError, extract_from_file

# --- optional real drag-and-drop -------------------------------------------
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
except Exception:  # pragma: no cover - depends on install
    _DND_AVAILABLE = False


APP_TITLE = "NFA Decompiler"
BG = "#1e1f2b"
BG_DROP = "#2a2c3d"
BG_DROP_HOVER = "#34374d"
ACCENT = "#7c6cff"
FG = "#e6e6f0"
FG_MUTED = "#9a9ab0"
OK = "#4ec98a"
ERR = "#ff6b6b"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.minsize(560, 380)
        self._center(560, 420)

        self.result_text = ""  # the current "name----token" string

        self._build_ui()

        if _DND_AVAILABLE:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)
            self.drop.dnd_bind("<<DragEnter>>", lambda e: self._hover(True))
            self.drop.dnd_bind("<<DragLeave>>", lambda e: self._hover(False))

    # -- layout --------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 20}

        header = tk.Label(
            self.root,
            text=APP_TITLE,
            bg=BG,
            fg=FG,
            font=("Segoe UI Semibold", 18),
        )
        header.pack(anchor="w", pady=(18, 0), **pad)

        subtitle = tk.Label(
            self.root,
            text="Drop an .exe below to pull its embedded string.",
            bg=BG,
            fg=FG_MUTED,
            font=("Segoe UI", 10),
        )
        subtitle.pack(anchor="w", pady=(2, 14), **pad)

        # drop zone
        self.drop = tk.Frame(self.root, bg=BG_DROP, height=130, cursor="hand2")
        self.drop.pack(fill="x", **pad)
        self.drop.pack_propagate(False)

        drop_hint = "Drag an .exe here" if _DND_AVAILABLE else "Click to choose an .exe"
        self.drop_label = tk.Label(
            self.drop,
            text=drop_hint + "\nor click to browse",
            bg=BG_DROP,
            fg=FG_MUTED,
            font=("Segoe UI", 12),
            justify="center",
        )
        self.drop_label.pack(expand=True)
        for w in (self.drop, self.drop_label):
            w.bind("<Button-1>", lambda e: self._browse())

        # result label
        tk.Label(
            self.root,
            text="Result",
            bg=BG,
            fg=FG_MUTED,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(16, 4), **pad)

        # result box
        box = tk.Frame(self.root, bg=BG)
        box.pack(fill="both", expand=True, **pad)

        self.output = tk.Text(
            box,
            height=4,
            wrap="char",
            bg=BG_DROP,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=("Consolas", 10),
            padx=10,
            pady=8,
        )
        self.output.pack(fill="both", expand=True, side="left")
        self.output.configure(state="disabled")

        # buttons + status
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", pady=(10, 16), **pad)

        self.copy_btn = tk.Button(
            bar,
            text="Copy",
            command=self._copy,
            bg=ACCENT,
            fg="white",
            activebackground="#6a5be0",
            activeforeground="white",
            relief="flat",
            font=("Segoe UI Semibold", 10),
            padx=18,
            pady=6,
            cursor="hand2",
            state="disabled",
        )
        self.copy_btn.pack(side="left")

        browse_btn = tk.Button(
            bar,
            text="Browse…",
            command=self._browse,
            bg=BG_DROP,
            fg=FG,
            activebackground=BG_DROP_HOVER,
            activeforeground=FG,
            relief="flat",
            font=("Segoe UI", 10),
            padx=14,
            pady=6,
            cursor="hand2",
        )
        browse_btn.pack(side="left", padx=(8, 0))

        self.status = tk.Label(
            bar, text="", bg=BG, fg=FG_MUTED, font=("Segoe UI", 9)
        )
        self.status.pack(side="right")

    # -- behaviour -----------------------------------------------------------
    def _hover(self, on: bool):
        color = BG_DROP_HOVER if on else BG_DROP
        self.drop.configure(bg=color)
        self.drop_label.configure(bg=color)

    def _on_drop(self, event):
        self._hover(False)
        paths = self.root.tk.splitlist(event.data)
        if paths:
            self.process(paths[0])

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Choose an executable",
            filetypes=[("Executables", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.process(path)

    def process(self, path: str):
        name = os.path.basename(path)
        try:
            result = extract_from_file(path)
        except ExtractionError as exc:
            self._show_error(str(exc))
            return
        except (OSError, PermissionError) as exc:
            self._show_error(f"Couldn't read file: {exc}")
            return
        except Exception as exc:  # pragma: no cover - safety net
            self._show_error(f"Unexpected error: {exc}")
            return

        self._set_output(result["combined"])
        self._copy(announce=False)

        note = ""
        if len(result["all_tokens"]) > 1:
            note = f"  ({len(result['all_tokens'])} tokens found, showing first)"
        self._set_status(f"Extracted from {name} – copied to clipboard{note}", OK)

    # -- output helpers ------------------------------------------------------
    def _set_output(self, text: str):
        self.result_text = text
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")
        self.copy_btn.configure(state="normal" if text else "disabled")

    def _show_error(self, message: str):
        self._set_output("")
        self._set_status(message, ERR)

    def _set_status(self, text: str, color: str = FG_MUTED):
        self.status.configure(text=text, fg=color)

    def _copy(self, announce: bool = True):
        if not self.result_text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.result_text)
        if announce:
            self._set_status("Copied to clipboard", OK)

    # -- misc ----------------------------------------------------------------
    def _center(self, w: int, h: int):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 3
        self.root.geometry(f"{w}x{h}+{x}+{y}")


def main():
    root = TkinterDnD.Tk() if _DND_AVAILABLE else tk.Tk()
    app = App(root)

    # If launched by dropping a file onto the .exe icon, process it immediately.
    for arg in sys.argv[1:]:
        if os.path.isfile(arg):
            root.after(150, lambda p=arg: app.process(p))
            break

    root.mainloop()


if __name__ == "__main__":
    main()
