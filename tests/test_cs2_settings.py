"""Shared CS2 settings: synthetic files, fake accounts and mocked Steam only."""
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import subprocess

import pytest

import cs2_settings as cs
import steam_login as sl
from store import Store
from test_steam_login import ALICE, BOB, DAVE, jwt, remembered, workflow


VIDEO = b'"video"\r\n{ "setting.defaultres" "1920" }\r\n'
KEYS = b'<!-- kv3 encoding:text:version{synthetic} -->\n{ bindings = { W = "+forward" } }\n'
CONVARS = b'"config" { "convars" { "sensitivity" "1.25" } }\n'


def seed(root, steam_id, contents=(VIDEO, KEYS, CONVARS)):
    folder = cs.settings_directory(root, steam_id)
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in zip(cs.REQUIRED_FILES, contents):
        (folder / name).write_bytes(content)
    return folder


@pytest.fixture
def game(workflow, monkeypatch):
    root = workflow.paths[0].parent.parent
    source = seed(root, ALICE)
    target = seed(root, DAVE, (b"old video", b"old keys", b"old mouse"))
    monkeypatch.setattr(cs, "current_steam_session", lambda: None)
    monkeypatch.setattr(cs, "ensure_cs2_closed", lambda: None)
    return SimpleNamespace(root=root, source=source, target=target, workflow=workflow)


def login(**kwargs):
    return sl.login_account("dave", jwt({"sub": DAVE}), **kwargs)


def test_copy_exact_bytes_backups_cloud_protection_and_no_unrelated_files(game):
    original = [(game.target / name).read_bytes() for name in cs.REQUIRED_FILES]
    for name in ("cs2_user_keys_0_slot0.vcfg_lastclouded", "autoexec.cfg", "unrelated.txt"):
        (game.source / name).write_bytes(b"must not copy")
    result = login()
    assert result["cs2_settings_copied"] == 3
    assert result["cs2_settings_applied"] is True
    assert result["disable_cloud_sync"] is True
    assert result["preserved_accounts"] == 2
    for name, before in zip(cs.REQUIRED_FILES, original):
        assert (game.target / name).read_bytes() == (game.source / name).read_bytes()
        assert next(game.target.glob(name + ".*.bak")).read_bytes() == before
    assert not (game.target / "autoexec.cfg").exists()
    assert not (game.target / "cs2_user_keys_0_slot0.vcfg_lastclouded").exists()
    cloud = game.root / "userdata" / str(int(DAVE) & 0xffffffff) / "7" / "remote" / "sharedconfig.vdf"
    assert sl._section(sl.read_config(cloud).data, "UserRoamingConfigStore", "Software", "Valve", "Steam")["cloudenabled"] == "0"
    assert game.workflow.calls[-1] == "launch"


def test_fixed_source_overrides_active_and_previous_account(game, monkeypatch):
    source = seed(game.root, BOB, (b"fixed video", b"fixed keys", b"fixed mouse"))
    monkeypatch.setattr(cs, "current_steam_session", lambda: (ALICE, 123))
    login(cs2_settings_source=BOB)
    assert (game.target / cs.REQUIRED_FILES[0]).read_bytes() == (source / cs.REQUIRED_FILES[0]).read_bytes()


def test_automatic_source_prefers_current_session(game, monkeypatch):
    seed(game.root, BOB, (b"active video", b"active keys", b"active mouse"))
    monkeypatch.setattr(cs, "current_steam_session", lambda: (BOB, 123))
    login()
    assert (game.target / cs.REQUIRED_FILES[0]).read_bytes() == b"active video"


def test_read_fresh_source_after_shutdown_and_capture_identity_before_shutdown(game, monkeypatch):
    active = [ALICE]
    monkeypatch.setattr(cs, "current_steam_session", lambda: (active[0], 123))
    def close(*args):
        active[0] = DAVE
        (game.source / cs.REQUIRED_FILES[0]).write_bytes(b"latest flushed video")
        game.workflow.calls.append("close")
    monkeypatch.setattr(sl, "close_steam", close)
    login()
    assert (game.target / cs.REQUIRED_FILES[0]).read_bytes() == b"latest flushed video"


def test_optional_machine_and_extra_slots_copied_only_when_present(game):
    for name in cs.OPTIONAL_FILES:
        (game.source / name).write_bytes(b"synthetic optional config")
    result = login()
    assert result["cs2_settings_copied"] == 3 + len(cs.OPTIONAL_FILES)
    assert all((game.target / name).read_bytes() == b"synthetic optional config" for name in cs.OPTIONAL_FILES)


@pytest.mark.parametrize("source", [None, 123, "../../bad", "0" * 17, "9" * 17])
def test_invalid_source_rejected_before_shutdown(game, source):
    with pytest.raises(sl.SteamLoginError, match="settings source"):
        login(cs2_settings_source=source)
    assert game.workflow.calls == []


@pytest.mark.parametrize("content", [None, b"", b"\xff\xfe\x00", b"x" * (cs.MAX_SETTINGS_FILE + 1)],
                         ids=["missing", "empty", "invalid-encoding", "oversized"])
def test_bad_source_files_fail_before_shutdown(game, content):
    path = game.source / cs.REQUIRED_FILES[0]
    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)
    with pytest.raises(sl.SteamLoginError):
        login()
    assert game.workflow.calls == []
    assert (game.target / cs.REQUIRED_FILES[0]).read_bytes() == b"old video"


def test_source_equals_target_and_identical_content_do_not_replace_files(game):
    result = login(cs2_settings_source=DAVE)
    assert result["cs2_settings_copied"] == 0
    assert result["cs2_settings_state"] == "same_account"
    assert not list(game.target.glob("*.bak"))
    seed(game.root, DAVE)
    result = login(cs2_settings_source=ALICE)
    assert result["cs2_settings_copied"] == 0
    assert result["cs2_settings_state"] == "unchanged"
    assert not list(game.target.glob("*.bak"))


def test_no_previous_cs2_settings_does_not_block_steam_login(game, monkeypatch):
    for name in cs.REQUIRED_FILES:
        (game.source / name).unlink()
    def unexpected():
        pytest.fail("Game process should not be queried when no settings are available")
    monkeypatch.setattr(cs, "ensure_cs2_closed", unexpected)
    result = sl.login_account("dave", jwt({"sub": DAVE}))
    assert result["cs2_settings_applied"] is False
    assert result["cs2_settings_state"] == "unavailable"
    assert result["disable_cloud_sync"] is False
    assert (game.target / cs.REQUIRED_FILES[0]).read_bytes() == b"old video"


@pytest.mark.parametrize("after_shutdown", [False, True])
def test_running_game_blocks_settings_and_credentials_updates(game, monkeypatch, after_shutdown):
    before = [path.read_bytes() for path in game.workflow.paths]
    def check():
        if not after_shutdown or "close" in game.workflow.calls:
            raise sl.SteamLoginError("Close CS2 before switching accounts")
    monkeypatch.setattr(cs, "ensure_cs2_closed", check)
    with pytest.raises(sl.SteamLoginError, match="Close CS2"):
        login()
    assert game.workflow.calls == (["close"] if after_shutdown else [])
    assert [path.read_bytes() for path in game.workflow.paths] == before


@pytest.mark.parametrize("failure", ["disk", "cancel", "source_changed"])
def test_game_write_failure_rolls_back_login_and_game_files(game, monkeypatch, failure):
    paths = game.workflow.paths + [game.target / name for name in cs.REQUIRED_FILES]
    before = [path.read_bytes() for path in paths]
    cancel = threading.Event()
    replace = sl.os.replace
    def interfere(source, target):
        if target == game.target / cs.REQUIRED_FILES[1] and failure == "disk":
            raise PermissionError("Synthetic game write failure")
        replace(source, target)
        if target == game.target / cs.REQUIRED_FILES[0]:
            if failure == "cancel":
                cancel.set()
            elif failure == "source_changed":
                (game.source / cs.REQUIRED_FILES[0]).write_bytes(b"external edit")
    monkeypatch.setattr(sl.os, "replace", interfere)
    with pytest.raises(sl.SteamLoginError):
        login(cancel=cancel)
    assert [path.read_bytes() for path in paths] == before
    assert game.workflow.calls == ["close"]
    assert not list(game.root.rglob("*.tmp"))
    assert not list(game.root.rglob("*.lock"))
    cloud = game.root / "userdata" / str(int(DAVE) & 0xffffffff) / "7" / "remote" / "sharedconfig.vdf"
    assert not cloud.exists()


def test_new_target_files_removed_on_failed_transaction(game, monkeypatch):
    for name in cs.REQUIRED_FILES:
        (game.target / name).unlink()
    cancel = threading.Event()
    replace = sl.os.replace
    def interrupt(source, target):
        replace(source, target)
        if target == game.target / cs.REQUIRED_FILES[-1]:
            cancel.set()
    monkeypatch.setattr(sl.os, "replace", interrupt)
    with pytest.raises(sl.LoginCancelled):
        login(cancel=cancel)
    assert not any((game.target / name).exists() for name in cs.REQUIRED_FILES)


def test_destination_external_edit_preserved_and_login_rolled_back(game, monkeypatch):
    before = [p.read_bytes() for p in game.workflow.paths]
    replace = sl.os.replace
    def interfere(source, target):
        replace(source, target)
        if target == game.workflow.paths[0]:
            (game.target / cs.REQUIRED_FILES[0]).write_bytes(b"external destination")
    monkeypatch.setattr(sl.os, "replace", interfere)
    with pytest.raises(sl.SteamLoginError, match="changed during login"):
        login()
    assert [p.read_bytes() for p in game.workflow.paths] == before
    assert (game.target / cs.REQUIRED_FILES[0]).read_bytes() == b"external destination"
    assert game.workflow.calls == ["close"]


def test_source_selection_modern_legacy_timestamp_and_ambiguity(monkeypatch):
    monkeypatch.setattr(cs, "current_steam_session", lambda: None)
    users = remembered()[1]
    assert cs.resolve_source("", users) == ALICE
    users["users"][BOB]["AutoLogin"] = "1"
    assert cs.resolve_source("", users) == BOB
    users["users"][BOB].pop("AutoLogin")
    users["users"][ALICE]["MostRecent"] = "0"
    assert cs.resolve_source("", users) == BOB
    users["users"][ALICE]["Timestamp"] = "101"
    with pytest.raises(sl.SteamLoginError, match="Choose a CS2 settings source"):
        cs.resolve_source("", users)
    with pytest.raises(sl.SteamLoginError):
        cs.resolve_source("", {})


def test_game_process_detection_uses_cs2_only_and_errors_are_safe(monkeypatch):
    monkeypatch.setattr(cs, "_run", lambda args: SimpleNamespace(stdout='"cs2.exe","123"'))
    with pytest.raises(sl.SteamLoginError, match="Close CS2"):
        cs.ensure_cs2_closed()
    monkeypatch.setattr(cs, "_run", lambda args: SimpleNamespace(stdout='"other.exe","123"'))
    cs.ensure_cs2_closed()
    def fail(args):
        raise subprocess.TimeoutExpired("tasklist", 15)
    monkeypatch.setattr(cs, "_run", fail)
    with pytest.raises(sl.SteamLoginError, match="Could not check"):
        cs.ensure_cs2_closed()


def test_links_are_rejected_before_copy(game, tmp_path):
    link = game.source / cs.REQUIRED_FILES[0]
    link.unlink()
    actual = tmp_path / "outside.txt"
    actual.write_bytes(b"outside")
    try:
        link.symlink_to(actual)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode")
    with pytest.raises(sl.SteamLoginError, match="symbolic link"):
        login()
    assert actual.read_bytes() == b"outside"
    assert game.workflow.calls == []


def test_store_defaults_overrides_persistence_and_failed_save(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    store = Store(str(path))
    account = store.add(jwt({"sub": DAVE}), "dave")
    store.set_cs2_settings_source(ALICE)
    store.set_account_preferences(account["id"], {"cs2_settings_source": BOB})
    loaded = Store(str(path))
    assert "keep_cs2_settings" not in loaded.effective_preferences(account["id"])
    assert loaded.effective_preferences(account["id"])["cs2_settings_source"] == BOB
    loaded.reset_account_preferences(account["id"])
    assert loaded.effective_preferences(account["id"])["cs2_settings_source"] == ALICE
    def fail():
        raise OSError("Synthetic save failure")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError):
        store.set_cs2_settings_source(BOB)
    assert store.preferences["cs2_settings_source"] == ALICE
    with pytest.raises(sl.SteamLoginError):
        store.set_account_preferences(account["id"], {"cs2_settings_source": "bad"})


def test_invalid_global_source_falls_back_to_automatic(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": [], "preferences": {"keep_cs2_settings": True, "cs2_settings_source": "bad"}}))
    store = Store(str(path))
    assert "keep_cs2_settings" not in store.preferences
    assert store.preferences["cs2_settings_source"] == ""


def test_legacy_off_preferences_cannot_disable_automatic_copying(game, tmp_path):
    path = tmp_path / "accounts.json"
    store = Store(str(path))
    account = store.add(jwt({"sub": DAVE}), "dave")
    data = json.loads(path.read_text())
    data["preferences"]["keep_cs2_settings"] = False
    data["accounts"][0]["preferences"] = {"keep_cs2_settings": False, "cs2_settings_source": ALICE}
    path.write_text(json.dumps(data))
    loaded = Store(str(path))
    effective = loaded.effective_preferences(account["id"])
    assert "keep_cs2_settings" not in effective
    assert effective["cs2_settings_source"] == ALICE
    result = login(cs2_settings_source=effective["cs2_settings_source"])
    assert result["cs2_settings_copied"] == 3
    loaded.save()
    saved = json.loads(path.read_text())
    assert "keep_cs2_settings" not in saved["preferences"]
    assert "keep_cs2_settings" not in saved["accounts"][0]["preferences"]


def test_first_steam_account_without_any_history_can_still_log_in(game):
    game.workflow.paths[1].write_bytes(b'"users" {}')
    result = login()
    assert result["cs2_settings_state"] == "unavailable"
    assert game.workflow.calls[-1] == "launch"


def test_source_picker_lists_complete_remembered_accounts_without_saved_tokens(game):
    # The source is known to Steam, not necessarily JWTractor's token store.
    assert cs.list_settings_sources(game.root) == [{"steam_id": ALICE, "name": "alice"}]
    seed(game.root, BOB)
    assert cs.list_settings_sources(game.root) == [
        {"steam_id": ALICE, "name": "alice"}, {"steam_id": BOB, "name": "bob"}]
    (game.source / cs.REQUIRED_FILES[0]).unlink()
    assert cs.list_settings_sources(game.root) == [{"steam_id": BOB, "name": "bob"}]


def test_source_discovery_returns_empty_for_missing_or_malformed_steam_data(tmp_path):
    assert cs.list_settings_sources(tmp_path) == []
    config = tmp_path / "config"
    config.mkdir()
    (config / "loginusers.vdf").write_text("invalid {")
    assert cs.list_settings_sources(tmp_path) == []
