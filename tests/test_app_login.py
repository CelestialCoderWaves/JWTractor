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
    monkeypatch.setattr(gui, "fetch_presence", lambda *a, **k: {"state": "unknown", "message": "Synthetic presence unavailable"})
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


def test_failed_extraction_preserves_selected_account_and_cached_details(window, tmp_path):
    account = window.store.add(jwt(), "alice")
    window.store.set_cooldown(account["subject"], {"state": "clear", "message": "Saved cooldown result"})
    window._use_account(account)
    original = window.result_text
    window.process(str(tmp_path / "missing.exe"))
    assert window.current_account is account
    assert window.result_text == original
    assert "Saved cooldown result" in detail_text(window)
    assert "Selected account unchanged" in window.status.cget("text")
    assert window.login_btn._enabled == (os.name == "nt")


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
    assert "Friends & Chat offline and Remote Play off configured" in window.status.cget("text")


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


def test_account_switch_shows_matching_identity_and_cached_cooldown(window):
    alice = window.store.add(jwt(), "alice")
    bob_id = "76561198000000001"
    bob = window.store.add(jwt({"sub": bob_id}), "bob")
    window.store.set_alias(bob["id"], "Main account")
    window.store.set_cooldown(bob_id, {"state": "active", "message": "Bob cooldown"})
    window._use_account(alice)
    assert "Bob cooldown" not in detail_text(window)
    window._use_account(bob)
    text = detail_text(window)
    assert "Bob cooldown" in text
    assert "Main account" in text and "Steam login: bob" in text
    assert window.result_text == gui.account_combined(bob)


def test_renaming_another_account_does_not_change_login_target(window):
    alice = window._add_token("alice----" + jwt())
    bob = window.store.add(jwt({"sub": "76561198000000001"}), "bob")
    window._rename_account(bob)
    window._rename_dialog.alias.insert(0, "Secondary")
    window._rename_dialog._submit()
    assert window.current_account is alice
    assert window.result_text == gui.account_combined(alice)
    assert "Steam login: alice" in detail_text(window)


def test_visible_token_matches_full_copy_and_updates_on_account_change(window, monkeypatch):
    copied = []
    monkeypatch.setattr(window.root, "clipboard_append", copied.append)
    alice = window._add_token("alice----" + jwt())
    assert window.output.get("1.0", "end-1c") == gui.account_combined(alice)
    assert copied[-1] == gui.account_combined(alice)
    bob = window.store.add(jwt({"sub": "76561198000000001"}), "bob")
    window._use_account(bob)
    assert window.output.get("1.0", "end-1c") == gui.account_combined(bob)
    assert copied[-1] == gui.account_combined(bob)


def test_clipboard_failure_does_not_lose_successful_import(window, monkeypatch):
    def fail():
        raise tk.TclError("Synthetic busy clipboard")
    monkeypatch.setattr(window.root, "clipboard_clear", fail)
    account = window._add_token("alice----" + jwt())
    assert window.current_account is account
    assert gui.Store(window.store.path).get(account["id"]) is not None
    assert "clipboard unavailable" in window.status.cget("text")
    assert "copied" not in window.status.cget("text")
    assert window._copy() is False
    assert "try Copy again" in window.status.cget("text")


def test_account_search_matches_alias_username_and_steam_id_and_recovers_empty_search(window):
    alice = window.store.add(jwt(), "alice")
    bob_id = "76561198000000001"
    bob = window.store.add(jwt({"sub": bob_id}), "bob")
    window.store.set_alias(bob["id"], "Main account")
    window._open_picker()
    popup = window._picker
    for query in ("MAIN bob", bob_id, "main ACCOUNT"):
        popup.query.set(query)
        assert popup.matches == [bob]
    popup.query.set("no such account")
    assert popup.matches == [] and popup.rows == []
    popup.query.set("ALICE")
    assert popup.matches == [alice]
    popup.query.set("")
    assert len(popup.matches) == 2
    window._close_picker()


def test_import_worker_keeps_ui_responsive_and_does_not_apply_cancelled_result(window, monkeypatch):
    alice = window._add_token("alice----" + jwt())
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    def extract(path):
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(3)
        return gui.parse_token_input("bob----" + jwt({"sub": "76561198000000001"}))
    monkeypatch.setattr(gui, "extract_from_file", extract)
    window._start_extraction("synthetic.exe")
    assert entered.wait(2)
    worker = window._extraction[0]
    assert not window.saved_btn._enabled and window.login_btn._text == "Cancel import"
    tick = []
    window.root.after_idle(lambda: tick.append(True))
    window.root.update()
    assert tick == [True]
    window._login_to_steam()  # The main action cancels an import, without launching Steam.
    assert window._extraction is None
    release.set()
    worker.join(timeout=2)
    window.root.update()
    assert window.store.accounts == [alice]
    assert window.current_account is alice
    assert window.login_btn._text == "Log in to Steam"


def test_background_import_saves_result_on_main_thread(window, tmp_path, monkeypatch):
    executable = tmp_path / "alice.exe"
    executable.write_bytes(b"synthetic file " + jwt().encode())
    main_thread = threading.get_ident()
    original_save = window.store.save
    def save():
        assert threading.get_ident() == main_thread
        original_save()
    monkeypatch.setattr(window.store, "save", save)
    window._start_extraction(str(executable))
    deadline = time.monotonic() + 3
    while window._extraction is not None and time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)
    assert window._extraction is None
    assert window.current_account["username"] == "alice"
    assert "copied · saved" in window.status.cget("text")


def test_escape_closes_picker_or_leaves_settings_without_closing_program(window):
    window._add_token("alice----" + jwt())
    window._open_picker()
    window._escape()
    assert window._picker is None and window.root.winfo_exists()
    window._select_tab("settings")
    window._escape()
    assert window.active_tab == "accounts" and window.root.winfo_exists()


def test_picker_keyboard_navigation_scrolls_rows_into_view_and_enter_selects(window):
    for index in range(10):
        window.store.add(jwt({"sub": str(76561198000000001 + index)}), f"account_{index}")
    window.root.deiconify()
    window._open_picker()
    popup = window._picker
    window.root.update()
    popup.search.focus_force()
    popup.search.event_generate("<Down>")
    window.root.update()
    assert popup.focus_get() is popup.rows[0]
    for row in popup.rows[:-1]:
        row.event_generate("<Down>")
        window.root.update()
    last = popup.rows[-1]
    assert popup.focus_get() is last
    assert last.winfo_rooty() + last.winfo_height() <= popup.winfo_rooty() + popup.winfo_height()
    selected = popup.matches[-1]
    last.event_generate("<Return>")
    window.root.update()
    assert window._picker is None and window.current_account is selected


def test_refresh_on_new_account_replaces_other_accounts_check(window, monkeypatch):
    bob_id = "76561198000000001"
    bob = window.store.add(jwt({"sub": bob_id}), "bob")
    old_cancel = threading.Event()
    window._cooldown_check = ("76561198000000002", None, old_cancel, gui.queue.Queue())
    window._use_account(bob)
    assert window.cooldown_btn._text == "Refresh data"
    entered, release = threading.Event(), threading.Event()
    def check(steam_id, *, cancel):
        assert steam_id == bob_id
        entered.set()
        assert release.wait(3)
        return {"state": "clear", "message": "Bob verified"}
    monkeypatch.setattr(gui, "check_client_cooldown", check)
    window._check_cooldown()
    assert entered.wait(2)
    assert old_cancel.is_set()
    worker = window._cooldown_check[1]
    assert window._cooldown_check[0] == bob_id
    release.set()
    worker.join(timeout=2)
    window._poll_cooldown_check()
    assert window.store.cooldowns[bob_id]["message"] == "Bob verified"


def test_copy_and_paste_shortcuts_leave_editable_fields_alone(window, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(window, "_copy", lambda: pytest.fail("Must preserve native text copying"))
    monkeypatch.setattr(window, "_open_token_dialog", lambda: pytest.fail("Must preserve native text pasting"))
    assert window._copy_shortcut(SimpleNamespace(widget=window.cs2_launch_options_entry)) is None
    assert window._paste_shortcut(SimpleNamespace(widget=window.cs2_launch_options_entry)) is None


def test_paste_shortcut_prefills_dialog_without_saving_or_logging_in(window, monkeypatch):
    from types import SimpleNamespace
    pasted = "alice----" + jwt()
    monkeypatch.setattr(window.root, "clipboard_get", lambda: pasted)
    assert window._paste_shortcut(SimpleNamespace(widget=window.saved_btn)) == "break"
    assert window._token_dialog.token.get("1.0", "end-1c") == pasted
    assert window.store.accounts == [] and window._login_thread is None


def test_find_accounts_from_settings_saves_draft_and_uses_existing_popup(window):
    window._add_token("alice----" + jwt())
    window._select_tab("settings")
    window.cs2_launch_options_entry.insert(0, "-console")
    window._open_picker()
    popup = window._picker
    assert window.active_tab == "accounts"
    assert window.store.preferences["cs2_launch_options"] == "-console"
    window._open_picker()
    assert window._picker is popup


def test_startup_restores_selected_account_without_clipboard_or_login(window, monkeypatch):
    alice = window.store.add(jwt(), "alice")
    window.store.add(jwt({"sub": "76561198000000001"}), "bob")
    window.store.touch(alice["id"])
    monkeypatch.setattr(gui, "login_account", lambda *a, **k: pytest.fail("Startup must not sign in"))
    root = tk.Toplevel(window.root)
    root.withdraw()
    monkeypatch.setattr(root, "clipboard_clear", lambda: pytest.fail("Startup must not copy"))
    before = Path(window.store.path).read_bytes()
    try:
        restored = gui.App(root)
        assert restored.current_account["id"] == alice["id"]
        assert restored.result_text == gui.account_combined(alice)
        assert restored._login_thread is None
        assert Path(window.store.path).read_bytes() == before
    finally:
        root.destroy()


def test_invalid_cs2_draft_can_be_reverted_and_navigation_recovers(window):
    window._add_token("alice----" + jwt())
    window.store.set_cs2_launch_options("-console")
    window._revert_cs2_options()
    window._select_tab("settings")
    window.cs2_options_var.set("x" * 4097)
    window._select_tab("accounts")
    assert window.active_tab == "settings"
    assert "4096" in window.cs2_options_status.cget("text")
    assert window.cs2_revert_btn._enabled
    window._revert_cs2_options()
    assert window.cs2_launch_options_entry.get() == "-console"
    assert not window.cs2_revert_btn._enabled
    window._select_tab("accounts")
    assert window.active_tab == "accounts"


def test_failed_cs2_save_reveals_settings_from_account_login(window, monkeypatch):
    window._add_token("alice----" + jwt())
    window.cs2_options_var.set("-console")
    monkeypatch.setattr(window.store, "save", lambda: (_ for _ in ()).throw(PermissionError("Synthetic failure")))
    window._login_to_steam()
    assert window._login_thread is None
    assert window.active_tab == "settings"
    assert "Could not save" in window.cs2_options_status.cget("text")


def test_idle_escape_keeps_window_open_and_details_toggle_preserves_token(window):
    window._add_token("alice----" + jwt())
    original = window.result_text
    window._escape()
    assert window.root.winfo_exists()
    assert not window._claims_expanded
    window._toggle_claims()
    assert "Issuer" in detail_text(window)
    assert window.result_text == original
    window._toggle_claims()
    assert "Issuer" not in detail_text(window)
    assert window.output.get("1.0", "end-1c") == original


@pytest.mark.skipif(os.name != "nt", reason="Steam login button is Windows-only")
def test_cancellation_stays_disabled_after_switching_tabs(window, monkeypatch):
    window._add_token("alice----" + jwt())
    entered, release = threading.Event(), threading.Event()
    def login(*args, cancel, **kwargs):
        entered.set()
        assert cancel.wait(2)
        assert release.wait(2)
        raise gui.LoginCancelled("Cancelled.")
    monkeypatch.setattr(gui, "login_account", login)
    window._login_to_steam()
    wait_until_entered(window, entered)
    window._escape()
    window._select_tab("settings")
    assert not window.login_btn._enabled
    assert window.login_btn._text == "Cancelling…"
    release.set()
    finish(window)
    assert window.status.cget("text") == "Cancelled."


def test_search_survives_rename_and_login_summary_tracks_options(window):
    account = window._add_token("alice----" + jwt())
    assert window.login_summary.cget("text") == "Next login: Cloud off"
    window._toggle_private_login()
    assert "Friends offline · Remote Play off" in window.login_summary.cget("text")
    window._open_picker()
    window._picker.query.set("alice")
    window._rename_account(account)
    window._rename_dialog.alias.insert(0, "Main")
    window._rename_dialog._submit()
    assert window._picker.query.get() == "alice"
    assert window._picker.matches == [account]


def test_repeated_account_refreshes_release_native_images(window):
    window._add_token("alice----" + jwt())
    before = set(window.root.tk.call("image", "names"))
    for _ in range(50):
        window._refresh_cooldown_details()
    after = set(window.root.tk.call("image", "names"))
    # Only the current claims button can add images; destroyed controls must
    # not accumulate native image handles in a long-running Steam session.
    assert len(after) <= len(before) + 4


def test_account_panels_and_import_strip_follow_selection_without_blank_token_card(window):
    assert not window.token_heading.winfo_manager()
    assert not window.result_card.winfo_manager()
    assert not window.actions.winfo_manager()
    initial_height = int(window.drop.cget("height"))
    window._add_token("alice----" + jwt())
    assert int(window.drop.cget("height")) < initial_height
    assert window.result_card.winfo_manager()
    assert window.actions.winfo_manager()
    window.root.update_idletasks()
    assert window.details_card.winfo_y() < window.token_heading.winfo_y()


@pytest.mark.parametrize("scaling", [1.3333, 2.0])
def test_settings_labels_fit_inside_cards_at_multiple_font_scales(window, scaling):
    root = tk.Toplevel(window.root)
    root.withdraw()
    original_scaling = float(root.tk.call("tk", "scaling"))
    try:
        root.tk.call("tk", "scaling", scaling)
        application = gui.App(root)
        application._select_tab("settings")
        root.update_idletasks()
        for row in application.login_options_card.inner.winfo_children():
            text, switch = [w for w in row.winfo_children() if isinstance(w, tk.Frame)][0], row.winfo_children()[0]
            assert text.winfo_x() + text.winfo_width() <= switch.winfo_x()
            for label in text.winfo_children():
                assert label.winfo_width() <= text.winfo_width()
        assert root.winfo_reqwidth() <= gui.WIN_W
    finally:
        root.destroy()
        window.root.tk.call("tk", "scaling", original_scaling)


def finish_import(application):
    deadline = time.monotonic() + 4
    while application._extraction is not None and time.monotonic() < deadline:
        application.root.update()
        time.sleep(0.01)
    assert application._extraction is None


def test_batch_import_reports_each_file_continues_after_failure_and_refreshes(window, tmp_path):
    files = [tmp_path / name for name in ("alice.exe", "invalid.exe", "bob.exe", "renamed.exe")]
    files[0].write_bytes(jwt({"exp": 2000000000}).encode())
    files[1].write_bytes(b"no token here")
    files[2].write_bytes(jwt({"sub": "76561198000000001"}).encode())
    files[3].write_bytes(jwt({"exp": 2100000000, "jti": "new"}).encode())
    window._start_imports([str(path) for path in files])
    finish_import(window)
    assert len(window.store.accounts) == 2
    assert [result[2] for result in window._import_results] == [True, False, True, True]
    assert window._import_results[-1][1] == "Refreshed"
    assert window.current_account["username"] == "alice"
    assert "invalid.exe" in window.import_report.get("1.0", "end-1c")
    assert "3 saved, 1 failed" in window.status.cget("text")


def test_batch_cancel_keeps_completed_saves_and_discards_late_result(window, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def extract(path):
        if path.endswith("second.exe"):
            entered.set()
            assert release.wait(3)
            return gui.parse_token_input("bob----" + jwt({"sub": "76561198000000001"}))
        return gui.parse_token_input("alice----" + jwt())
    monkeypatch.setattr(gui, "extract_from_file", extract)
    window._start_imports(["first.exe", "second.exe", "third.exe"])
    wait_until_entered(window, entered)
    worker = window._extraction[0]
    window._cancel_extraction()
    release.set()
    worker.join(timeout=2)
    window.root.update()
    assert len(window.store.accounts) == 1
    assert window.current_account["username"] == "alice"
    assert [item[1] for item in window._import_results[1:]] == ["Skipped (cancelled)"] * 2
    assert "1 completed file" in window.status.cget("text")


@pytest.mark.skipif(os.name != "nt", reason="Steam login button is Windows-only")
def test_selected_account_settings_are_used_even_when_default_settings_are_visible(window, monkeypatch):
    alice = window._add_token("alice----" + jwt())
    bob = window.store.add(jwt({"sub": "76561198000000001"}), "bob")
    window._select_tab("settings")
    window._edit_selected_settings()
    window._toggle_private_login()
    window.cs2_options_var.set("-console")
    window._toggle_use_cs2_launch_options()
    assert not window.store.preferences["private_login"]
    window._change_settings_scope(None)
    assert not window.private_login
    received = []
    def login(name, token, **options):
        received.append((name, options["private_login"], options["cs2_launch_options"]))
        return {"preserved_accounts": 0, "sign_in": "confirmed"}
    monkeypatch.setattr(gui, "login_account", login)
    window._select_tab("accounts")
    assert "Friends offline" in window.login_summary.cget("text")
    window._login_to_steam()
    finish(window)
    window._use_account(bob)
    window._login_to_steam()
    finish(window)
    assert received == [("alice", True, "-console"), ("bob", False, None)]
    window._use_account(alice)
    window._edit_selected_settings()
    window._reset_profile()
    assert window.store.effective_preferences(alice["id"]) == window.store.preferences


def test_local_session_status_tracks_matching_other_and_closed_steam(window, monkeypatch):
    alice = window._add_token("alice----" + jwt())
    bob = window.store.add(jwt({"sub": "76561198000000001"}), "bob")
    monkeypatch.setattr(window, "_check_cooldown", lambda *a, **k: None)
    monkeypatch.setattr(gui, "current_steam_session", lambda: (alice["subject"], 123))
    window._watch_steam_session()
    assert "Signed into this account" in detail_text(window)
    window._use_account(bob)
    assert "another account" in detail_text(window)
    monkeypatch.setattr(gui, "current_steam_session", lambda: None)
    window._watch_steam_session()
    assert "No active Steam sign-in" in detail_text(window)


def test_presence_result_stays_with_its_account_and_unknown_does_not_claim_offline(window):
    alice = window._add_token("alice----" + jwt())
    window._stop_presence_check()
    events = gui.queue.Queue()
    events.put({"state": "online", "message": "Online", "checked_at": int(time.time())})
    window._presence_worker = (alice["subject"], threading.Event(), events, None)
    window._poll_presence()
    assert "Public Friends status: Online" in detail_text(window)
    bob = window.store.add(jwt({"sub": "76561198000000001"}), "bob")
    window._use_account(bob)
    assert "Public Friends status: Online" not in detail_text(window)
    window._stop_presence_check()
    events = gui.queue.Queue()
    events.put({"state": "unknown", "message": "Private profile", "checked_at": int(time.time())})
    window._presence_worker = (bob["subject"], threading.Event(), events, None)
    window._poll_presence()
    assert "Public Friends status: Private profile" in detail_text(window)
    assert "Public Friends status: Appears offline" not in detail_text(window)


def test_recovery_ui_restores_selection_and_preserves_current_file(window, monkeypatch):
    alice = window._add_token("alice----" + jwt())
    window.store.set_alias(alice["id"], "Restored alias")
    window.store.save()
    backup = window.store.path + ".bak"
    Path(window.store.path).write_bytes(b"damaged saved data")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: backup)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    window._recover_data()
    assert window.current_account["alias"] == "Restored alias"
    assert "Backup restored" in window.data_status.cget("text")
    preserved = list(Path(window.store.path).parent.glob("accounts-preserved-*.json"))
    assert preserved[0].read_bytes() == b"damaged saved data"


@pytest.mark.parametrize("width", [540, 840])
def test_manual_resize_reflows_cards_and_keeps_actions_visible(window, width):
    window._add_token("alice----" + jwt())
    window.root.deiconify()
    window.root.geometry(f"{width}x650")
    window.root.update()
    window._apply_resize()
    window.root.update_idletasks()
    assert window._content_width == width - 2 * gui.PAD
    assert window.details_card.winfo_width() == width - 2 * gui.PAD
    assert window.login_btn.winfo_rootx() + window.login_btn.winfo_width() <= window.root.winfo_rootx() + width - gui.PAD
    assert window.login_btn.winfo_rooty() + window.login_btn.winfo_height() <= window.root.winfo_rooty() + 650 - gui.PAD
    window._select_tab("settings")
    window.root.update()
    assert window.root.winfo_width() == width and window.root.winfo_height() == 650
    assert window.viewport.yview()[1] < 1


def test_new_claims_fit_after_resizing_to_minimum_width(window):
    window._add_token("alice----" + jwt())
    window.root.deiconify()
    window.root.geometry("520x650")
    window.root.update()
    window._apply_resize()
    window._toggle_claims()
    window.root.update_idletasks()
    inner = window.details_inner
    for widget in inner.winfo_children():
        if isinstance(widget, tk.Label) and widget.grid_info().get("column") == 1:
            assert widget.winfo_x() + widget.winfo_width() <= inner.winfo_width()
            assert widget.winfo_reqwidth() <= widget.winfo_width()


def test_batch_selection_updates_the_account_settings_scope(window, monkeypatch):
    window._add_token("alice----" + jwt())
    window._edit_selected_settings()
    monkeypatch.setattr(gui, "extract_from_file", lambda _: gui.parse_token_input(
        "bob----" + jwt({"sub": "76561198000000001"})))
    window._start_imports(["bob.exe"])
    finish_import(window)
    assert window.current_account["username"] == "bob"
    assert window._settings_account_id == window.current_account["id"]
    assert "Editing bob" in window.settings_scope_note.cget("text")


def test_old_online_result_is_marked_stale_and_refresh_requests_are_throttled(window, monkeypatch):
    account = window._add_token("alice----" + jwt())
    window._stop_presence_check()
    window._presence[account["subject"]] = {"state": "online", "message": "Online",
                                          "checked_at": int(time.time()) - 100}
    window._update_presence_label()
    assert "Last known" in window.public_presence_label.cget("text")
    assert "stale" in window.public_presence_label.cget("text")
    assert window.public_presence_label.cget("fg") == gui.FG_MUTED
    window._presence[account["subject"]]["checked_at"] = int(time.time())
    monkeypatch.setattr(gui, "fetch_presence", lambda *_: pytest.fail("Fresh result was fetched again"))
    window._ensure_presence()
    assert window._presence_worker is None
