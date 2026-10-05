"""GUI wiring with a temporary store and a mocked login backend."""
import os
from pathlib import Path
import sys
import threading
import time

import pytest

tk = pytest.importorskip("tkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as gui
from test_steam_login import jwt


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = gui.TkinterDnD.Tk() if gui._DND_AVAILABLE else tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def window(tmp_path, monkeypatch, tk_root):
    monkeypatch.setenv("JWTRACTOR_STORE", str(tmp_path / "accounts.json"))
    root = tk.Toplevel(tk_root)
    root.withdraw()
    # Extraction auto-copies; keep GUI tests off the user's real clipboard.
    monkeypatch.setattr(root, "clipboard_clear", lambda: None)
    monkeypatch.setattr(root, "clipboard_append", lambda value: None)
    application = gui.App(root)
    yield application
    if application._token_dialog is not None:
        application._token_dialog.close()
    application._login_cancel.set()
    if application._login_thread is not None:
        application._login_thread.join(timeout=3)
    try:
        root.destroy()
    except tk.TclError:
        pass


def finish(application):
    deadline = time.monotonic() + 3
    while application._login_thread is not None and time.monotonic() < deadline:
        application.root.update()
        time.sleep(0.01)
    assert application._login_thread is None


def wait_until_entered(application, event):
    deadline = time.monotonic() + 2
    while not event.is_set() and time.monotonic() < deadline:
        application.root.update()
        time.sleep(0.01)
    assert event.is_set()


@pytest.mark.skipif(os.name != "nt", reason="Steam login button is Windows-only")
def test_extract_select_and_login_uses_original_username_not_alias(window, tmp_path, monkeypatch):
    assert not window.login_btn._enabled
    executable = tmp_path / "alice.exe"
    token = jwt()
    executable.write_bytes(b"synthetic executable " + token.encode() + b" end")
    window.process(str(executable))
    assert window.login_btn._enabled
    window.store.set_alias(window.current_account["id"], "My main")
    window._use_account(window.store.get(window.current_account["id"]))
    main_thread = threading.get_ident()
    calls = []
    def login(name, supplied, *, cancel, progress):
        assert threading.get_ident() != main_thread
        calls.append((name, supplied))
        progress("Synthetic progress")
        return {"preserved_accounts": 2, "warning": None}
    monkeypatch.setattr(gui, "login_account", login)
    window._login_to_steam()
    assert not window.saved_btn._enabled
    assert not window.copy_btn._enabled
    assert window.login_btn._text == "Cancel login"
    finish(window)
    assert calls == [("alice", token)]
    assert window.saved_btn._enabled
    assert window.login_btn._text == "Log in to Steam"
    assert "2 other remembered" in window.status.cget("text")


def test_failed_extraction_clears_login_target(window, tmp_path):
    window._use_account(window.store.add(jwt(), "alice"))
    window.process(str(tmp_path / "missing.exe"))
    assert window.current_account is None
    assert not window.login_btn._enabled


def test_invalid_steam_token_is_rejected_before_starting_worker(window, monkeypatch):
    window._use_account(window.store.add(jwt({"sub": "not-steam"}), "alice"))
    monkeypatch.setattr(gui, "login_account", lambda *args, **kwargs: pytest.fail("Must reject invalid Steam token before calling login"))
    window._login_to_steam()
    assert window._login_thread is None
    assert "not a Steam login token" in window.status.cget("text")


@pytest.mark.skipif(os.name != "nt", reason="Steam login button is Windows-only")
def test_cancel_button_keeps_window_responsive_and_reports_cancellation_once(window, monkeypatch):
    window._use_account(window.store.add(jwt(), "alice"))
    entered = threading.Event()
    def login(*args, cancel, progress):
        entered.set()
        assert cancel.wait(2)
        raise gui.LoginCancelled("Cancelled.")
    monkeypatch.setattr(gui, "login_account", login)
    window._login_to_steam()
    wait_until_entered(window, entered)
    window.root.update()
    window._login_to_steam()
    finish(window)
    assert window.status.cget("text") == "Cancelled."
    assert window.login_btn._enabled


def test_footer_controls_fit_content_width(window):
    window.root.update_idletasks()
    width = sum(button.winfo_reqwidth() for button in (window.copy_btn, window.login_btn)) + 10
    assert width <= gui.CONTENT_W
    assert window.root.winfo_reqwidth() <= gui.WIN_W


@pytest.mark.skipif(os.name != "nt", reason="Steam login button is Windows-only")
def test_closing_waits_for_worker_cleanup_before_destroying_window(window, monkeypatch):
    window._use_account(window.store.add(jwt(), "alice"))
    entered, release = threading.Event(), threading.Event()
    def login(*args, cancel, progress):
        entered.set()
        assert cancel.wait(2)
        assert release.wait(2)
        raise gui.LoginCancelled("Cancelled.")
    monkeypatch.setattr(gui, "login_account", login)
    window._login_to_steam()
    wait_until_entered(window, entered)
    window._request_close()
    assert window._login_cancel.is_set()
    assert window.root.winfo_exists()
    release.set()
    finish(window)
    assert not window.root.winfo_exists()


def test_paste_dialog_adds_account_and_selects_it_without_logging_in(window, monkeypatch):
    monkeypatch.setattr(gui, "login_account", lambda *args, **kwargs: pytest.fail("Adding a token must not start Steam login"))
    window._open_token_dialog()
    dialog = window._token_dialog
    window._open_token_dialog()
    assert window._token_dialog is dialog
    dialog.token.insert("1.0", "alice----" + jwt())
    dialog._submit()
    assert window._token_dialog is None
    assert window.current_account["username"] == "alice"
    assert window.result_text == "alice----" + jwt()
    assert len(window.store.accounts) == 1
    assert window._login_thread is None
    assert window.login_btn._enabled == (os.name == "nt")


def test_raw_token_import_prompts_for_username_and_keeps_invalid_dialog_open(window):
    window._open_token_dialog()
    dialog = window._token_dialog
    dialog.token.insert("1.0", jwt())
    dialog._submit()
    assert window._token_dialog is dialog
    assert "login name" in dialog.error.cget("text")
    assert window.store.accounts == []
    dialog.username.insert(0, "alice")
    dialog._submit()
    assert window.current_account["username"] == "alice"
    assert window._token_dialog is None


def test_reimport_preserves_alias_and_deduplicates(window):
    account = window._add_token("alice----" + jwt())
    window.store.set_alias(account["id"], "Main")
    window._add_token(jwt(), "alice")
    assert len(window.store.accounts) == 1
    assert window.current_account["alias"] == "Main"


def test_paste_save_failure_keeps_prior_selection_and_shows_inline_error(window, monkeypatch):
    old = window._add_token("alice----" + jwt())
    old_result = window.result_text
    window._open_token_dialog()
    dialog = window._token_dialog
    dialog.token.insert("1.0", "bob----" + jwt({"sub": "76561198000000001"}))
    def fail():
        raise PermissionError("Synthetic failure")
    monkeypatch.setattr(window.store, "save", fail)
    dialog._submit()
    assert "Couldn't save" in dialog.error.cget("text")
    assert window.current_account is old
    assert window.result_text == old_result
    assert window.store.accounts == [old]
    assert window._token_dialog is dialog


def test_import_dialog_layout_and_keyboard_controls(window):
    window._open_token_dialog()
    dialog = window._token_dialog
    dialog.update_idletasks()
    assert dialog.winfo_width() == 520
    assert dialog._actions.winfo_reqwidth() <= 472
    assert dialog.token.cget("wrap") == "char"
    assert dialog.username.cget("show") == ""
    dialog.token.insert("1.0", "alice----" + jwt())
    dialog.add_btn._activate()
    assert window.current_account["username"] == "alice"
    window._open_token_dialog()
    window._token_dialog.cancel_btn._activate()
    assert window._token_dialog is None


@pytest.mark.parametrize("sign_in,reason,expected", [
    ("confirmed", None, "Signed in to Steam."),
    ("rejected", "Access denied", "Steam rejected sign-in: Access denied."),
    ("other_account", None, "different account"),
])
def test_login_result_status_reflects_client_response(window, sign_in, reason, expected):
    window._login_events.put(("done", {"preserved_accounts": 1, "warning": None,
                                      "sign_in": sign_in, "reason": reason}))
    window._drain_login_events()
    assert expected in window.status.cget("text")
    assert "account(s)" not in window.status.cget("text")


def test_long_account_details_scroll_on_small_screens_and_keep_actions_visible(window, monkeypatch):
    monkeypatch.setattr(window.root, "winfo_screenheight", lambda: 720)
    window._add_token("alice----" + jwt({"jti": "synthetic " * 300, "iat": 1790778246,
                                         "nbf": 1782138246, "oat": 1790778246, "per": 1}))
    window.root.deiconify()
    window.root.update()
    assert window.root.winfo_height() <= 600
    assert window.viewport.yview()[1] < 1
    assert window.login_btn.winfo_rooty() + window.login_btn.winfo_height() <= window.root.winfo_rooty() + window.root.winfo_height()
    window.viewport.yview_moveto(1)
    assert window.viewport.yview()[1] == 1
