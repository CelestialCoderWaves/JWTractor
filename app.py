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
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox
from datetime import datetime, timezone

from cooldown import check_client_cooldown, current_steam_session
from cs2_settings import list_settings_sources
from diagnostics import Diagnostics
from presence import fetch_presence

from extractor import (
    DEFAULT_SEPARATOR,
    ExtractionError,
    decode_token,
    extract_from_file,
    parse_token_input,
    summarize_claims,
)
from store import Store, account_combined, account_label, account_status
from steam_login import LoginCancelled, SteamLoginError, login_account, validate_account_name, validate_token
from toggle_graphics import BUTTON_ICONS, DROP_ICONS, LOGO_IMAGE, SURFACE_CORNERS, SWITCH_IMAGES

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
FG_FAINT = "#8490ad"    # readable labels / placeholder
OK = "#4ade80"          # success
ERR = "#f87171"         # error

# Start at a readable size; taller content grows the window up to the screen
# limit, and manually resized windows keep their chosen dimensions.
WIN_W = 600
DEFAULT_H = 720
PAD = 24                # equal outer margins
GAP = 12                # shared spacing between controls and sections
CONTENT_W = WIN_W - 2 * PAD


def _round_rect(x1, y1, x2, y2, r):
    """Point list for a rounded rectangle, for ``create_polygon(smooth=True)``."""
    return [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def _paint_surface(canvas, width, height, radius, fill, border, tags="shape"):
    """Nine-slice rendering keeps rounded edges smooth as a panel grows."""
    key = (fill, border, radius)
    corner = radius + 2
    if key not in SURFACE_CORNERS or min(width, height) <= 2 * corner:
        canvas.create_polygon(_round_rect(1, 1, width - 1, height - 1, radius),
                              smooth=True, fill=fill, outline=border, tags=tags)
        return
    if not hasattr(canvas, "_surface_images"):
        canvas._surface_images = {}
    if key not in canvas._surface_images:
        canvas._surface_images[key] = tuple(tk.PhotoImage(master=canvas, data=data)
                                            for data in SURFACE_CORNERS[key])
    sprites = canvas._surface_images[key]
    canvas.create_rectangle(corner, corner, width - corner, height - corner,
                            fill=fill, outline="", tags=tags)
    for sprite, (x, y) in zip(sprites[:4], ((0, 0), (width - corner, 0),
                                          (0, height - corner), (width - corner, height - corner))):
        canvas.create_image(x, y, anchor="nw", image=sprite, tags=tags)
    edge_key = (key, width, height)
    if getattr(canvas, "_surface_edge_key", None) != edge_key:
        canvas._surface_edges = [sprites[4].zoom(width - 2 * corner, 1),
                                 sprites[5].zoom(width - 2 * corner, 1),
                                 sprites[6].zoom(1, height - 2 * corner),
                                 sprites[7].zoom(1, height - 2 * corner)]
        canvas._surface_edge_key = edge_key
    for sprite, (x, y) in zip(canvas._surface_edges, ((corner, 0), (corner, height - corner),
                                                    (0, corner), (width - corner, corner))):
        canvas.create_image(x, y, anchor="nw", image=sprite, tags=tags)


class GraphicCanvas(tk.Canvas):
    """Release native image resources when a control is destroyed."""

    def destroy(self):
        super().destroy()
        for name in ("_surface_images", "_surface_edges", "_icons", "_switch_images"):
            cache = getattr(self, name, None)
            if cache is not None:
                cache.clear()


class RoundedButton(GraphicCanvas):
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
        self._pressed = False

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
        _paint_surface(self, self._cw, self._ch, self._rad, fill, outline)
        self.create_text(self._cw / 2, self._ch / 2, text=self._text, fill=fg, font=self._font, tags="label")

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
            self._pressed = True
            self.focus_set()
            self._fill = self._c[2]
            self._render()

    def _release(self, event):
        pressed, self._pressed = self._pressed, False
        if not self._enabled or not pressed:
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
        self._pressed = False
        self._fill = self._c[0]
        self.configure(cursor="hand2" if on else "arrow", takefocus=bool(on))
        self._render()

    def set_text(self, text):
        self._text = text
        self._render()

    def set_width(self, width):
        self._cw = max(1, int(width))
        self.configure(width=self._cw)
        self._render()


class NavigationButton(RoundedButton):
    """Compact navigation with antialiased icons independent of system fonts."""

    def __init__(self, parent, command):
        self._icon = "settings"
        self._icons = {}
        super().__init__(parent, "Settings", command, style="secondary", height=28,
                         radius=8, min_width=112, pad_x=12)

    def set_destination(self, name):
        self._icon = "back" if name == "accounts" else "settings"
        self.set_text("Accounts" if name == "accounts" else "Settings")

    def _render(self):
        super()._render()
        self.delete("label")
        color = self._fg if self._enabled else "#5f6884"
        key = (self._icon, color)
        if key not in self._icons:
            self._icons[key] = tk.PhotoImage(master=self, data=BUTTON_ICONS[key])
        start = (self._cw - self._font.measure(self._text) - 18 - 8) / 2
        self.create_image(start, self._ch / 2, anchor="w", image=self._icons[key])
        self.create_text(start + 26, self._ch / 2, anchor="w", text=self._text,
                         fill=color, font=self._font, tags="label")


class ToggleSwitch(RoundedButton):
    """Compact switch with the same mouse, focus and keyboard behavior as buttons."""

    def __init__(self, parent, checked, command):
        self.checked = bool(checked)
        self._switch_images = {}
        super().__init__(parent, "", command, height=32, radius=16, pad_x=0, min_width=70)

    def set_checked(self, checked):
        self.checked = bool(checked)
        self._render()

    def _render(self):
        self.delete("all")
        state = "normal"
        if not self._enabled:
            state = "disabled"
        elif self._fill == self._c[2]:
            state = "pressed"
        elif self._fill == self._c[1]:
            state = "hover"
        key = (self.checked, state, self._focused and self._enabled)
        if key not in self._switch_images:
            self._switch_images[key] = tk.PhotoImage(master=self, data=SWITCH_IMAGES[key])
        self.create_image(0, 0, anchor="nw", image=self._switch_images[key])
        self.create_text(24 if self.checked else 45, 16, text="ON" if self.checked else "OFF",
                         font=("Segoe UI", 8, "bold"), fill="white" if self._enabled else FG_FAINT)


class TokenDialog(tk.Toplevel):
    """A themed, non-blocking dialog for one already extracted account."""

    def __init__(self, parent, on_add, on_close):
        super().__init__(parent, bg=BG)
        self.title("Add an account — JWTractor")
        self.transient(parent)
        self.resizable(False, False)
        self._on_add, self._on_close = on_add, on_close
        self._closed = False
        try:
            self.iconbitmap(_resource("icon.ico"))
        except tk.TclError:
            pass
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        tk.Label(body, text="Add an account", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 16), anchor="w").pack(fill="x")
        tk.Label(body, text="Paste a token you already have.", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 10), anchor="w").pack(fill="x", pady=GAP)
        tk.Label(body, text="TOKEN", bg=BG, fg=FG_FAINT,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, GAP))
        self.token = tk.Text(body, width=1, height=5, wrap="char", bg=FIELD, fg=FG,
                             insertbackground=FG, selectbackground=ACCENT_LO,
                             font=("Consolas", 10), padx=GAP, pady=GAP, relief="flat", bd=0,
                             highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.token.pack(fill="x")
        tk.Label(body, text="Accepts a token alone or username----token.", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=GAP)
        tk.Label(body, text="STEAM LOGIN NAME", bg=BG, fg=FG_FAINT,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, GAP))
        self.username = tk.Entry(body, width=1, bg=FIELD, fg=FG, insertbackground=FG,
                                 selectbackground=ACCENT_LO, font=("Segoe UI", 11), relief="flat", bd=0,
                                 highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.username.pack(fill="x", ipady=GAP)
        tk.Label(body, text="Filled automatically from username----token. Enter it for a token alone.", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 9), wraplength=470, justify="left", anchor="w").pack(fill="x", pady=(GAP, 0))
        self.error = tk.Label(body, text="", bg=BG, fg=ERR, font=("Segoe UI", 10),
                               wraplength=470, justify="left", anchor="w")
        self._actions = tk.Frame(body, bg=BG)
        self._actions.pack(fill="x", pady=(GAP, 0))
        action_width = (520 - 2 * PAD - GAP) // 2
        self.add_btn = RoundedButton(self._actions, "Add account", self._submit, min_width=action_width)
        self.add_btn.pack(side="right")
        self.cancel_btn = RoundedButton(self._actions, "Cancel", self.close, style="secondary", min_width=action_width)
        self.cancel_btn.pack(side="right", padx=(0, GAP))
        self.bind("<Escape>", self.close)
        self.bind("<Control-Return>", self._submit)
        self.username.bind("<Return>", self._submit)
        self.token.edit_modified(False)
        self.token.bind("<<Modified>>", self._token_changed)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._fit(center=True)
        _use_dark_titlebar(self)
        self.grab_set()
        self._focus_job = self.after_idle(self.token.focus_set)

    def _token_changed(self, _=None):
        if not self.token.edit_modified():
            return
        self.token.edit_modified(False)
        self._fill_username()

    def _fill_username(self):
        text = self.token.get("1.0", "end-1c").strip()
        if len(text) > 64 * 1024 or DEFAULT_SEPARATOR not in text:
            return
        if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
            text = text[1:-1].strip()
        try:
            name = parse_token_input(text)["username"]
        except ExtractionError:
            # A token alone can contain hyphens in its signature. Keep its
            # manually entered login name while the token is validated later.
            if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", re.sub(r"\s+", "", text)):
                return
            name = text.partition(DEFAULT_SEPARATOR)[0].strip()
            if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", name):
                return
        if self.username.get() != name:
            self.username.delete(0, "end")
            self.username.insert(0, name)

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
        self._fill_username()
        try:
            self._on_add(self.token.get("1.0", "end-1c"), self.username.get())
        except ExtractionError as exc:
            self.error.configure(text=str(exc))
            self.error.pack(before=self._actions, fill="x", pady=(GAP, 0))
            self._fit()
            if "login name" in str(exc).lower():
                self.username.focus_set()
            else:
                self.token.focus_set()
        except OSError:
            self.error.configure(text="Couldn't save the account. Check that the saved-accounts folder is writable and try again.")
            self.error.pack(before=self._actions, fill="x", pady=(GAP, 0))
            self._fit()
        else:
            self.close()
        return "break"

    def close(self, _=None):
        if self._closed:
            return "break"
        self._closed = True
        self.after_cancel(self._focus_job)
        self.grab_release()
        self.destroy()
        self._on_close()
        return "break"


class RoundedCard(GraphicCanvas):
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
        _paint_surface(self, self._card_w, height, self._radius, self._fill, self._border)
        self.tag_lower("shape")  # keep the inner window above the shape

    def fit(self, min_height=0):
        """Resize the card to hug ``self.inner``'s current requested height."""
        self.inner.update_idletasks()
        height = max(min_height, self.inner.winfo_reqheight() + 2 * self.inset)
        self.configure(height=height)
        self._paint(height)

    def set_width(self, width):
        self._card_w = max(2 * self.inset + 1, int(width))
        self.configure(width=self._card_w)
        self.itemconfigure(self._win, width=self._card_w - 2 * self.inset)
        self.fit()


class RenameDialog(tk.Toplevel):
    """Themed account alias editor, using the app's existing cards and buttons."""

    def __init__(self, parent, account, on_save, on_close):
        super().__init__(parent, bg=BG)
        self.title("Rename account — JWTractor")
        self.transient(parent)
        self.resizable(False, False)
        self._on_save, self._on_close = on_save, on_close
        self._closed = False
        try:
            self.iconbitmap(_resource("icon.ico"))
        except tk.TclError:
            pass
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        tk.Label(body, text="Rename account", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 16), anchor="w").pack(fill="x")
        tk.Label(body, text=f"Steam login: {account.get('username', '')}", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 10), wraplength=392, justify="left", anchor="w").pack(fill="x", pady=GAP)
        tk.Label(body, text="DISPLAY NAME", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(0, GAP))
        field = RoundedCard(body, 392, fill=FIELD, radius=12, inset=12)
        field.pack(fill="x")
        self.alias = tk.Entry(field.inner, width=1, bg=FIELD, fg=FG, insertbackground=FG,
                              selectbackground=ACCENT_LO, selectforeground="white", relief="flat", bd=0,
                              font=("Segoe UI", 11))
        self.alias.pack(fill="x")
        self.alias.insert(0, (account.get("alias") or "").strip())
        self.alias.selection_range(0, "end")
        self.alias.bind("<FocusIn>", lambda _: self._field_focus(field, True))
        self.alias.bind("<FocusOut>", lambda _: self._field_focus(field, False))
        field.fit(min_height=46)
        tk.Label(body, text="Leave blank to use the original account name.", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(GAP, 0))
        self.error = tk.Label(body, text="", bg=BG, fg=ERR, font=("Segoe UI", 9),
                              wraplength=392, justify="left", anchor="w")
        self.actions = tk.Frame(body, bg=BG)
        self.actions.pack(fill="x", pady=(GAP, 0))
        action_width = (440 - 2 * PAD - GAP) // 2
        self.save_btn = RoundedButton(self.actions, "Save name", self._submit, min_width=action_width)
        self.save_btn.pack(side="right")
        self.cancel_btn = RoundedButton(self.actions, "Cancel", self.close, style="secondary", min_width=action_width)
        self.cancel_btn.pack(side="right", padx=(0, GAP))
        self.alias.bind("<Return>", self._submit)
        self.bind("<Escape>", self.close)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._fit(center=True)
        _use_dark_titlebar(self)
        self.grab_set()
        self._focus_job = self.after_idle(self.alias.focus_set)

    def _field_focus(self, field, focused):
        field._border = ACCENT if focused else BORDER
        field.fit(min_height=46)

    def _fit(self, center=False):
        self.update_idletasks()
        width, height = 440, self.winfo_reqheight()
        position = ""
        if center:
            parent = self.master
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
            x = max(0, min(x, self.winfo_screenwidth() - width))
            y = max(0, min(y, self.winfo_screenheight() - height))
            position = f"+{x}+{y}"
        self.geometry(f"{width}x{height}{position}")

    def _submit(self, _=None):
        try:
            self._on_save(self.alias.get())
        except OSError:
            self.error.configure(text="Could not save the name. Check access to the accounts file and try again.")
            self.error.pack(before=self.actions, fill="x", pady=(GAP, 0))
            self._fit()
            self.alias.focus_set()
        else:
            self.close()
        return "break"

    def close(self, _=None):
        if not self._closed:
            self._closed = True
            self.after_cancel(self._focus_job)
            self.grab_release()
            self.destroy()
            self._on_close()
        return "break"


class DropZone(GraphicCanvas):
    """A rounded, clickable drop target with an icon and a hover state."""

    def __init__(self, parent, width, height, on_click, *, dnd=False):
        super().__init__(
            parent, width=width, height=height,
            bg=parent.cget("bg"), highlightthickness=0, bd=0,
        )
        self._cw, self._ch = width, height
        self._regular_height = height
        self._compact = False
        self._on_click = on_click
        self._dnd = dnd
        self._hover = False
        self._focused = False
        self._enabled = True
        self._icons = {}
        self._reading = False
        self._render()
        self.configure(cursor="hand2")
        self.bind("<Enter>", lambda e: self.set_hover(True))
        self.bind("<Leave>", lambda e: self.set_hover(False))
        self.configure(takefocus=True)
        self.bind("<Button-1>", self._activate)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<FocusIn>", lambda _: self._focus(True))
        self.bind("<FocusOut>", lambda _: self._focus(False))

    def _activate(self, _=None):
        if self._enabled:
            self.focus_set()
            self._on_click()
        return "break"

    def _focus(self, on):
        self._focused = on
        self._render()

    def set_enabled(self, on):
        self._enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow", takefocus=bool(on))
        self._render()

    def set_hover(self, on):
        self._hover = bool(on)
        self._render()

    def set_reading(self, on):
        self._reading = bool(on)
        self._render()

    def set_compact(self, on):
        self._compact = bool(on)
        self._ch = 76 if on else self._regular_height
        self.configure(height=self._ch)
        self._render()

    def set_width(self, width):
        self._cw = max(1, int(width))
        self.configure(width=self._cw)
        self._render()

    def _render(self):
        self.delete("all")
        hover = self._hover and self._enabled
        fill = DROP_BG_HI if hover else DROP_BG
        border = FG if self._focused and self._enabled else ACCENT if hover else DROP_BORDER
        icon = ACCENT if hover else FG_FAINT
        _paint_surface(self, self._cw, self._ch, 16, fill, border)
        if self._compact:
            self._icon(32, self._ch / 2, icon)
            self.create_text(64, self._ch / 2 - 11, anchor="w",
                             text="Reading executable…" if self._reading else "Import another executable",
                             fill=FG, font=("Segoe UI Semibold", 11))
            self.create_text(64, self._ch / 2 + 11, anchor="w",
                             text="You can cancel the import below." if self._reading else
                                  "Drag an .exe here or click to browse" if self._dnd else "Click to choose an .exe",
                             fill=FG_MUTED, font=("Segoe UI", 9))
            return
        cx = self._cw / 2
        self._icon(cx, 0, icon)
        line1 = "Drag an .exe here" if self._dnd else "Click to choose an .exe"
        if self._reading:
            line1 = "Reading executable…"
        self.create_text(cx, 0, text=line1, fill=FG, font=("Segoe UI", 12), tags="drop-title")
        self.create_text(
            cx, 0, text="You can cancel the import below." if self._reading else
                       "or click to browse" if self._dnd else "Browse for an executable file",
            fill=FG_MUTED, font=("Segoe UI", 10), tags="drop-hint",
        )
        cursor = 0
        for tag in ("drop-icon", "drop-title", "drop-hint"):
            _, top, _, bottom = self.bbox(tag)
            self.move(tag, 0, cursor - top)
            cursor += bottom - top + GAP
        offset = (self._ch - (cursor - GAP)) / 2
        for tag in ("drop-icon", "drop-title", "drop-hint"):
            self.move(tag, 0, offset)

    def _icon(self, cx, cy, color):
        if color not in self._icons:
            self._icons[color] = tk.PhotoImage(master=self, data=DROP_ICONS[color])
        self.create_image(cx, cy, image=self._icons[color], tags="drop-icon")


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
    """Render the same supersampled mark used by the executable icon."""
    canvas._logo_image = tk.PhotoImage(master=canvas, data=LOGO_IMAGE)
    canvas.create_image(s / 2, s / 2, image=canvas._logo_image)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._content_width = CONTENT_W
        self._manual_size = None
        self._auto_geometry = None
        self._resize_job = None
        self._layout_running = False
        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.resizable(True, True)
        root.minsize(520, min(420, max(260, root.winfo_screenheight() - 160)))
        try:  # custom title-bar / taskbar icon when icon.ico is present
            root.iconbitmap(_resource("icon.ico"))
        except Exception:  # pragma: no cover - falls back to the default icon
            pass

        self.result_text = ""  # the current "name----token" string
        self._copy_reset_job = None
        self.store = Store()  # saved accounts (tokens + aliases) on disk
        self.diagnostics = Diagnostics(os.path.dirname(os.path.abspath(self.store.path)))
        self.diagnostics.record("startup")
        self._settings_account_id = None
        self._presence = {}
        self._presence_worker = None
        self._presence_poll_job = None
        self._observed_session = current_steam_session()
        self._import_results = []
        self._import_paths = []
        self.private_login = self.store.preferences["private_login"]
        self.disable_cloud_sync = self.store.preferences["disable_cloud_sync"]
        self.use_cs2_launch_options = self.store.preferences["use_cs2_launch_options"]
        self._cs2_source_names = {}
        self.current_account = None  # the saved account currently shown, if any
        self._picker = None  # the open account-picker popup, if any
        self._picker_closed_at = 0.0
        self._token_dialog = None
        self._rename_dialog = None
        self._account_query = ""
        self._login_thread = None
        self._login_poll_job = None
        self._extraction = None
        self._extraction_poll_job = None
        self._login_cancel = threading.Event()
        self._login_events = queue.Queue()
        self._closing = False
        self._cooldown_check = None
        self._cooldown_poll_job = None
        self._cooldown_notice = {}
        self._detail_rows = []
        self._claims_expanded = False
        self.cooldown_btn = None
        self._steam_session_seen = None
        self._steam_watch_job = None

        self._build_ui()
        self._restore_selection()
        if self.store.load_error:
            self._set_status(self.store.load_error, ERR)
        self._fit_and_center()  # size the window to its content, then centre
        _use_dark_titlebar(root)
        root.bind("<Configure>", self._on_root_resize, add="+")

        # Text fields retain their native copy/paste behavior.
        root.bind("<Control-c>", self._copy_shortcut)
        root.bind("<Escape>", self._escape)
        root.bind("<Control-o>", lambda _: self._browse())
        root.bind("<Control-k>", lambda _: self._open_picker())
        root.bind("<Control-v>", self._paste_shortcut)
        root.bind("<Control-q>", self._request_close)
        root.protocol("WM_DELETE_WINDOW", self._request_close)
        root.bind("<MouseWheel>", self._scroll_content)
        root.bind("<Control-Tab>", self._cycle_tab)
        root.bind("<Control-Shift-Tab>", self._cycle_tab)
        root.bind("<Destroy>", self._stop_steam_watch, add="+")
        self._steam_watch_job = root.after(1500, self._watch_steam_session)
        root.report_callback_exception = self._ui_exception

        if _DND_AVAILABLE:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)
            self.drop.dnd_bind("<<DragEnter>>", lambda e: self.drop.set_hover(True))
            self.drop.dnd_bind("<<DragLeave>>", lambda e: self.drop.set_hover(False))

    # -- layout --------------------------------------------------------------
    def _section(self, text, parent=None):
        return tk.Label(
            parent if parent is not None else self.content, text=text, bg=BG, fg=FG_FAINT,
            font=("Segoe UI", 9, "bold"), anchor="w",
        )

    def _build_ui(self):
        self._mono = tkfont.Font(family="Consolas", size=10)  # result font + metrics
        self.viewport = tk.Canvas(self.root, width=WIN_W, bg=BG, bd=0, highlightthickness=0)
        self.viewport.pack(fill="both", expand=True)
        self.content = tk.Frame(self.viewport, bg=BG)
        self.settings_content = tk.Frame(self.viewport, bg=BG)
        self._pages = {"accounts": self.content, "settings": self.settings_content}
        self.active_tab = "accounts"
        self._tab_scroll = {"accounts": 0.0, "settings": 0.0}
        self._content_window = self.viewport.create_window(0, 0, window=self.content, anchor="nw", width=WIN_W)
        self.scrollbar = tk.Canvas(self.viewport, bg=BG, bd=0, highlightthickness=0, cursor="hand2")
        self.viewport.configure(yscrollcommand=self._update_scrollbar)
        self.viewport.bind("<Configure>", lambda _: self._update_scrollbar(*self.viewport.yview()))
        self.scrollbar.bind("<Button-1>", self._drag_scrollbar)
        self.scrollbar.bind("<B1-Motion>", self._drag_scrollbar)

        # Compact navigation in the upper-left corner, above the app header.
        toolbar = tk.Frame(self.root, bg=BG)
        toolbar.pack(before=self.viewport, fill="x", padx=PAD, pady=(PAD, 0))
        self.settings_btn = NavigationButton(toolbar, self._cycle_tab)
        self.settings_btn.pack(side="left")
        self.settings_btn.bind("<Left>", lambda _: self._select_tab("accounts"))
        self.settings_btn.bind("<Right>", lambda _: self._select_tab("settings"))
        self.steam_status = tk.Label(toolbar, bg=BG, fg=FG_MUTED, font=("Segoe UI", 9),
                                     anchor="e", justify="right", wraplength=self._content_width - 124)
        self.steam_status.pack(side="right")
        self._update_session_labels()

        # header: logo tile + title/subtitle
        header = tk.Frame(self.root, bg=BG)
        header.pack(before=self.viewport, fill="x", padx=PAD, pady=(GAP, 0))
        logo = tk.Canvas(header, width=46, height=46, bg=BG, highlightthickness=0, bd=0)
        logo.pack(side="left")
        _draw_logo(logo, 46)

        titles = tk.Frame(header, bg=BG)
        self.header_titles = titles
        titles.pack(side="left", fill="x", expand=True, padx=(GAP, GAP))
        tk.Label(
            titles, text=APP_TITLE, bg=BG, fg=FG, font=("Segoe UI Semibold", 19)
        ).pack(anchor="w")
        self.subtitle = tk.Label(
            titles, text="Extract or paste a token, then log in.",
            bg=BG, fg=FG_MUTED, font=("Segoe UI", 10), anchor="w", justify="left",
            wraplength=self._content_width - 46 - 2 * GAP - 132,
        )
        self.subtitle.pack(anchor="w", pady=(GAP, 0))
        self.paste_btn = RoundedButton(header, "Paste token", self._open_token_dialog,
                                       style="secondary", min_width=132, pad_x=GAP)
        self.paste_btn.pack(side="right", before=titles)

        # drop zone
        self.drop = DropZone(self.content, self._content_width, 130, self._browse, dnd=_DND_AVAILABLE)
        self.drop.pack(padx=PAD, pady=(GAP, 0))
        self.import_card = RoundedCard(self.content, self._content_width, fill=SURFACE, inset=GAP)
        tk.Label(self.import_card.inner, text="IMPORT RESULTS", bg=SURFACE, fg=FG_MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.import_report = tk.Text(self.import_card.inner, height=4, width=1, bg=SURFACE, fg=FG,
                                     font=("Segoe UI", 9), wrap="word", relief="flat", bd=0,
                                     highlightthickness=0, selectbackground=ACCENT_LO)
        self.import_report.pack(fill="x", pady=(GAP, 0))
        self.import_report.configure(state="disabled")

        # saved accounts — pick one you've pulled before
        self._section("SAVED ACCOUNTS").pack(fill="x", padx=PAD, pady=GAP)
        self.saved_btn = RoundedButton(
            self.content, self._saved_btn_text(), self._toggle_picker,
            style="secondary", min_width=self._content_width,
        )
        self.saved_btn.pack(padx=PAD)

        # Steam config backup/restore is intentionally disabled pending redesign.

        self._build_settings()

        self.details_heading = self._section("SELECTED ACCOUNT")
        self.details_heading.pack(fill="x", padx=PAD, pady=GAP)
        self.details_card = RoundedCard(self.content, self._content_width, fill=SURFACE, border=BORDER, inset=GAP)
        self.details_card.pack(padx=PAD)
        self.details_inner = self.details_card.inner

        # result
        self.token_heading = self._section("ACCOUNT TOKEN")
        self.token_heading.pack(fill="x", padx=PAD, pady=GAP)
        self.result_card = RoundedCard(self.content, self._content_width, fill=FIELD, border=BORDER, inset=GAP)
        self.result_card.pack(padx=PAD)
        self._text_padx = 0
        self.output = tk.Text(
            self.result_card.inner, width=1, height=2, wrap="char", bg=FIELD, fg=FG,
            insertbackground=FG, relief="flat", highlightthickness=0, bd=0,
            font=self._mono, padx=self._text_padx, pady=0, selectbackground=ACCENT_LO,
        )
        self.output.pack(fill="x")

        # footer: actions above a wrapping status line
        self.footer = footer = tk.Frame(self.root, bg=BG)
        footer.pack(side="bottom", before=self.viewport, fill="x", padx=PAD, pady=(GAP, PAD))
        self.login_summary = tk.Label(footer, bg=BG, fg=FG_MUTED, font=("Segoe UI", 9),
                                      wraplength=self._content_width, justify="left", anchor="w")
        self.actions = actions = tk.Frame(footer, bg=BG)
        actions.pack(fill="x")

        action_width = (self._content_width - GAP) // 2
        self.copy_btn = RoundedButton(actions, "Copy", self._copy, style="secondary", min_width=action_width)
        self.copy_btn.pack(side="left")
        self.copy_btn.set_enabled(False)
        self.login_btn = RoundedButton(actions, "Log in to Steam", self._login_to_steam, min_width=action_width)
        self.login_btn.pack(side="left", padx=(GAP, 0))
        self.login_btn.set_enabled(False)

        self.status = tk.Label(footer, text="", bg=BG, fg=FG_MUTED, font=("Segoe UI", 9),
                               wraplength=self._content_width, justify="left", anchor="w")

        # initial (empty) content — also sizes the two cards to their placeholders
        self._set_output("")
        self._set_details([])
        self._refresh_login_action()

    def _build_settings(self):
        tk.Label(self.settings_content, text="Settings", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 17), anchor="w").pack(fill="x", padx=PAD, pady=(GAP, 0))
        tk.Label(self.settings_content, text="Choose defaults or customize the selected account. Changes are saved automatically.",
                 bg=BG, fg=FG_MUTED, font=("Segoe UI", 10), anchor="w",
                 wraplength=self._content_width, justify="left").pack(fill="x", padx=PAD, pady=(GAP, 0))
        scope = tk.Frame(self.settings_content, bg=BG)
        scope.pack(fill="x", padx=PAD, pady=(GAP, 0))
        scope_width = (self._content_width - GAP) // 2
        self.defaults_scope_btn = RoundedButton(scope, "Default settings", lambda: self._change_settings_scope(None),
                                                style="secondary", height=32, min_width=scope_width)
        self.defaults_scope_btn.pack(side="left")
        self.account_scope_btn = RoundedButton(scope, "Selected account", self._edit_selected_settings,
                                               style="secondary", height=32, min_width=scope_width)
        self.account_scope_btn.pack(side="right")
        self.settings_scope_note = tk.Label(self.settings_content,
                                            text="Editing defaults. Accounts without custom settings inherit these options.",
                                            bg=BG, fg=FG_MUTED, font=("Segoe UI", 9), wraplength=self._content_width,
                                            anchor="w", justify="left")
        self.settings_scope_note.pack(fill="x", padx=PAD, pady=(GAP, 0))
        self.reset_profile_btn = RoundedButton(self.settings_content, "Use defaults for this account", self._reset_profile,
                                               style="secondary", height=32, pad_x=GAP)
        self.reset_profile_btn.pack(anchor="w", padx=PAD, pady=(GAP, 0))
        self.reset_profile_btn.set_enabled(False)
        option_heading = self._section("LOGIN OPTIONS", self.settings_content)
        option_heading.configure(fg=FG_MUTED)
        option_heading.pack(fill="x", padx=PAD, pady=GAP)
        self.login_options_card = RoundedCard(self.settings_content, self._content_width, fill=SURFACE, border=BORDER,
                                             radius=20, inset=GAP)
        self.login_options_card.pack(padx=PAD)
        for index, (name, title, description, command) in enumerate((
            ("private_login", "Start Steam w/ Remote Play & Friends offline",
             "Remote Play disabled. Games and downloads stay online.", self._toggle_private_login),
            ("disable_cloud_sync", "Disable Steam Cloud Sync",
             "Before every login. Game saves stay on this PC.", self._toggle_disable_cloud_sync),
        )):
            row = tk.Frame(self.login_options_card.inner, bg=SURFACE)
            row.pack(fill="x", pady=(0, GAP) if index == 0 else 0)
            button = ToggleSwitch(row, getattr(self, name), command)
            button.set_enabled(os.name == "nt")
            button.pack(side="right", padx=(GAP, 0))
            setattr(self, name + "_btn", button)
            text = tk.Frame(row, bg=SURFACE)
            text.pack(side="left", fill="x", expand=True)
            tk.Label(text, text=title, bg=SURFACE, fg=FG,
                     font=("Segoe UI Semibold", 10), anchor="w", justify="left",
                     wraplength=self._content_width - 2 * GAP - 70 - GAP).pack(fill="x")
            tk.Label(text, text=description, bg=SURFACE, fg=FG_MUTED,
                     font=("Segoe UI", 9), anchor="w", justify="left",
                     wraplength=self._content_width - 2 * GAP - 70 - GAP).pack(fill="x", pady=(GAP, 0))
        self.login_options_card.fit()
        tk.Label(self.settings_content, text="Turning an option off leaves Steam’s current settings in place.",
                 bg=BG, fg=FG_FAINT, font=("Segoe UI", 9), wraplength=self._content_width,
                 justify="left", anchor="w").pack(fill="x", padx=PAD, pady=(GAP, 0))

        self._section("CS2 LAUNCH OPTIONS", self.settings_content).pack(fill="x", padx=PAD, pady=GAP)
        self.cs2_options_card = RoundedCard(self.settings_content, self._content_width, fill=SURFACE,
                                           border=BORDER, radius=20, inset=GAP)
        self.cs2_options_card.pack(padx=PAD)
        row = tk.Frame(self.cs2_options_card.inner, bg=SURFACE)
        row.pack(fill="x")
        self.use_cs2_launch_options_btn = ToggleSwitch(row, self.use_cs2_launch_options,
                                                     self._toggle_use_cs2_launch_options)
        self.use_cs2_launch_options_btn.set_enabled(os.name == "nt")
        self.use_cs2_launch_options_btn.pack(side="right", padx=(GAP, 0))
        tk.Label(row, text="Use custom launch options", bg=SURFACE, fg=FG,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        tk.Label(self.cs2_options_card.inner, text="Replaces CS2 launch options for the selected account at login.",
                 bg=SURFACE, fg=FG_MUTED, font=("Segoe UI", 9), wraplength=self._content_width - 2 * GAP,
                 anchor="w", justify="left").pack(fill="x", pady=(GAP, 0))
        self.cs2_options_field = RoundedCard(self.cs2_options_card.inner, self._content_width - 2 * GAP,
                                            fill=FIELD, border=BORDER, radius=12, inset=GAP)
        self.cs2_options_field.pack(fill="x", pady=(GAP, 0))
        self.cs2_options_var = tk.StringVar(master=self.root, value=self.store.preferences["cs2_launch_options"])
        self.cs2_launch_options_entry = tk.Entry(self.cs2_options_field.inner, textvariable=self.cs2_options_var,
                                                bg=FIELD, fg=FG,
                                                insertbackground=FG, selectbackground=ACCENT_LO,
                                                disabledbackground=FIELD, disabledforeground=FG_FAINT,
                                                font=("Consolas", 10), relief="flat", bd=0)
        self.cs2_launch_options_entry.pack(fill="x")
        self.cs2_launch_options_entry.bind("<FocusIn>", lambda _: self._focus_cs2_options(True))
        self.cs2_launch_options_entry.bind("<FocusOut>", lambda _: self._save_cs2_launch_options())
        self.cs2_launch_options_entry.bind("<Return>", self._save_cs2_launch_options_on_enter)
        self.cs2_options_field.fit()
        tk.Label(self.cs2_options_card.inner, text="Blank clears CS2 options when enabled. Off leaves Steam’s options alone.",
                 bg=SURFACE, fg=FG_MUTED, font=("Segoe UI", 9), wraplength=self._content_width - 2 * GAP,
                 anchor="w", justify="left").pack(fill="x", pady=(GAP, 0))
        self.cs2_options_status = tk.Label(self.cs2_options_card.inner, text="Saved when you leave the field or press Enter.",
                                         bg=SURFACE, fg=FG_FAINT, font=("Segoe UI", 9),
                                         anchor="w", justify="left", wraplength=self._content_width - 2 * GAP)
        self.cs2_options_status.pack(fill="x", pady=(GAP, 0))
        self.cs2_revert_btn = RoundedButton(self.cs2_options_card.inner, "Revert changes", self._revert_cs2_options,
                                           style="secondary", height=32, pad_x=GAP)
        self.cs2_revert_btn.pack(anchor="w", pady=(GAP, 0))
        self.cs2_revert_btn.set_enabled(False)
        self.cs2_options_var.trace_add("write", self._cs2_draft_changed)
        self.cs2_options_card.fit()
        self._section("CS2 VIDEO & CONTROLS", self.settings_content).pack(fill="x", padx=PAD, pady=GAP)
        self.cs2_settings_card = RoundedCard(self.settings_content, self._content_width, fill=SURFACE,
                                            border=BORDER, radius=20, inset=GAP)
        self.cs2_settings_card.pack(padx=PAD)
        row = tk.Frame(self.cs2_settings_card.inner, bg=SURFACE)
        row.pack(fill="x")
        tk.Label(row, text="Always keep CS2 video and controls", bg=SURFACE, fg=FG,
                 font=("Segoe UI Semibold", 10), anchor="w", justify="left",
                 wraplength=self._content_width - 2 * GAP).pack(fill="x")
        tk.Label(self.cs2_settings_card.inner,
                 text="Video settings, keybinds and mouse preferences are copied automatically at every login. Close CS2 before switching. Steam Cloud stays off when settings are copied.",
                 bg=SURFACE, fg=FG_MUTED, font=("Segoe UI", 9), wraplength=self._content_width - 2 * GAP,
                 anchor="w", justify="left").pack(fill="x", pady=(GAP, 0))
        self.cs2_source_label = tk.Label(self.cs2_settings_card.inner, bg=SURFACE, fg=FG_MUTED,
                                         font=("Segoe UI", 9), wraplength=self._content_width - 2 * GAP,
                                         anchor="w", justify="left")
        self.cs2_source_label.pack(fill="x", pady=(GAP, 0))
        self.cs2_source_btn = RoundedButton(self.cs2_settings_card.inner, "Choose settings source",
                                            self._choose_cs2_source, style="secondary", height=32, pad_x=GAP)
        self.cs2_source_btn.pack(anchor="w", pady=(GAP, 0))
        tk.Label(self.cs2_settings_card.inner,
                 text="Automatic uses the previous Steam account. Choose a fixed source to always use your main account’s settings. Existing files are backed up before replacement.",
                 bg=SURFACE, fg=FG_FAINT, font=("Segoe UI", 9), wraplength=self._content_width - 2 * GAP,
                 anchor="w", justify="left").pack(fill="x", pady=(GAP, 0))
        self._refresh_cs2_source_label()
        self.cs2_settings_card.fit()
        self._section("SAVED DATA & DIAGNOSTICS", self.settings_content).pack(fill="x", padx=PAD, pady=GAP)
        self.data_card = RoundedCard(self.settings_content, self._content_width, fill=SURFACE, inset=GAP)
        self.data_card.pack(padx=PAD)
        self.data_status = tk.Label(self.data_card.inner, text=self.store.load_error or
                                    "A validated backup is kept before saved accounts or settings are updated.",
                                    bg=SURFACE, fg=ERR if self.store.load_error else FG_MUTED,
                                    font=("Segoe UI", 9), wraplength=self._content_width - 2 * GAP, anchor="w", justify="left")
        self.data_status.pack(fill="x")
        self.recover_btn = RoundedButton(self.data_card.inner, "Recover backup", self._recover_data,
                                         style="secondary", height=32, pad_x=GAP)
        self.recover_btn.pack(anchor="w", pady=(GAP, 0))
        tk.Label(self.data_card.inner, text="Diagnostics contain local error events only. Tokens, account details and page contents are excluded.",
                 bg=SURFACE, fg=FG_MUTED, font=("Segoe UI", 9), wraplength=self._content_width - 2 * GAP,
                 anchor="w", justify="left").pack(fill="x", pady=(GAP, 0))
        self.diagnostics_btn = RoundedButton(self.data_card.inner, "Copy diagnostics", self._copy_diagnostics,
                                             style="secondary", height=32, pad_x=GAP)
        self.diagnostics_btn.pack(anchor="w", pady=(GAP, 0))
        self.data_card.fit()

    def _settings_values(self):
        return self.store.effective_preferences(self._settings_account_id)

    def _edit_selected_settings(self):
        if self.current_account:
            self._change_settings_scope(self.current_account["id"])

    def _change_settings_scope(self, account_id, *, save=True):
        if self._login_thread is not None or self._extraction is not None:
            return
        if save:
            self._dismiss_import_report()
        if save and not self._save_cs2_launch_options():
            self._reveal_cs2_error()
            return
        self._settings_account_id = account_id
        values = self._settings_values()
        for name in ("private_login", "disable_cloud_sync", "use_cs2_launch_options"):
            setattr(self, name, values[name])
            getattr(self, name + "_btn").set_checked(values[name])
        self.cs2_options_var.set(values["cs2_launch_options"])
        self._refresh_cs2_source_label()
        account = self.store.get(account_id)
        self.settings_scope_note.configure(text=f"Editing {account_label(account)}. Changes override this account’s defaults."
                                            if account else "Editing defaults. Accounts without custom settings inherit these options.")
        self._refresh_login_action()
        self._refit()

    def _save_setting(self, name, value, default_save):
        if self._settings_account_id is None:
            default_save(value)
        else:
            account = self.store.get(self._settings_account_id)
            overrides = dict(account.get("preferences", {}))
            overrides[name] = value
            self.store.set_account_preferences(account["id"], overrides)

    def _refresh_cs2_source_label(self):
        source = self._settings_values()["cs2_settings_source"]
        account = next((a for a in self.store.accounts if a.get("issuer") == "steam" and a.get("subject") == source), None)
        if source and account is None and source not in self._cs2_source_names:
            self._cs2_source_names.update({item["steam_id"]: item["name"] for item in list_settings_sources()})
        label = account_label(account) if account else self._cs2_source_names.get(source, source)
        self.cs2_source_label.configure(text="Source: " + (label if source else "Previous Steam account (automatic)"))

    def _set_cs2_source(self, source):
        if self._login_thread is not None or self._extraction is not None:
            return
        self._dismiss_import_report()
        try:
            self._save_setting("cs2_settings_source", source, self.store.set_cs2_settings_source)
        except (OSError, SteamLoginError):
            self._set_status("Could not save the CS2 settings source. Saved settings are unchanged.", ERR)
        else:
            self._refresh_cs2_source_label()
            self._refresh_login_action()
            self.cs2_settings_card.fit()
            self._refit()

    def _choose_cs2_source(self):
        if self._login_thread is not None or self._extraction is not None:
            return
        previous = getattr(self, "_cs2_source_menu", None)
        if previous is not None:
            previous.destroy()
        menu = tk.Menu(self.root, tearoff=False, bg=SURFACE, fg=FG, activebackground=ACCENT,
                       activeforeground="white", font=("Segoe UI", 10))
        self._cs2_source_menu = menu
        menu.add_command(label="Previous Steam account (automatic)", command=lambda: self._set_cs2_source(""))
        seen = set()
        for account in self.store.ordered():
            try:
                claims = decode_token(account["token"])["payload"]
                source = claims.get("sub") if claims.get("iss") == "steam" else None
                if not isinstance(source, str) or not re.fullmatch(r"[0-9]{17}", source) or source in seen:
                    continue
            except (ExtractionError, AttributeError, KeyError):
                continue
            seen.add(source)
            menu.add_command(label=account_label(account), command=lambda value=source: self._set_cs2_source(value))
        sources = list_settings_sources()
        self._cs2_source_names.update({item["steam_id"]: item["name"] for item in sources})
        for item in sources:
            source = item["steam_id"]
            if source not in seen:
                seen.add(source)
                menu.add_command(label=item["name"] + " (Steam)", command=lambda value=source: self._set_cs2_source(value))
        try:
            menu.tk_popup(self.cs2_source_btn.winfo_rootx(), self.cs2_source_btn.winfo_rooty() + self.cs2_source_btn.winfo_height())
        finally:
            menu.grab_release()

    def _reset_profile(self):
        if self._settings_account_id and self._login_thread is None and self._extraction is None:
            self._dismiss_import_report()
            try:
                self.store.reset_account_preferences(self._settings_account_id)
            except OSError:
                self._set_status("Could not reset this account’s settings. Saved settings are unchanged.", ERR)
            else:
                self._change_settings_scope(self._settings_account_id, save=False)

    def _recover_data(self):
        if self._login_thread is not None or self._extraction is not None:
            return
        self._dismiss_import_report()
        source = filedialog.askopenfilename(title="Choose a JWTractor saved-data backup",
                                            initialdir=os.path.dirname(os.path.abspath(self.store.path)),
                                            initialfile=os.path.basename(self.store.path) + ".bak",
                                            filetypes=[("JWTractor backups", "*.bak *.json"), ("All files", "*.*")], parent=self.root)
        if not source:
            return
        if not messagebox.askyesno("Recover saved data", "Restore this backup? The current file will be preserved separately before replacement.", parent=self.root):
            return
        self._stop_cooldown_check()
        self._stop_presence_check()
        try:
            self.store.recover(source)
        except OSError as exc:
            self.diagnostics.record("store.recovery_failed", exc)
            self.data_status.configure(text=str(exc), fg=ERR)
        else:
            self.diagnostics.record("store.recovered")
            self.current_account = None
            self._set_output("")
            self._set_details([])
            self._account_query = ""
            self._change_settings_scope(None, save=False)
            self._restore_selection()
            self._refresh_saved()
            self._refresh_login_action()
            self.data_status.configure(text="Backup restored. The previous file was preserved in the saved-data folder.", fg=OK)
            self._set_status("Saved data recovered.", OK)
        self.data_card.fit()
        self._refit()

    def _copy_diagnostics(self):
        self._dismiss_import_report()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.diagnostics.report())
        except tk.TclError:
            self._set_status("Could not copy diagnostics. Try again when the clipboard is available.", ERR)
        else:
            self._set_status("Diagnostics copied. Tokens and account details are excluded.", OK)

    def _ui_exception(self, kind, value, traceback):
        self.diagnostics.record("ui.failed", value)
        if not self._closing:
            self._set_status("An interface error occurred. Copy diagnostics in Settings, then retry.", ERR)

    def _select_tab(self, name):
        self._dismiss_import_report()
        if name == self.active_tab:
            return "break"
        if not self._save_cs2_launch_options():
            self._reveal_cs2_error()
            return "break"
        return self._show_tab(name)

    def _show_tab(self, name):
        """Switch pages after the caller has handled any unsaved settings."""
        self._close_picker()
        self._tab_scroll[self.active_tab] = self.viewport.yview()[0]
        self.active_tab = name
        self.viewport.itemconfigure(self._content_window, window=self._pages[name])
        self.settings_btn.set_destination("accounts" if name == "settings" else "settings")
        self.subtitle.configure(text="Manage your login preferences." if name == "settings"
                                else "Extract or paste a token, then log in.")
        self._refresh_login_action()
        self._refit()
        self.viewport.yview_moveto(self._tab_scroll[name])
        self.settings_btn.focus_set()
        return "break"

    def _reveal_cs2_error(self):
        if self.active_tab != "settings":
            self._show_tab("settings")
        self.root.update_idletasks()
        height = max(1, self.settings_content.winfo_reqheight())
        self.viewport.yview_moveto(self.cs2_options_card.winfo_y() / height)
        self.cs2_launch_options_entry.focus_set()

    def _cycle_tab(self, _=None):
        return self._select_tab("settings" if self.active_tab == "accounts" else "accounts")

    # -- behaviour -----------------------------------------------------------
    def _on_drop(self, event):
        self.drop.set_hover(False)
        paths = self.root.tk.splitlist(event.data)
        if paths:
            self._start_imports(paths)

    def _browse(self):
        if self._login_thread is not None or self._extraction is not None:
            return
        self._dismiss_import_report()
        paths = filedialog.askopenfilenames(
            title="Choose executables to import",
            filetypes=[("Executables", "*.exe"), ("All files", "*.*")],
            parent=self.root,
        )
        if paths:
            self._start_imports(paths)

    def _start_extraction(self, path, extra_files=0):
        return self._start_imports([path])

    def _start_imports(self, paths):
        if self._login_thread is not None or self._extraction is not None or self._closing:
            return
        self._select_tab("accounts")
        if self.active_tab != "accounts":
            return
        self._close_picker()
        paths = list(dict.fromkeys(os.path.abspath(os.fspath(path)) for path in paths))
        if not paths:
            return
        if len(paths) > 2000:
            self._show_error("Import up to 2,000 files at a time.")
            return
        self._import_paths = paths
        self._import_results = []
        self.import_card.pack_forget()
        if len(paths) > 1:
            self.import_card.pack(after=self.drop, padx=PAD, pady=(GAP, 0))
        cancel, events = threading.Event(), queue.Queue()
        def worker():
            for index, path in enumerate(paths):
                if cancel.is_set():
                    return
                events.put(("progress", (index, path)))
                result, error = None, None
                try:
                    result = extract_from_file(path)
                except ExtractionError as exc:
                    error = str(exc)
                    self.diagnostics.record("import.failed", exc)
                except Exception as exc:
                    error = "Could not read or extract this file. Check access or paste its token instead."
                    self.diagnostics.record("import.failed", exc)
                ack = threading.Event()
                if cancel.is_set():
                    return
                events.put(("item", (path, result, error, ack)))
                while not ack.wait(0.05):
                    if cancel.is_set():
                        return
            if not cancel.is_set():
                events.put(("done", None))
        thread = threading.Thread(target=worker, name="Token extraction", daemon=True)
        self._extraction = (thread, cancel, events, paths, 0)
        self.drop.set_reading(True)
        self._refresh_login_action()
        self._set_status(f"Importing {len(paths)} file{'s' if len(paths) != 1 else ''}…")
        thread.start()
        self._extraction_poll_job = self.root.after(80, self._poll_extraction)

    def _poll_extraction(self):
        self._extraction_poll_job = None
        if self._extraction is None:
            return
        thread, cancel, events, paths, _ = self._extraction
        try:
            kind, value = events.get_nowait()
        except queue.Empty:
            self._extraction_poll_job = self.root.after(80, self._poll_extraction)
            return
        if kind == "progress":
            index, path = value
            self._set_status(f"Importing {index + 1} of {len(paths)}: {os.path.basename(path)}…")
        elif kind == "item":
            path, result, error, ack = value
            outcome = "Failed"
            if error is None:
                try:
                    self._accept_result(result, copy=False)
                    outcome = self.store.last_add_action.capitalize()
                except (OSError, ExtractionError) as exc:
                    error = str(exc) if isinstance(exc, ExtractionError) else "Could not save. Check access to saved data."
                    self.diagnostics.record("import.save_failed", exc)
            self._import_results.append((os.path.basename(path), error or outcome, error is None))
            self._render_import_report()
            ack.set()
        else:
            self._extraction = None
            self.drop.set_reading(False)
            if self._settings_account_id is not None and self.current_account:
                self._change_settings_scope(self.current_account["id"], save=False)
            self._refresh_login_action()
            successes = sum(item[2] for item in self._import_results)
            if len(paths) == 1 and successes:
                copied = self._copy(announce=False)
                self._set_status(f"Imported {os.path.basename(paths[0])} — " +
                                 ("copied · saved" if copied else "saved · clipboard unavailable; use Copy to retry"),
                                 OK if copied else FG_MUTED)
            elif len(paths) == 1:
                self._show_error(self._import_results[0][1])
            else:
                if successes:
                    self._copy(announce=False)
                self._set_status(f"Import complete: {successes} saved, {len(paths) - successes} failed. See the per-file results.",
                                 OK if successes == len(paths) else FG_MUTED)
            self._ensure_presence()
            return
        self._extraction_poll_job = self.root.after(30, self._poll_extraction)

    def _render_import_report(self):
        self.import_report.configure(state="normal")
        self.import_report.delete("1.0", "end")
        self.import_report.insert("1.0", "\n".join(f"{name} — {outcome}" for name, outcome, _ in self._import_results))
        self.import_report.configure(state="disabled", height=min(6, max(1, len(self._import_results))))
        self.import_report.see("end")
        self.import_card.fit()
        self._refit()

    def _dismiss_import_report(self):
        # Keep the live report throughout a batch, including automatic account
        # selection. Completed results last only until the next user action.
        if self._extraction is not None or not self._import_results:
            return
        self.import_card.pack_forget()
        self._import_results = []
        self._import_paths = []
        self.import_report.configure(state="normal")
        self.import_report.delete("1.0", "end")
        self.import_report.configure(state="disabled", height=1)
        if self.status.cget("text").startswith(("Import complete:", "Import cancelled.")):
            self._set_status("")
        else:
            self._refit()

    def _cancel_extraction(self):
        if self._extraction_poll_job is not None:
            self.root.after_cancel(self._extraction_poll_job)
            self._extraction_poll_job = None
        if self._extraction is not None:
            self._extraction[1].set()
            self._extraction = None
            self.drop.set_reading(False)
            if self._settings_account_id is not None and self.current_account:
                self._change_settings_scope(self.current_account["id"], save=False)
            self._refresh_login_action()
            completed = len(self._import_results)
            self._import_results.extend((os.path.basename(path), "Skipped (cancelled)", False)
                                        for path in self._import_paths[completed:])
            self._render_import_report()
            saved = sum(item[2] for item in self._import_results)
            self._set_status(f"Import cancelled. {saved} completed file{'s' if saved != 1 else ''} saved; remaining files skipped."
                             if saved else "Import cancelled. No account was added.")

    def process(self, path: str, extra_files: int = 0):
        if self._login_thread is not None or self._extraction is not None:
            return
        self._dismiss_import_report()
        self._close_picker()
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

        self._present_extracted(result, path, extra_files)

    def _present_extracted(self, result, path, extra_files=0):
        name = os.path.basename(path)
        try:
            self._accept_result(result)
        except ExtractionError as exc:
            self._show_error(str(exc))
            return
        except OSError:
            self._set_status("Couldn't save the account. Check the saved-accounts folder and try again.", ERR)
            return

        note = ""
        if len(result["all_tokens"]) > 1:
            note = f"  ·  {len(result['all_tokens'])} tokens found, showing first"
        elif extra_files:
            note = f"  ·  {extra_files} more ignored (drop one at a time)"
        copied = "copied · saved" if self._last_copy_succeeded else "saved · clipboard unavailable; use Copy to retry"
        self._set_status(f"Extracted from {name} — {copied}{note}", OK if self._last_copy_succeeded else FG_MUTED)

    def _accept_result(self, result, *, copy=True):
        # Save successfully before replacing the current account or result.
        account = self.store.add(result["token"], result["username"])
        self._display_account(account)
        self._last_copy_succeeded = self._copy(announce=False) if copy else False
        return account

    def _display_account(self, account):
        self._dismiss_import_report()
        if not self.current_account or self.current_account.get("id") != account.get("id"):
            self._claims_expanded = False
        self.current_account = account
        if self._settings_account_id is not None and self._settings_account_id != account["id"]:
            self._change_settings_scope(account["id"], save=False)
        self._set_output(account_combined(account))
        try:
            payload = decode_token(account["token"]).get("payload")
        except ExtractionError:
            payload = None
        self._set_details(summarize_claims(payload))
        self._refresh_saved()
        self._refresh_login_action()
        self._refit()

        self._ensure_presence()

    def _restore_selection(self):
        account = self.store.get(self.store.selected_account_id)
        if account is None:
            ordered = self.store.ordered()
            account = ordered[0] if ordered else None
        if account is not None:
            self._display_account(account)

    # -- direct token entry --------------------------------------------------
    def _open_token_dialog(self):
        if self._login_thread is not None or self._extraction is not None:
            return
        self._dismiss_import_report()
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
        if self._login_thread is not None or self._extraction is not None:
            raise ExtractionError("Wait for the current operation to finish before adding an account.")
        account = self._accept_result(parse_token_input(text, username))
        copied = "copied · saved" if self._last_copy_succeeded else "saved · clipboard unavailable; use Copy to retry"
        self._set_status(f"{self.store.last_add_action.capitalize()} {account_label(account)} — {copied}", OK if self._last_copy_succeeded else FG_MUTED)
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
            self.saved_btn.focus_set()
        return "break"

    def _open_picker(self):
        if self._login_thread is not None or self._extraction is not None:
            return
        if self._picker is not None:
            if hasattr(self._picker, "search"):
                self._picker.search.focus_set()
            return
        self._select_tab("accounts")
        if self.active_tab != "accounts":
            return
        accounts = self.store.ordered()
        self.root.update_idletasks()
        x = self.saved_btn.winfo_rootx()
        y = self.saved_btn.winfo_rooty() + self.saved_btn.winfo_height() + GAP
        width = self.saved_btn.winfo_width()

        top = tk.Toplevel(self.root, bg=BORDER_HI)  # bg shows as a 1px border
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        self._picker = top

        body = tk.Frame(top, bg=SURFACE)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        def fit_popup():
            top.update_idletasks()
            height = body.winfo_reqheight() + 2
            popup_x = max(0, min(x, top.winfo_screenwidth() - width))
            popup_y = y
            if popup_y + height + PAD > top.winfo_screenheight():
                above = self.saved_btn.winfo_rooty() - height - GAP
                if above >= PAD:
                    popup_y = above
            popup_y = max(0, min(popup_y, top.winfo_screenheight() - height - PAD))
            top.geometry(f"{width}x{height}+{popup_x}+{popup_y}")

        if not accounts:
            tk.Label(
                body, text="No saved accounts yet.\nExtract a token and it's kept here.",
                bg=SURFACE, fg=FG_MUTED, font=("Segoe UI", 10),
                justify="left", padx=GAP, pady=GAP,
            ).pack(fill="x", anchor="w")
        else:
            search_heading = tk.Frame(body, bg=SURFACE)
            search_heading.pack(fill="x", padx=GAP, pady=GAP)
            tk.Label(search_heading, text="Search accounts", bg=SURFACE, fg=FG,
                     font=("Segoe UI Semibold", 10)).pack(side="left")
            count = tk.Label(search_heading, bg=SURFACE, fg=FG_FAINT, font=("Segoe UI", 9))
            count.pack(side="right")
            query = tk.StringVar(master=top, value=self._account_query)
            search = tk.Entry(body, textvariable=query, bg=FIELD, fg=FG, insertbackground=FG,
                              selectbackground=ACCENT_LO, relief="flat", bd=0, font=("Segoe UI", 10),
                              highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
            search.pack(fill="x", padx=GAP, pady=(0, GAP), ipady=6)
            top.search = search
            top.query = query
            list_frame = tk.Frame(body, bg=SURFACE)
            list_frame.pack(fill="both", expand=True)
            canvas = tk.Canvas(list_frame, bg=SURFACE, highlightthickness=0, bd=0,
                               yscrollincrement=20)
            scroll = tk.Canvas(list_frame, bg=SURFACE, width=12, height=1, bd=0,
                               highlightthickness=0, cursor="hand2")
            scroll.pack(side="right", fill="y")
            def paint_scroll(first, last):
                first, last = float(first), float(last)
                scroll.delete("all")
                height = max(1, scroll.winfo_height())
                if last - first < 0.999:
                    bottom = min(height, max(first * height + 12, last * height))
                    scroll.create_polygon(_round_rect(3, first * height, 9, bottom, 3),
                                          smooth=True, fill=BORDER_HI, outline="")
            canvas.configure(yscrollcommand=paint_scroll)
            scroll.bind("<Configure>", lambda _: paint_scroll(*canvas.yview()))
            def drag_scroll(event):
                first, last = canvas.yview()
                canvas.yview_moveto(event.y / max(1, scroll.winfo_height()) - (last - first) / 2)
            scroll.bind("<Button-1>", drag_scroll)
            scroll.bind("<B1-Motion>", drag_scroll)
            inner = tk.Frame(canvas, bg=SURFACE)
            canvas.create_window(0, 0, window=inner, anchor="nw", width=width - 14)
            canvas.pack(side="left", fill="both", expand=True)
            max_view_h = min(360, max(1, self.root.winfo_screenheight() - 240))
            view_h = 1
            top.rows = []
            top.matches = []

            def focus_row(index):
                if not top.rows:
                    return "break"
                if index < 0:
                    search.focus_set()
                    return "break"
                row = top.rows[min(index, len(top.rows) - 1)]
                row.focus_set()
                inner.update_idletasks()
                first, last = canvas.yview()
                content_h = max(1, inner.winfo_reqheight())
                row_top, row_bottom = row.winfo_y(), row.winfo_y() + row.winfo_height()
                if row_top < first * content_h:
                    canvas.yview_moveto(row_top / content_h)
                elif row_bottom > last * content_h:
                    canvas.yview_moveto((row_bottom - view_h) / content_h)
                return "break"

            def render_matches(*_):
                nonlocal view_h
                if self._picker is not top:
                    return
                self._account_query = query.get()
                for child in inner.winfo_children():
                    child.destroy()
                terms = query.get().casefold().split()
                top.matches = [acc for acc in accounts if all(term in " ".join(
                    str(acc.get(key) or "") for key in ("alias", "username", "subject", "issuer")
                ).casefold() for term in terms)]
                count.configure(text=f"{len(top.matches)} of {len(accounts)}")
                top.rows = []
                for index, acc in enumerate(top.matches):
                    row = self._build_account_row(inner, acc)
                    top.rows.append(row)
                    row.bind("<Down>", lambda _, i=index: focus_row(i + 1))
                    row.bind("<Up>", lambda _, i=index: focus_row(i - 1))
                if not top.matches:
                    tk.Label(inner, text="No matching accounts. Try a name or SteamID.", bg=SURFACE,
                             fg=FG_MUTED, font=("Segoe UI", 10), padx=GAP, pady=GAP,
                             anchor="w").pack(fill="x")
                inner.update_idletasks()
                content_h = inner.winfo_reqheight()
                view_h = min(content_h, max_view_h)
                canvas.configure(height=view_h, scrollregion=(0, 0, width - 14, content_h))
                canvas.yview_moveto(0)
                fit_popup()

            query.trace_add("write", render_matches)
            render_matches()
            search.bind("<Down>", lambda _: focus_row(0))
            search.bind("<Return>", lambda _: (self._use_account(top.matches[0]) if top.matches else None, "break")[1])
            top.bind("<MouseWheel>", lambda e: (canvas.yview_scroll(
                (-1 if e.delta > 0 else 1) * max(1, abs(e.delta) // 120) * 2, "units"), "break")[1])

        fit_popup()
        top.bind("<Escape>", self._close_picker)
        top.focus_force()
        def dismiss_if_outside():
            if self._picker is not top:
                return
            focused = top.focus_get()
            if focused is None or (focused is not top and not str(focused).startswith(str(top) + ".")):
                self._close_picker()
        top.bind("<FocusOut>", lambda _: self.root.after_idle(dismiss_if_outside))
        if accounts:
            top.after_idle(lambda: search.focus_set() if self._picker is top else None)

    def _build_account_row(self, parent, acc):
        row = tk.Frame(parent, bg=SURFACE, takefocus=True)
        row.pack(fill="x")

        actions = tk.Frame(row, bg=SURFACE)
        actions.pack(side="right", padx=GAP)
        text = tk.Frame(row, bg=SURFACE)
        text.pack(side="left", fill="x", expand=True, padx=(GAP, 0), pady=GAP)
        name_lbl = tk.Label(
            text, text=account_label(acc), bg=SURFACE, fg=FG,
            font=("Segoe UI Semibold", 11), anchor="w", wraplength=320, justify="left",
        )
        name_lbl.pack(anchor="w")
        parts = []
        if (acc.get("alias") or "").strip() and acc.get("username"):
            parts.append(acc["username"])  # show the real username when aliased
        if acc.get("issuer"):
            parts.append(str(acc["issuer"]))
        if self.current_account and self.current_account.get("id") == acc.get("id"):
            parts.append("Selected")
        if account_status(acc) == "expired":
            parts.append("Token expired")
        if self._observed_session and self._observed_session[0] == acc.get("subject"):
            parts.append("Signed in on this PC")
        presence = self._presence.get(acc.get("subject"))
        if presence and presence["state"] in ("online", "in-game") and time.time() - presence["checked_at"] <= 90:
            parts.append("Public: " + presence["message"])
        sub_lbl = tk.Label(
            text, text="  ·  ".join(parts) or "—", bg=SURFACE, fg=FG_MUTED,
            font=("Segoe UI", 9), anchor="w", wraplength=320, justify="left",
        )
        sub_lbl.pack(anchor="w", pady=(GAP, 0))

        rename_lbl = tk.Label(actions, text="Rename", bg=SURFACE, fg=FG_MUTED,
                              font=("Segoe UI", 9), cursor="hand2", takefocus=True)
        rename_lbl.pack(side="left", padx=(0, GAP))
        del_lbl = tk.Label(actions, text="Delete", bg=SURFACE, fg=FG_MUTED,
                           font=("Segoe UI", 9), cursor="hand2", takefocus=True)
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
        row.bind("<Return>", lambda e: (self._use_account(acc), "break")[1])
        row.bind("<space>", lambda e: (self._use_account(acc), "break")[1])
        row.bind("<FocusIn>", lambda _: name_lbl.configure(fg=ACCENT_HI))
        row.bind("<FocusOut>", lambda _: name_lbl.configure(fg=FG))
        rename_lbl.bind("<Button-1>", lambda e, a=acc: self._rename_account(a))
        del_lbl.bind("<Button-1>", lambda e, a=acc: self._delete_account(a))
        for widget, action in ((rename_lbl, self._rename_account), (del_lbl, self._delete_account)):
            widget.bind("<Return>", lambda e, action=action: (action(acc), "break")[1])
            widget.bind("<space>", lambda e, action=action: (action(acc), "break")[1])
            widget.bind("<FocusIn>", lambda e: e.widget.configure(fg=ACCENT_HI))
            widget.bind("<FocusOut>", lambda e: e.widget.configure(fg=FG_MUTED))
        for w in (rename_lbl, del_lbl):
            w.bind("<Enter>", lambda e, ww=w: ww.configure(fg=FG), add="+")
            w.bind("<Leave>", lambda e, ww=w: ww.configure(fg=FG_MUTED), add="+")
        return row

    def _use_account(self, acc):
        if self._login_thread is not None or self._extraction is not None:
            return
        self._close_picker()
        self._display_account(acc)
        try:
            self.store.touch(acc["id"])
        except OSError:
            self._set_status(f"Loaded {account_label(acc)}. Could not save the last-used order.", ERR)
            self._copy(announce=False)
            return
        self._refresh_saved()
        self._refit()
        copied = self._copy(announce=False)
        self._set_status(f"Loaded {account_label(acc)} — " + ("copied" if copied else "clipboard unavailable; use Copy to retry"),
                         OK if copied else FG_MUTED)

    def _rename_account(self, acc):
        if self._login_thread is not None or self._extraction is not None:
            return
        self._dismiss_import_report()
        if self._rename_dialog is not None:
            self._rename_dialog.lift()
            self._rename_dialog.alias.focus_set()
            return
        self._close_picker()
        def save(alias):
            self.store.set_alias(acc["id"], alias)
            self._refresh_saved()
            if self.current_account and self.current_account.get("id") == acc["id"]:
                self._refresh_cooldown_details()
                self._refresh_saved()
        self._rename_dialog = RenameDialog(self.root, acc, save, self._rename_dialog_closed)

    def _rename_dialog_closed(self):
        self._rename_dialog = None
        self._open_picker()

    def _delete_account(self, acc):
        if self._login_thread is not None or self._extraction is not None:
            return
        self._dismiss_import_report()
        self._close_picker()
        if messagebox.askyesno(
            "Delete account",
            f"Forget “{account_label(acc)}”?\n\n"
            "This removes the saved token from JWTractor. It doesn't affect the "
            "account itself.",
            parent=self.root, icon="warning",
        ):
            try:
                self.store.remove(acc["id"])
            except OSError:
                self._set_status("Could not delete the saved account. Check access to the accounts file.", ERR)
                self._open_picker()
                return
            if self.current_account and self.current_account.get("id") == acc["id"]:
                self.current_account = None
                self._change_settings_scope(None, save=False)
                self._set_output("")
                self._set_details([])
                self._refresh_login_action()
                self._set_status("Saved account removed. Select another account or add a token.")
            else:
                self._set_status(f"Removed {account_label(acc)} from saved accounts.")
            self._refresh_saved()
        self._open_picker()

    # -- Steam login ---------------------------------------------------------
    def _toggle_private_login(self):
        self._toggle_login_option("private_login", self.private_login_btn, self.store.set_private_login)

    def _toggle_disable_cloud_sync(self):
        self._toggle_login_option("disable_cloud_sync", self.disable_cloud_sync_btn, self.store.set_disable_cloud_sync)

    def _toggle_use_cs2_launch_options(self):
        if self._login_thread is None and self._extraction is None and self._save_cs2_launch_options():
            self._toggle_login_option("use_cs2_launch_options", self.use_cs2_launch_options_btn,
                                      self.store.set_use_cs2_launch_options)

    def _save_cs2_launch_options_on_enter(self, _=None):
        self._save_cs2_launch_options()
        return "break"

    def _cs2_draft_changed(self, *_):
        changed = self.cs2_options_var.get() != self._settings_values()["cs2_launch_options"]
        self.cs2_revert_btn.set_enabled(changed and self._login_thread is None and self._extraction is None)
        self.cs2_options_status.configure(text="Unsaved changes. Press Enter or leave the field to save." if changed
                                           else "Saved. Applies on your next login when enabled.", fg=FG_FAINT)
        self.cs2_options_field._border = ACCENT if self.root.focus_get() is self.cs2_launch_options_entry else BORDER
        self.cs2_options_field.fit()
        self.cs2_options_card.fit()

    def _revert_cs2_options(self):
        if self._login_thread is None and self._extraction is None:
            self.cs2_options_var.set(self._settings_values()["cs2_launch_options"])
            self._save_cs2_launch_options()
            self.cs2_launch_options_entry.focus_set()

    def _focus_cs2_options(self, focused):
        if self.cs2_options_field._border != ERR:
            self.cs2_options_field._border = ACCENT if focused else BORDER
            self.cs2_options_field.fit()

    def _save_cs2_launch_options(self):
        if self._login_thread is not None or self._extraction is not None:
            return True
        options = self.cs2_launch_options_entry.get()
        try:
            if options != self._settings_values()["cs2_launch_options"]:
                self._save_setting("cs2_launch_options", options, self.store.set_cs2_launch_options)
        except (OSError, SteamLoginError) as exc:
            message = str(exc) if isinstance(exc, SteamLoginError) else "Could not save. Check access to the accounts file."
            self.cs2_options_status.configure(text=message, fg=ERR)
            self.cs2_options_field._border = ERR
            saved = False
        else:
            self.cs2_options_status.configure(text="Saved. Applies on your next login when enabled.", fg=FG_FAINT)
            self.cs2_options_field._border = ACCENT if self.root.focus_get() is self.cs2_launch_options_entry else BORDER
            saved = True
        self.cs2_revert_btn.set_enabled(not saved)
        self.cs2_options_field.fit()
        self.cs2_options_card.fit()
        self._refit()
        return saved

    def _toggle_login_option(self, name, button, save):
        if self._login_thread is not None or self._extraction is not None:
            return
        self._dismiss_import_report()
        try:
            self._save_setting(name, not getattr(self, name), save)
        except OSError:
            self._set_status("Could not save the login option. Please check access to the accounts file.", ERR)
            self._refit()
            return
        setattr(self, name, self._settings_values()[name])
        button.set_checked(getattr(self, name))
        self._refresh_login_action()

    def _refresh_login_action(self):
        busy = self._login_thread is not None or self._extraction is not None
        cancelling = self._login_thread is not None and self._login_cancel.is_set()
        self.login_btn.set_text("Cancelling…" if cancelling else "Cancel import" if self._extraction is not None else
                                "Cancel login" if busy else "Log in to Steam")
        self.login_btn.set_enabled(not cancelling and not self._closing and
                                   (busy or (os.name == "nt" and self.current_account is not None)))
        self.saved_btn.set_enabled(not busy)
        self.paste_btn.set_enabled(not busy)
        self.copy_btn.set_enabled(bool(self.result_text) and not busy)
        self.drop.set_enabled(not busy)
        self.private_login_btn.set_enabled(not busy and os.name == "nt")
        self.disable_cloud_sync_btn.set_enabled(not busy and os.name == "nt")
        self.use_cs2_launch_options_btn.set_enabled(not busy and os.name == "nt")
        self.cs2_source_btn.set_enabled(not busy and os.name == "nt")
        self.cs2_launch_options_entry.configure(state="disabled" if busy else "normal")
        self.cs2_revert_btn.set_enabled(not busy and self.cs2_options_var.get() != self._settings_values()["cs2_launch_options"])
        self.defaults_scope_btn.set_enabled(not busy)
        self.account_scope_btn.set_enabled(not busy and self.current_account is not None)
        profile = self.store.get(self._settings_account_id)
        self.reset_profile_btn.set_enabled(not busy and bool(profile and profile.get("preferences")))
        self.recover_btn.set_enabled(not busy)
        if hasattr(self, "presence_btn") and self.presence_btn.winfo_exists():
            self.presence_btn.set_enabled(not busy)
        if self.cooldown_btn is not None and self.cooldown_btn.winfo_exists():
            self.cooldown_btn.set_enabled(not busy)
        if self.active_tab == "accounts":
            self.paste_btn.pack(side="right", before=self.header_titles)
        else:
            self.paste_btn.pack_forget()
        # Keep cancellation accessible when viewing Settings during a login.
        if (self.active_tab == "accounts" and self.current_account is not None) or busy:
            self.actions.pack(fill="x", **({"before": self.status} if self.status.winfo_manager() else {}))
        else:
            self.actions.pack_forget()
        if self.active_tab == "accounts" and self.current_account is not None:
            effective = self.store.effective_preferences(self.current_account["id"])
            options = []
            if effective["disable_cloud_sync"]:
                options.append("Cloud off")
            if effective["private_login"]:
                options.extend(("Friends offline", "Remote Play off"))
            if effective["use_cs2_launch_options"]:
                options.append("Custom CS2 options" if effective["cs2_launch_options"] else "Clear CS2 options")
            options.append("Keep CS2 video & controls")
            self.login_summary.configure(text="Next login: " + (" · ".join(options) if options else "Steam’s current settings"))
            self.login_summary.pack(before=self.actions, fill="x", pady=(0, GAP))
        else:
            self.login_summary.pack_forget()
        self._fit_footer_spacing()

    def _fit_footer_spacing(self):
        has_actions = bool(self.actions.winfo_manager())
        if self.status.winfo_manager():
            self.status.pack_configure(pady=(GAP if has_actions else 0, 0))
        has_content = has_actions or bool(self.status.winfo_manager())
        if has_content:
            self.footer.pack(side="bottom", before=self.viewport, fill="x", padx=PAD, pady=(GAP, PAD))
        else:
            # Empty Tk frames retain their previous requested height. Hide the
            # footer so Settings does not inherit a blank actions-sized gap.
            self.footer.pack_forget()

    def _login_to_steam(self):
        if self._extraction is not None:
            self._cancel_extraction()
            return
        if self._login_thread is not None:
            self._login_cancel.set()
            self._refresh_login_action()
            self._set_status("Cancelling Steam login…")
            return
        if self.current_account is None:
            return
        self._dismiss_import_report()
        account = dict(self.current_account)
        try:
            # Use the original login name, never the friendly display alias.
            validate_account_name(account.get("username"))
            validate_token(account.get("token"))
        except SteamLoginError as exc:
            self._set_status(str(exc), ERR)
            self._refit()
            return
        if not self._save_cs2_launch_options():
            self._reveal_cs2_error()
            return
        self._stop_cooldown_check()
        self._refresh_cooldown_details()
        self._close_picker()
        self._login_cancel.clear()
        effective = self.store.effective_preferences(account["id"])
        cs2_options = effective["cs2_launch_options"] if effective["use_cs2_launch_options"] else None
        self._login_thread = threading.Thread(target=self._login_worker,
                                              args=(account, effective["private_login"], effective["disable_cloud_sync"], cs2_options,
                                                    effective["cs2_settings_source"]), name="Steam login")
        self._refresh_login_action()
        self._set_status("Preparing Steam login…")
        self._login_thread.start()
        self._login_poll_job = self.root.after(80, self._drain_login_events)

    def _login_worker(self, account, private_login=False, disable_cloud_sync=False, cs2_launch_options=None,
                      cs2_settings_source=""):
        try:
            settings = {"cs2_settings_source": cs2_settings_source} if cs2_settings_source else {}
            result = login_account(account["username"], account["token"], cancel=self._login_cancel,
                                   progress=lambda message: self._login_events.put(("progress", message)),
                                   private_login=private_login, disable_cloud_sync=disable_cloud_sync,
                                   cs2_launch_options=cs2_launch_options, **settings)
            self._login_events.put(("done", result))
        except LoginCancelled as exc:
            self.diagnostics.record("login.cancelled", exc)
            self._login_events.put(("cancelled", str(exc)))
        except SteamLoginError as exc:
            self.diagnostics.record("login.failed", exc)
            self._login_events.put(("error", str(exc)))
        except Exception as exc:
            self.diagnostics.record("login.failed", exc)
            self._login_events.put(("error", "Unexpected Steam login failure. Check Steam and its configuration backups before retrying."))

    def _drain_login_events(self):
        self._login_poll_job = None
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
                cloud = " Steam Cloud Sync off configured." if value.get("disable_cloud_sync") else ""
                if value.get("cs2_launch_options_applied"):
                    cloud += " CS2 launch options configured."
                if value.get("cs2_settings_applied"):
                    if value.get("cs2_settings_state") == "same_account":
                        cloud += " CS2 source is this account; no settings copied. Choose another source to transfer settings."
                    else:
                        cloud += (" CS2 video and controls copied." if value.get("cs2_settings_copied")
                                  else " CS2 video and controls already match the source.")
                elif value.get("cs2_settings_state") == "unavailable":
                    cloud += " No previous CS2 settings available to copy."
                if sign_in == "rejected":
                    self._set_status(f"Steam rejected sign-in: {value.get('reason') or 'Login rejected'}. Check the session with the account owner.", ERR)
                elif sign_in == "other_account":
                    self._set_status("Steam signed in to a different account. Select the account you added in Steam.", "#facc15")
                elif sign_in == "confirmed":
                    options = " Friends & Chat offline and Remote Play off configured." if value.get("private_login") else ""
                    self._set_status(f"Signed in to Steam.{options}{cloud} {preserved}", OK)
                    steam_id = value.get("steam_id")
                    if steam_id:
                        self._check_cooldown(steam_id, automatic=True)
                elif value.get("warning"):
                    self._set_status(f"Steam launched.{cloud} {value['warning']}", "#facc15")
                else:
                    self._set_status(f"Steam launched; sign-in wasn't confirmed.{cloud} Check the client. {preserved}", FG_MUTED)
            else:
                self._set_status(value, FG_MUTED if kind == "cancelled" else ERR)
            self._refit()
        if self._login_thread is not None:
            self._login_poll_job = self.root.after(80, self._drain_login_events)

    def _request_close(self, *_):
        self._cancel_extraction()
        self._stop_cooldown_check()
        if self._login_thread is not None:
            self._closing = True
            self._login_cancel.set()
            self._refresh_login_action()
            self._set_status("Cancelling Steam login before closing…")
            return
        if self._save_cs2_launch_options():
            self.root.destroy()
        else:
            self._reveal_cs2_error()

    # -- output helpers ------------------------------------------------------
    def _set_output(self, text: str):
        self.result_text = text
        self.drop.set_compact(bool(text))
        self.copy_btn.set_text("Copy")
        self.copy_btn.set_enabled(bool(text))
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        if text:
            self.output.insert("1.0", text)
            self.output.configure(fg=FG)
        else:
            self.output.insert("1.0", "Extract a file or paste a token to select an account.")
            self.output.configure(fg=FG_FAINT)
        self.output.configure(state="disabled", takefocus=bool(text))
        self.output.yview_moveto(0)
        self._reflow_output()
        if text:
            self.token_heading.pack(fill="x", padx=PAD, pady=GAP)
            self.result_card.pack(padx=PAD)
        else:
            self.token_heading.pack_forget()
            self.result_card.pack_forget()

    def _reflow_output(self):
        """Grow the result Text to fit its wrapped content, then fit its card.

        The line count is derived from the *measured* monospace character width
        rather than the widget's laid-out state, so it is correct on any machine
        and doesn't depend on when this runs. A small margin rounds the wrap
        width down, so the box is never a line too short (never clips).
        """
        text = self.output.get("1.0", "end-1c")
        char_w = max(1, self._mono.measure("0"))
        avail = self._content_width - 2 * self.result_card.inset - 2 * self._text_padx - 2
        per_line = max(1, int(avail // char_w))
        lines = (len(text) + per_line - 1) // per_line  # ceil division
        self.output.configure(height=max(1, min(lines, 12)))
        self.result_card.fit()

    def _set_details(self, rows: list[tuple[str, str]]):
        self._detail_rows = rows
        self.cooldown_btn = None
        for child in self.details_inner.winfo_children():
            child.destroy()
        self.details_inner.columnconfigure(0, weight=0)
        self.details_inner.columnconfigure(1, weight=1)
        if self.current_account:
            account = self.current_account
            heading = tk.Frame(self.details_inner, bg=SURFACE)
            heading.grid(row=0, column=0, columnspan=2, sticky="ew")
            tk.Label(heading, text=account_label(account), bg=SURFACE, fg=FG,
                     font=("Segoe UI Semibold", 12), anchor="w", justify="left",
                     wraplength=self._content_width - 2 * GAP).pack(fill="x")
            tk.Label(heading, text=f"Steam login: {account['username']}", bg=SURFACE, fg=FG_MUTED,
                     font=("Segoe UI", 9), anchor="w", justify="left",
                     wraplength=self._content_width - 2 * GAP).pack(fill="x", pady=(GAP, 0))
            try:
                validate_account_name(account.get("username"))
                validate_token(account.get("token"))
            except SteamLoginError as exc:
                readiness, color = str(exc), ERR
            else:
                readiness, color = "Ready to log in. Steam will confirm the session.", FG_MUTED
            if os.name != "nt":
                readiness, color = "Steam login is available on Windows. You can still inspect and copy tokens.", FG_MUTED
            self.login_readiness = tk.Label(heading, text=readiness, bg=SURFACE, fg=color,
                                            font=("Segoe UI", 9), anchor="w", justify="left",
                                            wraplength=self._content_width - 2 * GAP)
            self.login_readiness.pack(fill="x", pady=(GAP, 0))
            self.local_session_label = tk.Label(heading, bg=SURFACE, font=("Segoe UI", 9),
                                                anchor="w", justify="left", wraplength=self._content_width - 2 * GAP)
            self.local_session_label.pack(fill="x", pady=(GAP, 0))
            self.public_presence_label = tk.Label(heading, bg=SURFACE, font=("Segoe UI", 9),
                                                  anchor="w", justify="left", wraplength=self._content_width - 2 * GAP)
            self.public_presence_label.pack(fill="x", pady=(GAP, 0))
            self._update_session_labels()
            self._update_presence_label()
            self._build_cooldown_details(1)
            detail_actions = tk.Frame(self.details_inner, bg=SURFACE)
            detail_actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(GAP, 0))
            self.claims_btn = RoundedButton(detail_actions,
                                           "Hide token details" if self._claims_expanded else "Show token details",
                                           self._toggle_claims, style="secondary", height=32, pad_x=GAP)
            self.claims_btn.pack(side="left")
            self.presence_btn = RoundedButton(detail_actions, "Refresh online status", lambda: self._ensure_presence(force=True),
                                              style="secondary", height=32, pad_x=GAP)
            self.presence_btn.pack(side="right")
            self.presence_btn.set_enabled(self._login_thread is None and self._extraction is None)
        else:
            tk.Label(
                self.details_inner,
                text="Choose a saved account, import an executable, or paste a token to get started.",
                bg=SURFACE, fg=FG_MUTED, font=("Segoe UI", 10), anchor="w", justify="left",
                wraplength=self._content_width - 2 * GAP,
            ).grid(row=0, column=0, columnspan=2, sticky="ew")
        if self._claims_expanded:
            _WARN_LABELS = {"Status", "Revoked if", "Note"}
            label_font = tkfont.Font(root=self.root, family="Segoe UI", size=10)
            label_width = max((label_font.measure(label) for label, _ in rows), default=0)
            value_width = max(120, self._content_width - 3 * GAP - label_width)
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
                ).grid(row=i + 3, column=0, sticky="nw", padx=(0, GAP), pady=(GAP, 0))
                tk.Label(
                    self.details_inner, text=value, bg=SURFACE, fg=val_fg,
                    font=("Segoe UI Semibold", 10),
                    wraplength=value_width, justify="left", anchor="w",
                ).grid(row=i + 3, column=1, sticky="ew", pady=(GAP, 0))
        self.details_card.fit()

    def _toggle_claims(self):
        self._dismiss_import_report()
        self._claims_expanded = not self._claims_expanded
        self._refresh_cooldown_details()
        self.claims_btn.focus_set()

    def _current_steam_id(self):
        if self.current_account is None:
            return None
        try:
            payload = decode_token(self.current_account["token"]).get("payload")
            steam_id = payload.get("sub") if isinstance(payload, dict) else None
        except (ExtractionError, KeyError):
            return None
        return steam_id if isinstance(steam_id, str) and re.fullmatch(r"[0-9]{17}", steam_id) else None

    def _build_cooldown_details(self, row):
        steam_id = self._current_steam_id()
        if steam_id is None:
            return
        panel = tk.Frame(self.details_inner, bg=SURFACE)
        panel.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(GAP, 0))
        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x", pady=(0, GAP))
        heading = tk.Frame(panel, bg=SURFACE)
        heading.pack(fill="x")
        tk.Label(heading, text="CS2 matchmaking cooldown", bg=SURFACE, fg=FG,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        checking = self._cooldown_check is not None and self._cooldown_check[0] == steam_id
        self.cooldown_btn = button = RoundedButton(heading, "Cancel check" if checking else "Refresh data",
                               self._check_cooldown, style="secondary", height=32, min_width=140, pad_x=GAP)
        button.pack(side="right")
        button.set_enabled(self._login_thread is None and self._extraction is None and not self._closing)
        cached = self.store.cooldowns.get(steam_id)
        message, color = "Not checked yet", FG_MUTED
        if cached:
            message = cached["message"]
            color = ERR if cached["state"] == "active" else OK
            if cached.get("expires_at") and cached["expires_at"] <= time.time():
                message, color = "Recorded cooldown has elapsed — refresh to confirm.", FG_MUTED
        tk.Label(panel, text=message, bg=SURFACE, fg=color, font=("Segoe UI", 10),
                 anchor="w", justify="left", wraplength=self._content_width - 2 * GAP).pack(fill="x", pady=(GAP, 0))
        if cached:
            checked = datetime.fromtimestamp(cached["checked_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            note = f"Last refreshed: {checked}. Updates when this account logs into Steam."
        else:
            note = "Checks automatically when this account is logged into Steam."
        tk.Label(panel, text=note, bg=SURFACE, fg=FG_FAINT, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=self._content_width - 2 * GAP).pack(fill="x", pady=(GAP, 0))
        notice = self._cooldown_notice.get(steam_id)
        if notice:
            tk.Label(panel, text=notice, bg=SURFACE, fg=FG_MUTED, font=("Segoe UI", 9),
                     anchor="w", justify="left", wraplength=self._content_width - 2 * GAP).pack(fill="x", pady=(GAP, 0))

    def _refresh_cooldown_details(self):
        self._set_details(self._detail_rows)
        self._refit()

    def _check_cooldown(self, steam_id=None, *, automatic=False):
        steam_id = steam_id or self._current_steam_id()
        if steam_id is None or self._login_thread is not None or self._extraction is not None or self._closing:
            return
        if not automatic:
            self._dismiss_import_report()
        if self._cooldown_check is not None:
            if automatic:
                return
            previous_id = self._cooldown_check[0]
            self._stop_cooldown_check()
            if previous_id == steam_id:
                self._refresh_cooldown_details()
                return
        session = current_steam_session()
        if session is not None and session[0] == steam_id:
            self._steam_session_seen = session
        cancel, events = threading.Event(), queue.Queue()
        def worker():
            try:
                result = check_client_cooldown(steam_id, cancel=cancel)
            except Exception as exc:
                self.diagnostics.record("cooldown.failed", exc)
                result = {"state": "unknown", "message": "Could not read the cooldown from Steam. Retry after Steam finishes loading."}
            if not cancel.is_set():
                events.put(result)
        thread = threading.Thread(target=worker, name="Steam cooldown check", daemon=True)
        self._cooldown_check = (steam_id, thread, cancel, events)
        self._cooldown_notice[steam_id] = "Checking the account already logged into Steam…"
        thread.start()
        self._refresh_cooldown_details()
        self._cooldown_poll_job = self.root.after(100, self._poll_cooldown_check)

    def _poll_cooldown_check(self):
        self._cooldown_poll_job = None
        if self._cooldown_check is None:
            return
        steam_id, thread, cancel, events = self._cooldown_check
        try:
            result = events.get_nowait()
        except queue.Empty:
            self._cooldown_poll_job = self.root.after(100, self._poll_cooldown_check)
            return
        self._cooldown_check = None
        if result.get("state") in ("active", "clear"):
            try:
                self.store.set_cooldown(steam_id, result)
            except (OSError, ValueError) as exc:
                self.diagnostics.record("cooldown.failed", exc)
                self._cooldown_notice[steam_id] = "Check completed, but could not save it. Check access to the accounts file."
            else:
                self._cooldown_notice.pop(steam_id, None)
        else:
            self.diagnostics.record("cooldown.failed")
            self._cooldown_notice[steam_id] = result.get("message", "Could not read Steam’s page.")
        self._refresh_cooldown_details()

    def _stop_cooldown_check(self):
        if self._cooldown_poll_job is not None:
            self.root.after_cancel(self._cooldown_poll_job)
            self._cooldown_poll_job = None
        if self._cooldown_check is None:
            return
        steam_id, thread, cancel, events = self._cooldown_check
        cancel.set()
        self._cooldown_check = None
        self._cooldown_notice[steam_id] = "Check cancelled. Saved data is unchanged."

    def _watch_steam_session(self):
        if self._steam_watch_job is not None:
            self.root.after_cancel(self._steam_watch_job)
        self._steam_watch_job = None
        if self._closing:
            return
        session = current_steam_session()
        changed = session != self._observed_session
        self._observed_session = session
        self._update_session_labels()
        if changed:
            self.details_card.fit()
            self._refit()
        self._ensure_presence()
        if session != self._steam_session_seen and self._login_thread is None and self._cooldown_check is None:
            if session is None:
                self._steam_session_seen = None
            if session is not None:
                steam_id = session[0]
                known = False
                for account in self.store.accounts:
                    try:
                        payload = decode_token(account["token"]).get("payload")
                        if isinstance(payload, dict) and payload.get("sub") == steam_id:
                            known = True
                            break
                    except (ExtractionError, KeyError):
                        continue
                if known:
                    self._check_cooldown(steam_id, automatic=True)
        self._steam_watch_job = self.root.after(3000, self._watch_steam_session)

    def _update_session_labels(self):
        session = self._observed_session
        if session:
            active = next((a for a in self.store.accounts if a.get("subject") == session[0]), None)
            self.steam_status.configure(text="Steam: " + (account_label(active) if active else session[0]), fg=OK)
        else:
            self.steam_status.configure(text="Steam: no active sign-in detected", fg=FG_MUTED)
        label = getattr(self, "local_session_label", None)
        if label is not None and label.winfo_exists():
            selected_id = self._current_steam_id()
            matching = bool(session and session[0] == selected_id)
            label.configure(text="This PC: Signed into this account" if matching else
                            "This PC: Steam is signed into another account" if session else "This PC: No active Steam sign-in detected",
                            fg=OK if matching else FG_MUTED)

    def _update_presence_label(self):
        label = getattr(self, "public_presence_label", None)
        if label is None or not label.winfo_exists():
            return
        steam_id = self._current_steam_id()
        result = self._presence.get(steam_id)
        checking = self._presence_worker and self._presence_worker[0] == steam_id
        if result:
            age = max(0, int(time.time()) - result.get("checked_at", 0))
            message = result["message"] + f" · checked {age}s ago"
            color = OK if result["state"] in ("online", "in-game") and age <= 90 else FG_MUTED
            if age > 90:
                message = "Last known: " + message + " (stale)"
        else:
            message, color = "Checking…" if checking else "Not checked", FG_MUTED
        if checking and result:
            message += " · refreshing…"
        label.configure(text="Public Friends status: " + message, fg=color)

    def _ensure_presence(self, *, force=False):
        steam_id = self._current_steam_id()
        if steam_id is None or self._closing or self._extraction is not None or self._login_thread is not None:
            return
        if force:
            self._dismiss_import_report()
        cached = self._presence.get(steam_id)
        if not force and cached and time.time() - cached.get("checked_at", 0) < 60:
            self._update_presence_label()
            return
        if self._presence_worker is not None:
            if self._presence_worker[0] == steam_id:
                return
            self._stop_presence_check()
        cancel, events = threading.Event(), queue.Queue()
        def worker():
            try:
                result = fetch_presence(steam_id)
            except Exception as exc:
                self.diagnostics.record("presence.failed", exc)
                result = {"state": "unknown", "message": "Public presence unavailable."}
            if not cancel.is_set():
                events.put(dict(result, checked_at=int(time.time())))
        thread = threading.Thread(target=worker, name="Public Steam presence", daemon=True)
        self._presence_worker = (steam_id, cancel, events, thread)
        self._update_presence_label()
        thread.start()
        self._presence_poll_job = self.root.after(100, self._poll_presence)

    def _poll_presence(self):
        if self._presence_poll_job is not None:
            self.root.after_cancel(self._presence_poll_job)
        self._presence_poll_job = None
        if self._presence_worker is None:
            return
        steam_id, cancel, events, thread = self._presence_worker
        try:
            result = events.get_nowait()
        except queue.Empty:
            self._presence_poll_job = self.root.after(100, self._poll_presence)
            return
        self._presence_worker = None
        self._presence[steam_id] = result
        if result.get("state") == "unknown":
            self.diagnostics.record("presence.failed")
        self._update_presence_label()
        self.details_card.fit()
        self._refit()

    def _stop_presence_check(self):
        if self._presence_poll_job is not None:
            self.root.after_cancel(self._presence_poll_job)
            self._presence_poll_job = None
        if self._presence_worker is not None:
            self._presence_worker[1].set()
            self._presence_worker = None

    def _stop_steam_watch(self, event):
        if event.widget is self.root:
            if self._resize_job is not None:
                self.root.after_cancel(self._resize_job)
                self._resize_job = None
            self._stop_presence_check()
            self.diagnostics.close()
            if self._login_poll_job is not None:
                self.root.after_cancel(self._login_poll_job)
                self._login_poll_job = None
            if self._extraction_poll_job is not None:
                self.root.after_cancel(self._extraction_poll_job)
                self._extraction_poll_job = None
            if self._extraction is not None:
                self._extraction[1].set()
                self._extraction = None
            if self._steam_watch_job is not None:
                self.root.after_cancel(self._steam_watch_job)
                self._steam_watch_job = None
            self._stop_cooldown_check()
            if self._copy_reset_job is not None:
                self.root.after_cancel(self._copy_reset_job)
                self._copy_reset_job = None

    def _show_error(self, message: str):
        if self.current_account is not None:
            message += " Selected account unchanged."
        self._set_status(message, ERR)

    def _set_status(self, text: str, color: str = FG_MUTED):
        self.status.configure(text=text, fg=color)
        if text:
            self.status.pack(fill="x", pady=(GAP if self.actions.winfo_manager() else 0, 0))
        else:
            self.status.pack_forget()
        self._fit_footer_spacing()
        self._refit()

    def _copy(self, announce: bool = True):
        if not self.result_text or self._login_thread is not None or self._extraction is not None:
            return False
        if announce:
            self._dismiss_import_report()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.result_text)
        except tk.TclError:
            if announce:
                self._set_status("Clipboard unavailable. The account is still loaded; try Copy again.", ERR)
            return False
        if announce:
            self._set_status("Copied to clipboard", OK)
            self.copy_btn.set_text("Copied!")
            if self._copy_reset_job is not None:
                self.root.after_cancel(self._copy_reset_job)
            self._copy_reset_job = self.root.after(1200, self._reset_copy_label)
        return True

    def _reset_copy_label(self):
        self._copy_reset_job = None
        self.copy_btn.set_text("Copy")

    def _copy_shortcut(self, event):
        if isinstance(event.widget, (tk.Entry, tk.Text)):
            return
        self._copy()
        return "break"

    def _paste_shortcut(self, event):
        if isinstance(event.widget, (tk.Entry, tk.Text)):
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        existing = self._token_dialog
        self._open_token_dialog()
        if existing is None and self._token_dialog is not None and text:
            if len(text) <= 64 * 1024:
                self._token_dialog.token.insert("1.0", text)
        return "break"

    def _escape(self, _=None):
        if self._picker is not None:
            return self._close_picker()
        if self._extraction is not None or self._login_thread is not None:
            self._login_to_steam()
            return "break"
        if self.active_tab == "settings":
            self._select_tab("accounts")
        return "break"

    # -- misc ----------------------------------------------------------------
    def _fit_and_center(self):
        """Size the window to its content's requested size, then centre it."""
        self._refit()
        self.root.update_idletasks()
        w = WIN_W
        # A withdrawn window can still report its old minimum height until it
        # is mapped. Center the requested size, rather than restoring that height.
        h = self._auto_geometry[1] if self._auto_geometry else self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 3
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _refit(self):
        """Resize the window to its content's height, keeping its position."""
        if self._layout_running:
            return
        # Geometry changes and idle layout passes can emit intermediate sizes.
        # Process them under the guard so they never latch as a manual resize.
        self._layout_running = True
        try:
            self.root.update_idletasks()
            page = self._pages[self.active_tab]
            content_height = page.winfo_reqheight() + (PAD if not self.footer.winfo_manager() else 0)
            width = self._content_width + 2 * PAD
            self.viewport.configure(height=content_height, scrollregion=(0, 0, width, content_height))
            self.root.update_idletasks()
            if self._manual_size is not None:
                return
            h = min(max(DEFAULT_H, self.root.winfo_reqheight()), self.root.winfo_screenheight() - 120)
            y = max(0, min(self.root.winfo_y(), self.root.winfo_screenheight() - h - 80))
            self._auto_geometry = (WIN_W, h)
            self.root.geometry(f"{WIN_W}x{h}+{max(0, self.root.winfo_x())}+{y}")
            self.root.update_idletasks()
        finally:
            self._layout_running = False

    def _on_root_resize(self, event):
        if event.widget is not self.root or self._layout_running:
            return
        size = (event.width, event.height)
        if size != (self.root.winfo_width(), self.root.winfo_height()):
            return  # Ignore queued events for a superseded programmatic size.
        if size == self._auto_geometry or event.width < 520:
            return
        self._manual_size = size
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(60, self._apply_resize)

    def _apply_resize(self):
        self._resize_job = None
        width = self.root.winfo_width()
        new_width = max(472, width - 2 * PAD)
        if self._content_width == new_width:
            self._refit()
            return
        old_width = self._content_width
        self._layout_running = True
        try:
            self._content_width = new_width
            self.viewport.itemconfigure(self._content_window, width=width)
            def rewrap(widget):
                if isinstance(widget, tk.Label):
                    # Older Tkinter returns a Tcl distance object here. Use
                    # Tk's pixel conversion, which also understands unit suffixes.
                    wraplength = widget.winfo_pixels(str(widget.cget("wraplength")))
                    if wraplength:
                        if not hasattr(widget, "_layout_margin"):
                            widget._layout_margin = old_width - wraplength
                        widget.configure(wraplength=max(120, new_width - widget._layout_margin))
                for child in widget.winfo_children():
                    rewrap(child)
            for page in (self.content, self.settings_content, self.footer):
                rewrap(page)
            self.subtitle.configure(wraplength=max(150, new_width - 46 - 2 * GAP - self.paste_btn.winfo_reqwidth()))
            self.steam_status.configure(wraplength=max(120, new_width - 124))
            self.drop.set_width(new_width)
            for card in (self.import_card, self.result_card, self.details_card, self.login_options_card,
                         self.cs2_options_card, self.cs2_settings_card, self.data_card):
                card.set_width(new_width)
            self.cs2_options_field.set_width(new_width - 2 * GAP)
            self.cs2_options_card.fit()
            self.saved_btn.set_width(new_width)
            for button in (self.copy_btn, self.login_btn, self.defaults_scope_btn, self.account_scope_btn):
                button.set_width((new_width - GAP) // 2)
            self._reflow_output()
        finally:
            self._layout_running = False
        self._refit()

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
        if isinstance(event.widget, tk.Text) and event.widget.yview() != (0.0, 1.0):
            return  # Text's own scrolling already handled this event.
        if self.viewport.yview() != (0.0, 1.0):
            direction = -1 if event.delta > 0 else 1
            self.viewport.yview_scroll(direction * max(1, abs(event.delta) // 120) * 2, "units")
            return "break"


def main():
    root = TkinterDnD.Tk() if _DND_AVAILABLE else tk.Tk()
    app = App(root)

    # If launched by dropping a file onto the .exe icon, process it immediately.
    paths = [arg for arg in sys.argv[1:] if os.path.isfile(arg)]
    if paths:
        root.after(150, lambda: app._start_imports(paths))

    root.mainloop()


if __name__ == "__main__":
    main()
