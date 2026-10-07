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
    monkeypatch.setattr(gui, "current_steam_session", lambda: None)
    root = tk.Toplevel(tk_root)
    root.withdraw()
    # Extraction auto-copies; keep GUI tests off the user's real clipboard.
    monkeypatch.setattr(root, "clipboard_clear", lambda: None)
    monkeypatch.setattr(root, "clipboard_append", lambda value: None)
    application = gui.App(root)
    yield application
    if application._token_dialog is not None:
        application._token_dialog.close()
    if application._rename_dialog is not None:
        application._rename_dialog.close()
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
    def login(name, supplied, *, cancel, progress, private_login, disable_cloud_sync, cs2_launch_options):
        assert threading.get_ident() != main_thread
        assert private_login is False
        assert disable_cloud_sync is True
        assert cs2_launch_options is None
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


@pytest.mark.skipif(os.name != "nt", reason="Steam login button is Windows-only")
def test_private_login_toggle_is_remembered_and_passed_to_worker(window, monkeypatch):
    window._use_account(window.store.add(jwt(), "alice"))
    window._toggle_private_login()
    assert window.private_login_btn.checked
    assert gui.Store(window.store.path).preferences["private_login"] is True
    entered = threading.Event()
    release = threading.Event()
    received = []
    def login(*args, cancel, progress, private_login, disable_cloud_sync, cs2_launch_options):
        received.append(private_login)
        entered.set()
        assert release.wait(2)
        return {"preserved_accounts": 1, "private_login": True, "sign_in": "confirmed"}
    monkeypatch.setattr(gui, "login_account", login)
    window._login_to_steam()
    wait_until_entered(window, entered)
    assert not window.private_login_btn._enabled
    window._toggle_private_login()
    assert window.private_login is True
    release.set()
    finish(window)
    assert received == [True]
    assert window.private_login_btn._enabled
    assert "Remote Play off configured" in window.status.cget("text")


def test_toggle_save_failure_keeps_ui_and_store_in_sync(window, monkeypatch):
    def fail():
        raise PermissionError("Synthetic save failure")
    monkeypatch.setattr(window.store, "save", fail)
    window._toggle_private_login()
    assert not window.private_login
    assert not window.private_login_btn.checked
    assert not window.store.preferences["private_login"]
    assert "Could not save" in window.status.cget("text")


@pytest.mark.skipif(os.name != "nt", reason="Steam login button is Windows-only")
def test_cloud_option_defaults_on_is_locked_during_login_and_remembers_off(window, monkeypatch):
    assert window.disable_cloud_sync_btn.checked
    window._use_account(window.store.add(jwt(), "alice"))
    entered, release = threading.Event(), threading.Event()
    received = []
    def login(*args, cancel, progress, private_login, disable_cloud_sync, cs2_launch_options):
        received.append(disable_cloud_sync)
        entered.set()
        assert release.wait(2)
        return {"preserved_accounts": 1, "disable_cloud_sync": disable_cloud_sync, "sign_in": "confirmed"}
    monkeypatch.setattr(gui, "login_account", login)
    window._login_to_steam()
    wait_until_entered(window, entered)
    assert not window.disable_cloud_sync_btn._enabled
    window._toggle_disable_cloud_sync()
    assert window.disable_cloud_sync is True
    release.set()
    finish(window)
    assert "Steam Cloud Sync off configured" in window.status.cget("text")
    assert window.disable_cloud_sync_btn._enabled
    window._toggle_disable_cloud_sync()
    assert not window.disable_cloud_sync_btn.checked
    assert gui.Store(window.store.path).preferences["disable_cloud_sync"] is False
    window._use_account(window.store.add(jwt({"sub": "76561198000000001"}), "bob"))
    window._login_to_steam()
    finish(window)
    assert received == [True, False]
    assert "Cloud" not in window.status.cget("text")


def test_cloud_toggle_save_failure_keeps_default_enabled(window, monkeypatch):
    def fail():
        raise PermissionError("Synthetic save failure")
    monkeypatch.setattr(window.store, "save", fail)
    window._toggle_disable_cloud_sync()
    assert window.disable_cloud_sync
    assert window.disable_cloud_sync_btn.checked
    assert window.store.preferences["disable_cloud_sync"]
    assert "Could not save" in window.status.cget("text")


@pytest.mark.skipif(os.name != "nt", reason="Steam login button is Windows-only")
def test_cs2_options_saved_before_login_and_controls_locked(window, monkeypatch):
    options = '-console +exec "custom settings.cfg"'
    window._select_tab("settings")
    window.cs2_launch_options_entry.insert(0, options)
    window._toggle_use_cs2_launch_options()
    assert gui.Store(window.store.path).preferences["cs2_launch_options"] == options
    assert gui.Store(window.store.path).preferences["use_cs2_launch_options"] is True
    options = '-console +exec "edited settings.cfg"'
    window.cs2_launch_options_entry.delete(0, "end")
    window.cs2_launch_options_entry.insert(0, options)
    window._use_account(window.store.add(jwt(), "alice"))
    entered, release = threading.Event(), threading.Event()
    received = []
    def login(*args, cancel, progress, private_login, disable_cloud_sync, cs2_launch_options):
        received.append(cs2_launch_options)
        entered.set()
        assert release.wait(2)
        return {"preserved_accounts": 1, "sign_in": "confirmed", "cs2_launch_options_applied": cs2_launch_options is not None}
    monkeypatch.setattr(gui, "login_account", login)
    window._login_to_steam()
    wait_until_entered(window, entered)
    assert window.cs2_launch_options_entry.cget("state") == "disabled"
    assert not window.use_cs2_launch_options_btn._enabled
    window._toggle_use_cs2_launch_options()
    assert window.use_cs2_launch_options is True
    release.set()
    finish(window)
    assert received == [options]
    assert gui.Store(window.store.path).preferences["cs2_launch_options"] == options
    assert window.cs2_launch_options_entry.cget("state") == "normal"
    assert "CS2 launch options configured" in window.status.cget("text")
    window._toggle_use_cs2_launch_options()
    window._login_to_steam()
    finish(window)
    assert received == [options, None]


def test_cs2_options_invalid_or_failed_save_keeps_previous_preference(window, monkeypatch):
    window._select_tab("settings")
    window.cs2_launch_options_entry.insert(0, "x" * 4097)
    window._toggle_use_cs2_launch_options()
    assert not window.use_cs2_launch_options
    assert window.store.preferences["cs2_launch_options"] == ""
    assert "4096" in window.cs2_options_status.cget("text")
    window.cs2_launch_options_entry.delete(0, "end")
    window.cs2_launch_options_entry.insert(0, "-console")
    def fail():
        raise PermissionError("Synthetic save failure")
    monkeypatch.setattr(window.store, "save", fail)
    assert not window._save_cs2_launch_options()
    assert "Could not save" in window.cs2_options_status.cget("text")
    assert window.store.preferences["cs2_launch_options"] == ""
    assert window.cs2_launch_options_entry.get() == "-console"
    window.cs2_launch_options_entry.delete(0, "end")


@pytest.mark.parametrize("action", ["enter", "navigate", "close"])
def test_cs2_draft_is_saved_before_enter_navigation_or_close(window, action):
    window._select_tab("settings")
    window.cs2_launch_options_entry.insert(0, '-console +exec "saved draft.cfg"')
    if action == "enter":
        window._save_cs2_launch_options_on_enter()
    elif action == "navigate":
        window._select_tab("accounts")
    else:
        window._request_close()
    preferences = gui.Store(window.store.path).preferences
    assert preferences["cs2_launch_options"] == '-console +exec "saved draft.cfg"'
    assert preferences["use_cs2_launch_options"] is False


@pytest.mark.skipif(os.name != "nt", reason="Steam login button is Windows-only")
def test_global_option_applies_across_accounts_until_disabled(window, monkeypatch):
    alice = window.store.add(jwt(), "alice")
    bob = window.store.add(jwt({"sub": "76561198000000001"}), "bob")
    calls = []
    def login(name, token, *, cancel, progress, private_login, disable_cloud_sync, cs2_launch_options):
        calls.append((name, private_login))
        return {"preserved_accounts": 1, "sign_in": "confirmed", "private_login": private_login}
    monkeypatch.setattr(gui, "login_account", login)
    window._toggle_private_login()
    for account in (alice, bob):
        window._use_account(account)
        assert window.private_login_btn.checked
        window._login_to_steam()
        finish(window)
    window._toggle_private_login()
    window._use_account(alice)
    window._login_to_steam()
    finish(window)
    assert calls == [("alice", True), ("bob", True), ("alice", False)]
    assert gui.Store(window.store.path).preferences["private_login"] is False


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
    def login(*args, cancel, progress, private_login, disable_cloud_sync, cs2_launch_options):
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
    def login(*args, cancel, progress, private_login, disable_cloud_sync, cs2_launch_options):
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


def test_themed_rename_dialog_saves_clears_and_cancels_alias(window):
    account = window.store.add(jwt(), "alice")
    window.store.set_alias(account["id"], "Old name")
    window._rename_account(account)
    dialog = window._rename_dialog
    assert dialog.cget("bg") == gui.BG
    assert dialog.alias.cget("bg") == gui.FIELD
    assert dialog.alias.get() == "Old name"
    window._rename_account(account)
    assert window._rename_dialog is dialog
    dialog.alias.delete(0, "end")
    dialog.alias.insert(0, "  Main account  ")
    dialog._submit()
    assert window._rename_dialog is None
    assert gui.Store(window.store.path).get(account["id"])["alias"] == "Main account"
    window._rename_account(account)
    window._rename_dialog.alias.delete(0, "end")
    window._rename_dialog._submit()
    assert account["alias"] == ""
    window._rename_account(account)
    window._rename_dialog.alias.insert(0, "Not saved")
    window._rename_dialog.close()
    assert account["alias"] == ""


def test_rename_save_failure_stays_open_and_preserves_previous_alias(window, monkeypatch):
    account = window.store.add(jwt(), "alice")
    window.store.set_alias(account["id"], "Original")
    window._rename_account(account)
    dialog = window._rename_dialog
    dialog.alias.delete(0, "end")
    dialog.alias.insert(0, "Unsaved")
    def fail():
        raise PermissionError("Synthetic save failure")
    monkeypatch.setattr(window.store, "save", fail)
    dialog._submit()
    assert window._rename_dialog is dialog
    assert dialog.alias.get() == "Unsaved"
    assert account["alias"] == "Original"
    assert "Could not save" in dialog.error.cget("text")


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


def test_button_release_requires_press_on_same_button(window):
    from types import SimpleNamespace
    calls = []
    button = gui.RoundedButton(window.root, 'Synthetic', lambda: calls.append(True))
    event = SimpleNamespace(x=10, y=10)
    button._release(event)
    assert calls == []
    button._press(event)
    button._release(event)
    assert calls == [True]
    button._press(event)
    button.set_enabled(False)
    button.set_enabled(True)
    button._release(event)
    assert calls == [True]


def test_drop_zone_keyboard_and_busy_state(window):
    calls = []
    window.drop._on_click = lambda: calls.append(True)
    window.drop._activate()
    window.drop.set_enabled(False)
    window.drop._activate()
    assert calls == [True]
    assert not int(window.drop.cget('takefocus'))


def detail_text(window):
    def labels(widget):
        result = [widget.cget('text')] if isinstance(widget, tk.Label) else []
        for child in widget.winfo_children():
            result.extend(labels(child))
        return result
    return '\n'.join(labels(window.details_inner))


def test_cached_cooldown_display_does_not_start_a_check(window):
    steam_id = '76561199749125703'
    window.store.set_cooldown(steam_id, {'state': 'clear', 'message': 'No active matchmaking cooldown'}, checked_at=1700000000)
    window._add_token('alice----' + jwt({'sub': steam_id}))
    assert 'No active matchmaking cooldown' in detail_text(window)
    assert 'Last refreshed: 2023-11-14 22:13:20 UTC' in detail_text(window)
    assert window._cooldown_check is None
    window._add_token('alice----' + jwt({'sub': steam_id, 'jti': 'new synthetic token'}))
    assert 'Last refreshed: 2023-11-14 22:13:20 UTC' in detail_text(window)


def test_cached_expiry_elapsed_is_not_claimed_as_currently_clear(window):
    steam_id = '76561199749125703'
    window.store.set_cooldown(steam_id, {'state': 'active', 'message': 'Active cooldown', 'expires_at': 1700000100}, checked_at=1700000000)
    window._add_token('alice----' + jwt({'sub': steam_id}))
    assert 'Recorded cooldown has elapsed' in detail_text(window)
    assert 'No active matchmaking cooldown' not in detail_text(window)


def test_background_cooldown_result_saved_for_its_account_only(window):
    steam_id = '76561199749125703'
    window._add_token('other----' + jwt({'sub': '76561198000000001'}))
    events = gui.queue.Queue()
    events.put({'state': 'clear', 'message': 'No active matchmaking cooldown'})
    window._cooldown_check = (steam_id, None, gui.threading.Event(), events)
    window._poll_cooldown_check()
    assert window.store.cooldowns[steam_id]['state'] == 'clear'
    assert 'No active matchmaking cooldown' not in detail_text(window)
    assert window._cooldown_check is None
    assert gui.Store(window.store.path).cooldowns[steam_id]['checked_at'] > 0


def test_failed_cooldown_refresh_keeps_cached_success(window):
    steam_id = '76561199749125703'
    window.store.set_cooldown(steam_id, {'state': 'clear', 'message': 'Previous clear result'}, checked_at=1700000000)
    window._add_token('alice----' + jwt({'sub': steam_id}))
    events = gui.queue.Queue()
    events.put({'state': 'error', 'message': 'Synthetic client error'})
    window._cooldown_check = (steam_id, None, gui.threading.Event(), events)
    window._poll_cooldown_check()
    assert 'Previous clear result' in detail_text(window)
    assert 'Synthetic client error' in detail_text(window)
    assert window.store.cooldowns[steam_id]['checked_at'] == 1700000000


def test_steam_account_changes_trigger_one_automatic_check(window, monkeypatch):
    steam_id = '76561199749125703'
    other = '76561198000000001'
    window._add_token('alice----' + jwt({'sub': steam_id}))
    window._add_token('bob----' + jwt({'sub': other}))
    calls = []
    def check(steam_id, *, automatic=False):
        calls.append((steam_id, automatic))
        window._steam_session_seen = gui.current_steam_session()
    monkeypatch.setattr(window, '_check_cooldown', check)
    monkeypatch.setattr(gui, 'current_steam_session', lambda: (steam_id, 100))
    window._watch_steam_session()
    window._watch_steam_session()
    assert calls == [(steam_id, True)]
    monkeypatch.setattr(gui, 'current_steam_session', lambda: (other, 100))
    window._watch_steam_session()
    assert calls[-1] == (other, True)
    monkeypatch.setattr(gui, 'current_steam_session', lambda: None)
    window._watch_steam_session()
    monkeypatch.setattr(gui, 'current_steam_session', lambda: (other, 100))
    window._watch_steam_session()
    assert calls.count((other, True)) == 2


def test_confirmed_login_triggers_selected_account_refresh(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, '_check_cooldown', lambda sid, **kwargs: calls.append((sid, kwargs)))
    window._login_events.put(('done', {'sign_in':'confirmed', 'steam_id':'76561199749125703',
                                      'preserved_accounts':1, 'warning':None}))
    window._drain_login_events()
    assert calls == [('76561199749125703', {'automatic':True})]


def test_cancelled_client_check_cannot_update_cache(window):
    steam_id = '76561199749125703'
    events = gui.queue.Queue()
    cancel = gui.threading.Event()
    window._cooldown_check = (steam_id, None, cancel, events)
    window._stop_cooldown_check()
    events.put({'state':'clear', 'message':'Late result'})
    window._poll_cooldown_check()
    assert cancel.is_set()
    assert steam_id not in window.store.cooldowns
