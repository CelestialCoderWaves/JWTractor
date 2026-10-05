"""JWTractor - drag-and-drop GUI.

Drop an .exe onto the window (or click Browse) and it shows the embedded string
as ``<name>----<token>``, decodes the token's claims for a quick sanity check,
and copies the string to your clipboard.

Drag-and-drop into the window uses ``tkinterdnd2`` when it's installed. If it
isn't, the app still works fully via the Browse button and by dropping a file
onto the .exe's icon in Explorer (Windows passes the path as an argument).

The UI is drawn with a handful of small Canvas-based widgets (rounded buttons,
cards and a drop zone) so it looks consistent instead of relying on the
platform's native, boxy Tk controls.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog

from extractor import (
    ExtractionError,
    decode_token,
    extract_from_file,
    parse_token_input,
    summarize_claims,
)
from store import Store, account_combined, account_label
from steam_login import LoginCancelled, SteamLoginError, login_account, validate_account_name, validate_token

# --- optional real drag-and-drop -------------------------------------------
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
except Exception:  # pragma: no cover - depends on install
    _DND_AVAILABLE = False


APP_TITLE = "JWTractor"

# --- palette ---------------------------------------------------------------
# A cohesive dark theme with real depth: a deep window, slightly lifted cards,
# subtle 1px borders and one violet accent used sparingly.
BG = "#0e1018"          # window background
SURFACE = "#161a24"     # cards / panels
SURFACE_HI = "#1e2330"  # hover / raised
FIELD = "#11141d"       # inset field (the result box)
BORDER = "#272d3b"      # hairline borders
BORDER_HI = "#39415a"   # hover border
ACCENT = "#7c6cff"      # brand violet
ACCENT_HI = "#8f82ff"   # accent hover
ACCENT_LO = "#6857e6"   # accent pressed
DROP_BG = "#161a29"     # drop zone fill
DROP_BG_HI = "#1d2236"  # drop zone hover fill
DROP_BORDER = "#303653"
FG = "#eceef6"          # primary text
FG_MUTED = "#9aa1b6"    # secondary text
FG_FAINT = "#5f6884"    # labels / placeholder
OK = "#4ade80"          # success
ERR = "#f87171"         # error

# Fixed content width; the window *height* is always sized to fit its content
# (see App._fit_and_center) so nothing clips regardless of the platform's font
# metrics. The cards likewise grow to fit their own content.
WIN_W = 600
PAD = 26                # left/right window padding
CONTENT_W = WIN_W - 2 * PAD


def _round_rect(x1, y1, x2, y2, r):
    """Point list for a rounded rectangle, for ``create_polygon(smooth=True)``."""
    return [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]


class RoundedButton(tk.Canvas):
    """A flat, rounded button drawn on a Canvas (crisp text, real hover states)."""

    def __init__(
        self,
        parent,
        text,
        command=None,
        *,
        style="primary",
        height=40,
        radius=11,
        pad_x=22,
        min_width=0,
    ):
        self._font = tkfont.Font(family="Segoe UI Semibold", size=10)
        self._parent_bg = parent.cget("bg")
        self.command = command
        self.style = style
        self._text = text
        self._enabled = True
        self._focused = False

        if style == "primary":
            self._c = (ACCENT, ACCENT_HI, ACCENT_LO)
            self._fg = "#ffffff"
            self._bordered = False
        else:  # secondary / ghost
            self._c = (SURFACE_HI, BORDER_HI, SURFACE)
            self._fg = FG
            self._bordered = True

        width = max(min_width, self._font.measure(text) + 2 * pad_x)
        super().__init__(
            parent, width=width, height=height,
            bg=self._parent_bg, highlightthickness=0, bd=0, takefocus=True,
        )
        self._cw, self._ch, self._rad = width, height, radius
        self._fill = self._c[0]
        self._render()
        self.configure(cursor="hand2")
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<FocusIn>", lambda e: self._focus(True))
        self.bind("<FocusOut>", lambda e: self._focus(False))

    def _render(self):
        self.delete("all")
        fill = self._fill if self._enabled else SURFACE
        fg = self._fg if self._enabled else FG_FAINT
        outline = fill
        if self._bordered:
            outline = BORDER_HI if (self._enabled and self._fill == self._c[1]) else BORDER
        if self._focused and self._enabled:
            outline = FG
        self.create_polygon(
            _round_rect(1, 1, self._cw - 1, self._ch - 1, self._rad),
            smooth=True, fill=fill, outline=outline,
        )
        self.create_text(self._cw / 2, self._ch / 2 + 1, text=self._text, fill=fg, font=self._font)

    def _enter(self, _):
        if self._enabled:
            self._fill = self._c[1]
            self._render()

    def _leave(self, _):
        if self._enabled:
            self._fill = self._c[0]
            self._render()

    def _press(self, _):
        if self._enabled:
            self.focus_set()
            self._fill = self._c[2]
            self._render()

    def _release(self, event):
        if not self._enabled:
            return
        inside = 0 <= event.x < self._cw and 0 <= event.y < self._ch
        self._fill = self._c[1] if inside else self._c[0]
        self._render()
        if inside and self.command:
            self.command()

    def _activate(self, _=None):
        if self._enabled and self.command:
            self.command()
        return "break"

    def _focus(self, on):
        self._focused = on
        self._render()

    def set_enabled(self, on):
        self._enabled = bool(on)
        self._fill = self._c[0]
        self.configure(cursor="hand2" if on else "arrow", takefocus=bool(on))
        self._render()

    def set_text(self, text):
        self._text = text
        self._render()


class TokenDialog(tk.Toplevel):
    """A themed, non-blocking dialog for one already extracted account."""

    def __init__(self, parent, on_add, on_close):
        super().__init__(parent, bg=BG)
        self.title("Add an account — JWTractor")
        self.transient(parent)
        self.resizable(False, False)
        self._on_add, self._on_close = on_add, on_close
        try:
            self.iconbitmap(_resource("icon.ico"))
        except tk.TclError:
            pass
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=24)
        tk.Label(body, text="Add an account", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 16), anchor="w").pack(fill="x")
        tk.Label(body, text="Paste a token you already have.", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 10), anchor="w").pack(fill="x", pady=(4, 20))
        tk.Label(body, text="TOKEN", bg=BG, fg=FG_FAINT,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, 7))
        self.token = tk.Text(body, width=1, height=5, wrap="char", bg=FIELD, fg=FG,
                             insertbackground=FG, selectbackground=ACCENT_LO,
                             font=("Consolas", 10), padx=10, pady=9, relief="flat", bd=0,
                             highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.token.pack(fill="x")
        tk.Label(body, text="Accepts a token alone or username----token.", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(7, 18))
        tk.Label(body, text="STEAM LOGIN NAME", bg=BG, fg=FG_FAINT,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, 7))
        self.username = tk.Entry(body, width=1, bg=FIELD, fg=FG, insertbackground=FG,
                                 selectbackground=ACCENT_LO, font=("Segoe UI", 11), relief="flat", bd=0,
                                 highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.username.pack(fill="x", ipady=8)
        tk.Label(body, text="Leave blank if the pasted text includes the username.", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 9), wraplength=470, justify="left", anchor="w").pack(fill="x", pady=(7, 0))
        self.error = tk.Label(body, text="", bg=BG, fg=ERR, font=("Segoe UI", 10),
                               wraplength=470, justify="left", anchor="w")
        self._actions = tk.Frame(body, bg=BG)
        self._actions.pack(fill="x", pady=(22, 0))
        self.add_btn = RoundedButton(self._actions, "Add account", self._submit, min_width=148)
        self.add_btn.pack(side="right")
        self.cancel_btn = RoundedButton(self._actions, "Cancel", self.close, style="secondary", min_width=110)
        self.cancel_btn.pack(side="right", padx=(0, 10))
        self.bind("<Escape>", self.close)
        self.bind("<Control-Return>", self._submit)
        self.username.bind("<Return>", self._submit)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._fit(center=True)
        _use_dark_titlebar(self)
        self.grab_set()
        self._focus_job = self.after_idle(self.token.focus_set)

    def _fit(self, center=False):
        self.update_idletasks()
        width, height = 520, self.winfo_reqheight()
        position = ""
        if center:
            parent = self.master
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
            x = max(0, min(x, self.winfo_screenwidth() - width))
            y = max(0, min(y, self.winfo_screenheight() - height))
            position = f"+{x}+{y}"
        self.geometry(f"{width}x{height}{position}")

    def _submit(self, _=None):
        try:
            self._on_add(self.token.get("1.0", "end-1c"), self.username.get())
        except ExtractionError as exc:
            self.error.configure(text=str(exc))
            self.error.pack(before=self._actions, fill="x", pady=(12, 0))
            self._fit()
            if "login name" in str(exc).lower():
                self.username.focus_set()
            else:
                self.token.focus_set()
        except OSError:
            self.error.configure(text="Couldn't save the account. Check that the saved-accounts folder is writable and try again.")
            self.error.pack(before=self._actions, fill="x", pady=(12, 0))
            self._fit()
        else:
            self.close()
        return "break"

    def close(self, _=None):
        self.after_cancel(self._focus_job)
        self.grab_release()
        self.destroy()
        self._on_close()
        return "break"


class RoundedCard(tk.Canvas):
    """A rounded, bordered panel that hosts child widgets in ``self.inner``.

    The width is fixed; the height is not baked in — call ``fit()`` after the
    inner content changes and the card grows/shrinks to wrap it exactly. This
    keeps the layout free of hard-coded pixel heights that could clip text on
    machines whose fonts render taller.
    """

    def __init__(self, parent, width, *, fill, border=BORDER, radius=13, inset=None):
        super().__init__(
            parent, width=width, height=2 * radius,
            bg=parent.cget("bg"), highlightthickness=0, bd=0,
        )
        self._card_w = width
        self._radius = radius
        self._fill = fill
        self._border = border
        self.inset = inset if inset is not None else radius - 2
        self.inner = tk.Frame(self, bg=fill)
        # Force the inner width (so text wraps predictably) but let its height
        # follow the content.
        self._win = self.create_window(
            self.inset, self.inset, window=self.inner, anchor="nw",
            width=width - 2 * self.inset,
        )
        self._paint(2 * radius)

    def _paint(self, height):
        self.delete("shape")
        self.create_polygon(
            _round_rect(1, 1, self._card_w - 1, height - 1, self._radius),
            smooth=True, fill=self._fill, outline=self._border, tags="shape",
        )
        self.tag_lower("shape")  # keep the inner window above the shape

    def fit(self, min_height=0):
        """Resize the card to hug ``self.inner``'s current requested height."""
        self.inner.update_idletasks()
        height = max(min_height, self.inner.winfo_reqheight() + 2 * self.inset)
        self.configure(height=height)
        self._paint(height)


class DropZone(tk.Canvas):
    """A rounded, clickable drop target with an icon and a hover state."""

    def __init__(self, parent, width, height, on_click, *, dnd=False):
        super().__init__(
            parent, width=width, height=height,
            bg=parent.cget("bg"), highlightthickness=0, bd=0,
        )
        self._cw, self._ch = width, height
        self._on_click = on_click
        self._dnd = dnd
        self._hover = False
        self._render()
        self.configure(cursor="hand2")
        self.bind("<Enter>", lambda e: self.set_hover(True))
        self.bind("<Leave>", lambda e: self.set_hover(False))
        self.bind("<Button-1>", lambda e: self._on_click())

    def set_hover(self, on):
        self._hover = bool(on)
        self._render()

    def _render(self):
        self.delete("all")
        fill = DROP_BG_HI if self._hover else DROP_BG
        border = ACCENT if self._hover else DROP_BORDER
        icon = ACCENT if self._hover else "#6f78a0"
        self.create_polygon(
            _round_rect(1, 1, self._cw - 1, self._ch - 1, 16),
            smooth=True, fill=fill, outline=border,
        )
        cx = self._cw / 2
        cy = self._ch / 2 - 18
        self._icon(cx, cy, icon)
        line1 = "Drag an .exe here" if self._dnd else "Click to choose an .exe"
        self.create_text(cx, self._ch / 2 + 20, text=line1, fill=FG, font=("Segoe UI", 12))
        self.create_text(
            cx, self._ch / 2 + 42, text="or click to browse",
            fill=FG_MUTED, font=("Segoe UI", 10),
        )

    def _icon(self, cx, cy, color):
        # A "download into a tray" glyph: arrow above an open-top box.
        w = 2
        self.create_line(cx, cy - 14, cx, cy + 8, fill=color, width=w, capstyle="round")
        self.create_line(cx - 7, cy + 1, cx, cy + 8, fill=color, width=w, capstyle="round")
        self.create_line(cx + 7, cy + 1, cx, cy + 8, fill=color, width=w, capstyle="round")
        self.create_line(cx - 14, cy + 7, cx - 14, cy + 17, fill=color, width=w, capstyle="round")
        self.create_line(cx - 14, cy + 17, cx + 14, cy + 17, fill=color, width=w, capstyle="round")
        self.create_line(cx + 14, cy + 17, cx + 14, cy + 7, fill=color, width=w, capstyle="round")


def _resource(name):
    """Path to a bundled resource, whether running from source or a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _use_dark_titlebar(root):
    """Ask Windows to paint this window's title bar dark, to match the app."""
    try:  # pragma: no cover - Windows-only, no-op elsewhere
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (current, then legacy)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
        # Nudge the frame so the new title-bar colour paints immediately.
        root.withdraw()
        root.deiconify()
    except Exception:
        pass


def _draw_logo(canvas, s):
    """A rounded accent tile with a white 'extract' mark (arrow onto a baseline)."""
    canvas.create_polygon(
        _round_rect(1, 1, s - 1, s - 1, s * 0.28), smooth=True, fill=ACCENT, outline=ACCENT
    )
    cx = s / 2
    canvas.create_line(cx, s * 0.26, cx, s * 0.60, fill="white", width=2.4, capstyle="round")
    canvas.create_line(cx - 6, s * 0.46, cx, s * 0.60, fill="white", width=2.4, capstyle="round")
    canvas.create_line(cx + 6, s * 0.46, cx, s * 0.60, fill="white", width=2.4, capstyle="round")
    canvas.create_line(s * 0.30, s * 0.72, s * 0.70, s * 0.72, fill="white", width=2.4, capstyle="round")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.resizable(False, False)
        try:  # custom title-bar / taskbar icon when icon.ico is present
            root.iconbitmap(_resource("icon.ico"))
        except Exception:  # pragma: no cover - falls back to the default icon
            pass

        self.result_text = ""  # the current "name----token" string
        self.store = Store()  # saved accounts (tokens + aliases) on disk
        self.current_account = None  # the saved account currently shown, if any
        self._picker = None  # the open account-picker popup, if any
        self._picker_closed_at = 0.0
        self._token_dialog = None
        self._login_thread = None
        self._login_cancel = threading.Event()
        self._login_events = queue.Queue()
        self._closing = False

        self._build_ui()
        self._fit_and_center()  # size the window to its content, then centre
        _use_dark_titlebar(root)

        # Keyboard: Ctrl+C copies the result, Esc closes the window.
        root.bind("<Control-c>", lambda e: self._copy())
        root.bind("<Escape>", self._request_close)
        root.protocol("WM_DELETE_WINDOW", self._request_close)
        root.bind("<MouseWheel>", self._scroll_content)

        if _DND_AVAILABLE:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)
            self.drop.dnd_bind("<<DragEnter>>", lambda e: self.drop.set_hover(True))
            self.drop.dnd_bind("<<DragLeave>>", lambda e: self.drop.set_hover(False))

    # -- layout --------------------------------------------------------------
    def _section(self, text):
        return tk.Label(
            self.content, text=text, bg=BG, fg=FG_FAINT,
            font=("Segoe UI", 9, "bold"), anchor="w",
        )

    def _build_ui(self):
        self._mono = tkfont.Font(family="Consolas", size=10)  # result font + metrics
        self.viewport = tk.Canvas(self.root, width=WIN_W, bg=BG, bd=0, highlightthickness=0)
        self.viewport.pack(fill="both", expand=True)
        self.content = tk.Frame(self.viewport, bg=BG)
        self.viewport.create_window(0, 0, window=self.content, anchor="nw", width=WIN_W)
        self.scrollbar = tk.Canvas(self.viewport, bg=BG, bd=0, highlightthickness=0, cursor="hand2")
        self.viewport.configure(yscrollcommand=self._update_scrollbar)
        self.viewport.bind("<Configure>", lambda _: self._update_scrollbar(*self.viewport.yview()))
        self.scrollbar.bind("<Button-1>", self._drag_scrollbar)
        self.scrollbar.bind("<B1-Motion>", self._drag_scrollbar)

        # header: logo tile + title/subtitle
        header = tk.Frame(self.content, bg=BG)
        header.pack(fill="x", padx=PAD, pady=(24, 0))

        logo = tk.Canvas(header, width=46, height=46, bg=BG, highlightthickness=0, bd=0)
        logo.pack(side="left")
        _draw_logo(logo, 46)

        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left", padx=(14, 0))
        tk.Label(
            titles, text=APP_TITLE, bg=BG, fg=FG, font=("Segoe UI Semibold", 19)
        ).pack(anchor="w")
        tk.Label(
            titles, text="Extract or paste a token, then log in.",
            bg=BG, fg=FG_MUTED, font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(1, 0))
        self.paste_btn = RoundedButton(header, "Paste token", self._open_token_dialog,
                                       style="secondary", min_width=132, pad_x=14)
        self.paste_btn.pack(side="right", pady=(3, 0))

        # drop zone
        self.drop = DropZone(self.content, CONTENT_W, 130, self._browse, dnd=_DND_AVAILABLE)
        self.drop.pack(padx=PAD, pady=(18, 0))

        # saved accounts — pick one you've pulled before
        self._section("SAVED ACCOUNTS").pack(fill="x", padx=PAD, pady=(20, 7))
        self.saved_btn = RoundedButton(
            self.content, self._saved_btn_text(), self._toggle_picker,
            style="secondary", min_width=CONTENT_W,
        )
        self.saved_btn.pack(padx=PAD)

        # Steam config backup/restore is intentionally disabled pending redesign.

        # result
        self._section("RESULT").pack(fill="x", padx=PAD, pady=(22, 7))
        self.result_card = RoundedCard(self.content, CONTENT_W, fill=FIELD, border=BORDER)
        self.result_card.pack(padx=PAD)
        self._text_padx = 4
        self.output = tk.Text(
            self.result_card.inner, height=2, wrap="char", bg=FIELD, fg=FG,
            insertbackground=FG, relief="flat", highlightthickness=0, bd=0,
            font=self._mono, padx=self._text_padx, pady=2,
        )
        self.output.pack(fill="x")

        # details (decoded claims)
        self._section("DETAILS").pack(fill="x", padx=PAD, pady=(18, 7))
        self.details_card = RoundedCard(self.content, CONTENT_W, fill=SURFACE, border=BORDER)
        self.details_card.pack(padx=PAD)
        self.details_inner = self.details_card.inner

        # footer: actions above a wrapping status line
        footer = tk.Frame(self.root, bg=BG)
        footer.pack(side="bottom", before=self.viewport, fill="x", padx=PAD, pady=(20, 24))
        actions = tk.Frame(footer, bg=BG)
        actions.pack(fill="x")

        self.copy_btn = RoundedButton(actions, "Copy", self._copy, style="secondary", min_width=104)
        self.copy_btn.pack(side="left")
        self.copy_btn.set_enabled(False)
        self.login_btn = RoundedButton(actions, "Log in to Steam", self._login_to_steam, min_width=160)
        self.login_btn.pack(side="left", padx=(10, 0))
        self.login_btn.set_enabled(False)

        self.status = tk.Label(footer, text="", bg=BG, fg=FG_MUTED, font=("Segoe UI", 9),
                               wraplength=CONTENT_W, justify="left", anchor="w")
        self.status.pack(fill="x", pady=(10, 0))

        # initial (empty) content — also sizes the two cards to their placeholders
        self._set_output("")
        self._set_details([])

    # -- behaviour -----------------------------------------------------------
    def _on_drop(self, event):
        self.drop.set_hover(False)
        paths = self.root.tk.splitlist(event.data)
        if paths:
            self.process(paths[0], extra_files=len(paths) - 1)

    def _browse(self):
        if self._login_thread is not None:
            return
        path = filedialog.askopenfilename(
            title="Choose an executable",
            filetypes=[("Executables", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.process(path)

    def process(self, path: str, extra_files: int = 0):
        if self._login_thread is not None:
            return
        self._close_picker()
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

        try:
            self._accept_result(result)
        except OSError:
            self._set_status("Couldn't save the account. Check the saved-accounts folder and try again.", ERR)
            return

        note = ""
        if len(result["all_tokens"]) > 1:
            note = f"  ·  {len(result['all_tokens'])} tokens found, showing first"
        elif extra_files:
            note = f"  ·  {extra_files} more ignored (drop one at a time)"
        self._set_status(f"Extracted from {name} — copied · saved{note}", OK)

    def _accept_result(self, result):
        # Save successfully before replacing the current account or result.
        account = self.store.add(result["token"], result["username"])
        self.current_account = account
        self._set_output(result["combined"])
        self._set_details(summarize_claims(result["payload"]))
        self._refresh_login_action()
        self._refresh_saved()
        self._refit()
        self._copy(announce=False)
        return account

    # -- direct token entry --------------------------------------------------
    def _open_token_dialog(self):
        if self._login_thread is not None:
            return
        if self._token_dialog is not None:
            self._token_dialog.lift()
            self._token_dialog.token.focus_set()
            return
        self._close_picker()
        self._token_dialog = TokenDialog(self.root, self._add_token, self._token_dialog_closed)

    def _token_dialog_closed(self):
        self._token_dialog = None
        self.paste_btn.focus_set()

    def _add_token(self, text, username=""):
        if self._login_thread is not None:
            raise ExtractionError("Wait for Steam login to finish before adding an account.")
        account = self._accept_result(parse_token_input(text, username))
        self._set_status(f"Added {account_label(account)} — copied · saved", OK)
        return account

    # -- saved accounts ------------------------------------------------------
    def _saved_btn_text(self) -> str:
        n = len(self.store.accounts)
        return "Saved accounts — none yet  ▾" if n == 0 else f"Saved accounts ({n})  ▾"

    def _refresh_saved(self):
        self.saved_btn.set_text(self._saved_btn_text())

    def _toggle_picker(self):
        if self._picker is not None:
            self._close_picker()
            return
        # The same click that dismissed an open popup also lands on the button;
        # ignore it so the button doesn't immediately reopen the popup.
        if time.monotonic() - self._picker_closed_at < 0.25:
            return
        self._open_picker()

    def _close_picker(self, *_):
        if self._picker is not None:
            self._picker.destroy()
            self._picker = None
            self._picker_closed_at = time.monotonic()

    def _open_picker(self):
        if self._login_thread is not None:
            return
        accounts = self.store.ordered()
        self.root.update_idletasks()
        x = self.saved_btn.winfo_rootx()
        y = self.saved_btn.winfo_rooty() + self.saved_btn.winfo_height() + 4
        width = self.saved_btn.winfo_width()

        top = tk.Toplevel(self.root, bg=BORDER_HI)  # bg shows as a 1px border
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        self._picker = top

        body = tk.Frame(top, bg=SURFACE)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        if not accounts:
            tk.Label(
                body, text="No saved accounts yet.\nExtract a token and it's kept here.",
                bg=SURFACE, fg=FG_MUTED, font=("Segoe UI", 10),
                justify="left", padx=16, pady=16,
            ).pack(anchor="w")
        else:
            canvas = tk.Canvas(body, bg=SURFACE, highlightthickness=0, bd=0)
            inner = tk.Frame(canvas, bg=SURFACE)
            canvas.create_window(0, 0, window=inner, anchor="nw", width=width - 2)
            canvas.pack(fill="both", expand=True)
            for acc in accounts:
                self._build_account_row(inner, acc)
            inner.update_idletasks()
            content_h = inner.winfo_reqheight()
            view_h = min(content_h, 6 * 58)  # show ~6 rows, then scroll
            canvas.configure(height=view_h, scrollregion=(0, 0, width - 2, content_h))
            if content_h > view_h:
                canvas.bind_all(
                    "<MouseWheel>",
                    lambda e: (canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"), "break")[1],
                )
                top.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        top.update_idletasks()
        top.geometry(f"{width}x{top.winfo_reqheight()}+{x}+{y}")
        top.bind("<Escape>", self._close_picker)
        top.focus_force()
        top.bind("<FocusOut>", self._close_picker)

    def _build_account_row(self, parent, acc):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x")

        text = tk.Frame(row, bg=SURFACE)
        text.pack(side="left", fill="x", expand=True, padx=(14, 8), pady=8)
        name_lbl = tk.Label(
            text, text=account_label(acc), bg=SURFACE, fg=FG,
            font=("Segoe UI Semibold", 11), anchor="w",
        )
        name_lbl.pack(anchor="w")
        parts = []
        if (acc.get("alias") or "").strip() and acc.get("username"):
            parts.append(acc["username"])  # show the real username when aliased
        if acc.get("issuer"):
            parts.append(str(acc["issuer"]))
        sub_lbl = tk.Label(
            text, text="  ·  ".join(parts) or "—", bg=SURFACE, fg=FG_MUTED,
            font=("Segoe UI", 9), anchor="w",
        )
        sub_lbl.pack(anchor="w", pady=(1, 0))

        actions = tk.Frame(row, bg=SURFACE)
        actions.pack(side="right", padx=(8, 12))
        rename_lbl = tk.Label(actions, text="Rename", bg=SURFACE, fg=FG_MUTED,
                              font=("Segoe UI", 9), cursor="hand2")
        rename_lbl.pack(side="left", padx=(0, 10))
        del_lbl = tk.Label(actions, text="Delete", bg=SURFACE, fg=FG_MUTED,
                           font=("Segoe UI", 9), cursor="hand2")
        del_lbl.pack(side="left")

        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x")  # hairline divider

        # Reliable hover across the whole row (child Enter/Leave would flicker):
        # recolour based on whether the pointer is actually within this row.
        row_widgets = [row, text, name_lbl, sub_lbl, actions, rename_lbl, del_lbl]

        def _refresh_hover(_=None):
            under = self.root.winfo_containing(
                self.root.winfo_pointerx(), self.root.winfo_pointery()
            )
            path = str(under) if under is not None else ""
            inside = under is row or path.startswith(str(row) + ".")
            bg = SURFACE_HI if inside else SURFACE
            for w in row_widgets:
                try:
                    w.configure(bg=bg)
                except tk.TclError:  # pragma: no cover - widget torn down
                    pass

        for w in row_widgets:
            w.bind("<Enter>", _refresh_hover, add="+")
            w.bind("<Leave>", lambda e: parent.after_idle(_refresh_hover), add="+")
        for w in (row, text, name_lbl, sub_lbl):
            w.bind("<Button-1>", lambda e, a=acc: self._use_account(a))
        rename_lbl.bind("<Button-1>", lambda e, a=acc: self._rename_account(a))
        del_lbl.bind("<Button-1>", lambda e, a=acc: self._delete_account(a))
        for w in (rename_lbl, del_lbl):
            w.bind("<Enter>", lambda e, ww=w: ww.configure(fg=FG), add="+")
            w.bind("<Leave>", lambda e, ww=w: ww.configure(fg=FG_MUTED), add="+")

    def _use_account(self, acc):
        if self._login_thread is not None:
            return
        self._close_picker()
        self._set_output(account_combined(acc))
        try:
            payload = decode_token(acc["token"]).get("payload")
        except Exception:  # pragma: no cover - stored tokens were valid when saved
            payload = None
        self._set_details(summarize_claims(payload))
        self.current_account = acc
        self._refresh_login_action()
        self.store.touch(acc["id"])
        self._refresh_saved()
        self._refit()
        self._copy(announce=False)
        self._set_status(f"Loaded {account_label(acc)} — copied", OK)

    def _rename_account(self, acc):
        self._close_picker()
        alias = simpledialog.askstring(
            "Rename account",
            f"Alias for “{acc.get('username')}”\n(leave blank to clear):",
            initialvalue=(acc.get("alias") or "").strip(), parent=self.root,
        )
        if alias is not None:  # None means the dialog was cancelled
            self.store.set_alias(acc["id"], alias)
            self._refresh_saved()
        self._open_picker()

    def _delete_account(self, acc):
        self._close_picker()
        if messagebox.askyesno(
            "Delete account",
            f"Forget “{account_label(acc)}”?\n\n"
            "This removes the saved token from JWTractor. It doesn't affect the "
            "account itself.",
            parent=self.root, icon="warning",
        ):
            self.store.remove(acc["id"])
            if self.current_account and self.current_account.get("id") == acc["id"]:
                self.current_account = None
                self._refresh_login_action()
            self._refresh_saved()
        self._open_picker()

    # -- Steam login ---------------------------------------------------------
    def _refresh_login_action(self):
        busy = self._login_thread is not None
        self.login_btn.set_text("Cancel login" if busy else "Log in to Steam")
        self.login_btn.set_enabled(busy or (os.name == "nt" and self.current_account is not None))
        self.saved_btn.set_enabled(not busy)
        self.paste_btn.set_enabled(not busy)
        self.copy_btn.set_enabled(bool(self.result_text) and not busy)

    def _login_to_steam(self):
        if self._login_thread is not None:
            self._login_cancel.set()
            self.login_btn.set_enabled(False)
            self._set_status("Cancelling Steam login…")
            return
        if self.current_account is None:
            return
        account = dict(self.current_account)
        try:
            # Use the original login name, never the friendly display alias.
            validate_account_name(account.get("username"))
            validate_token(account.get("token"))
        except SteamLoginError as exc:
            self._set_status(str(exc), ERR)
            self._refit()
            return
        self._close_picker()
        self._login_cancel.clear()
        self._login_thread = threading.Thread(target=self._login_worker, args=(account,), name="Steam login")
        self._refresh_login_action()
        self._set_status("Preparing Steam login…")
        self._login_thread.start()
        self.root.after(80, self._drain_login_events)

    def _login_worker(self, account):
        try:
            result = login_account(account["username"], account["token"], cancel=self._login_cancel,
                                   progress=lambda message: self._login_events.put(("progress", message)))
            self._login_events.put(("done", result))
        except LoginCancelled as exc:
            self._login_events.put(("cancelled", str(exc)))
        except SteamLoginError as exc:
            self._login_events.put(("error", str(exc)))
        except Exception:
            self._login_events.put(("error", "Unexpected Steam login failure. Check Steam and its configuration backups before retrying."))

    def _drain_login_events(self):
        while True:
            try:
                kind, value = self._login_events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                if not self._login_cancel.is_set():
                    self._set_status(value)
                continue
            self._login_thread = None
            if self._closing:
                self.root.destroy()
                return
            self._refresh_login_action()
            if kind == "done":
                sign_in = value.get("sign_in", "unconfirmed")
                count = value["preserved_accounts"]
                preserved = f"Kept {count} other remembered account{'s' if count != 1 else ''}."
                if sign_in == "rejected":
                    self._set_status(f"Steam rejected sign-in: {value.get('reason') or 'Login rejected'}. Check the session with the account owner.", ERR)
                elif sign_in == "other_account":
                    self._set_status("Steam signed in to a different account. Select the account you added in Steam.", "#facc15")
                elif sign_in == "confirmed":
                    self._set_status(f"Signed in to Steam. {preserved}", OK)
                elif value.get("warning"):
                    self._set_status(f"Steam launched. {value['warning']}", "#facc15")
                else:
                    self._set_status(f"Steam launched; sign-in wasn't confirmed. Check the client. {preserved}", FG_MUTED)
            else:
                self._set_status(value, FG_MUTED if kind == "cancelled" else ERR)
            self._refit()
        if self._login_thread is not None:
            self.root.after(80, self._drain_login_events)

    def _request_close(self, *_):
        if self._login_thread is not None:
            self._closing = True
            self._login_cancel.set()
            self.login_btn.set_enabled(False)
            self._set_status("Cancelling Steam login before closing…")
            return
        self.root.destroy()

    # -- output helpers ------------------------------------------------------
    def _set_output(self, text: str):
        self.result_text = text
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        if text:
            self.output.insert("1.0", text)
            self.output.configure(fg=FG)
        else:
            self.output.insert("1.0", "The <name>----<token> string will appear here.")
            self.output.configure(fg=FG_FAINT)
        self.output.configure(state="disabled")
        self.copy_btn.set_enabled(bool(text))
        self._reflow_output()

    def _reflow_output(self):
        """Grow the result Text to fit its wrapped content, then fit its card.

        The line count is derived from the *measured* monospace character width
        rather than the widget's laid-out state, so it is correct on any machine
        and doesn't depend on when this runs. A small margin rounds the wrap
        width down, so the box is never a line too short (never clips).
        """
        text = self.output.get("1.0", "end-1c")
        char_w = max(1, self._mono.measure("0"))
        avail = CONTENT_W - 2 * self.result_card.inset - 2 * self._text_padx - 2
        per_line = max(1, int(avail // char_w))
        lines = (len(text) + per_line - 1) // per_line  # ceil division
        self.output.configure(height=max(2, min(lines, 12)))
        self.result_card.fit()

    def _set_details(self, rows: list[tuple[str, str]]):
        for child in self.details_inner.winfo_children():
            child.destroy()
        if not rows:
            tk.Label(
                self.details_inner,
                text="Token claims (issuer, expiry, …) will appear here.",
                bg=SURFACE, fg=FG_FAINT, font=("Segoe UI", 10),
            ).grid(row=0, column=0, sticky="w")
        else:
            _WARN_LABELS = {"Status", "Revoked if", "Note"}
            for i, (label, value) in enumerate(rows):
                is_warn = label in _WARN_LABELS
                val_fg = FG
                if label == "Status" and "EXPIRED" in value and "NOT" not in value:
                    val_fg = ERR
                elif label == "Status":
                    val_fg = "#facc15"
                elif is_warn:
                    val_fg = FG_MUTED
                tk.Label(
                    self.details_inner, text=label, bg=SURFACE, fg=FG_MUTED,
                    font=("Segoe UI", 10),
                ).grid(row=i, column=0, sticky="nw", padx=(0, 18), pady=2)
                tk.Label(
                    self.details_inner, text=value, bg=SURFACE, fg=val_fg,
                    font=("Segoe UI Semibold", 10),
                    wraplength=350, justify="left",
                ).grid(row=i, column=1, sticky="w", pady=2)
        self.details_card.fit(min_height=44)

    def _show_error(self, message: str):
        self.current_account = None
        self._refresh_login_action()
        self._set_output("")
        self._set_details([])
        self._refit()
        self._set_status(message, ERR)

    def _set_status(self, text: str, color: str = FG_MUTED):
        self.status.configure(text=text, fg=color)
        self._refit()

    def _copy(self, announce: bool = True):
        if not self.result_text or self._login_thread is not None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.result_text)
        if announce:
            self._set_status("Copied to clipboard", OK)
            self.copy_btn.set_text("Copied!")
            self.root.after(1200, lambda: self.copy_btn.set_text("Copy"))

    # -- misc ----------------------------------------------------------------
    def _fit_and_center(self):
        """Size the window to its content's requested size, then centre it."""
        self._refit()
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 3
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _refit(self):
        """Resize the window to its content's height, keeping its position."""
        self.root.update_idletasks()
        content_height = self.content.winfo_reqheight()
        self.viewport.configure(height=content_height, scrollregion=(0, 0, WIN_W, content_height))
        self.root.update_idletasks()
        h = min(self.root.winfo_reqheight(), self.root.winfo_screenheight() - 120)
        y = max(0, min(self.root.winfo_y(), self.root.winfo_screenheight() - h - 80))
        self.root.geometry(f"{WIN_W}x{h}+{max(0, self.root.winfo_x())}+{y}")

    def _update_scrollbar(self, first, last):
        first, last = float(first), float(last)
        if last - first >= 0.999:
            self.scrollbar.place_forget()
            return
        self.scrollbar.place(relx=1, x=-12, y=8, width=6, relheight=1, height=-16)
        height = max(1, self.viewport.winfo_height() - 16)
        self.scrollbar.delete("all")
        self.scrollbar.create_polygon(_round_rect(0, first * height, 6, max(first * height + 8, last * height), 3),
                                      smooth=True, fill=BORDER_HI, outline="")

    def _drag_scrollbar(self, event):
        first, last = self.viewport.yview()
        self.viewport.yview_moveto(event.y / max(1, self.scrollbar.winfo_height()) - (last - first) / 2)

    def _scroll_content(self, event):
        if self.viewport.yview() != (0.0, 1.0):
            direction = -1 if event.delta > 0 else 1
            self.viewport.yview_scroll(direction * max(1, abs(event.delta) // 120) * 2, "units")
            return "break"


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
