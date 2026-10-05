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
    assert entered.wait(1)
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
    assert entered.wait(1)
    window._request_close()
    assert window._login_cancel.is_set()
    assert window.root.winfo_exists()
    release.set()
    finish(window)
    assert not window.root.winfo_exists()
