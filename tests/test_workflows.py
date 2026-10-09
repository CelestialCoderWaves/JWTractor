"""Account refresh, recovery, overrides and diagnostics use synthetic local data."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnostics import Diagnostics
from extractor import ExtractionError
from store import Store
from test_steam_login import jwt

ALICE = "76561198000000000"
BOB = "76561198000000001"


def test_refresh_replaces_token_without_losing_identity_alias_profile_or_cooldown(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    old = store.add(jwt({"exp": 2000000000}), "alice")
    store.set_alias(old["id"], "Main account")
    store.set_account_preferences(old["id"], {"disable_cloud_sync": False, "cs2_launch_options": "-console"})
    store.set_cooldown(ALICE, {"state": "clear", "message": "Saved result"})
    identity, added = old["id"], old["added"]
    fresh = jwt({"exp": 2100000000, "jti": "refreshed"})
    updated = store.add(fresh, "renamed_file")
    assert updated is old
    assert updated["id"] == identity and updated["added"] == added
    assert updated["username"] == "alice" and updated["alias"] == "Main account"
    assert updated["token"] == fresh
    assert len(store.accounts) == 1
    restored = Store(store.path)
    assert restored.selected_account_id == identity
    assert restored.effective_preferences(identity)["cs2_launch_options"] == "-console"
    assert restored.cooldowns[ALICE]["message"] == "Saved result"
    assert store.last_add_action == "refreshed"
    with pytest.raises(ExtractionError, match="newer token"):
        store.add(jwt({"exp": 2000000000}), "alice")
    assert old["token"] == fresh


def test_failed_refresh_rolls_back_original_token_and_selection(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "accounts.json"))
    alice = store.add(jwt(), "alice")
    bob = store.add(jwt({"sub": BOB}), "bob")
    original = dict(alice)
    monkeypatch.setattr(store, "save", lambda: (_ for _ in ()).throw(PermissionError("synthetic")))
    with pytest.raises(OSError):
        store.add(jwt({"jti": "new"}), "alice")
    assert alice == original
    assert store.selected_account_id == bob["id"]


def test_duplicate_legacy_accounts_consolidate_into_selected_record(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    old = store.add(jwt({"exp": 2000000000}), "alice")
    duplicate = dict(old, id="legacy-second", token=jwt({"exp": 2000000001}), alias="Selected alias")
    store.accounts.append(duplicate)
    store.selected_account_id = duplicate["id"]
    store.save()
    refreshed = store.add(jwt({"exp": 2100000000}), "alice")
    assert store.accounts == [refreshed]
    assert refreshed["id"] == "legacy-second"
    assert refreshed["alias"] == "Selected alias"


def test_reimporting_an_old_duplicate_cannot_discard_a_newer_saved_token(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    old = store.add(jwt({"exp": 2000000000}), "alice")
    newer = dict(old, id="newer-legacy", token=jwt({"exp": 2100000000}), exp=2100000000)
    store.accounts.append(newer)
    store.save()
    before = Path(store.path).read_bytes()
    with pytest.raises(ExtractionError, match="newer token"):
        store.add(old["token"], "alice")
    assert store.accounts == [old, newer]
    assert Path(store.path).read_bytes() == before


@pytest.mark.parametrize("damaged", [b"{ broken", b'{"accounts": [42]}', b'{"accounts":[],"version":999}',
                                     b'{"accounts":[],"accounts":[]}'])
def test_recovery_blocks_writes_validates_backup_and_preserves_damaged_original(tmp_path, damaged):
    path = tmp_path / "accounts.json"
    store = Store(str(path))
    alice = store.add(jwt(), "alice")
    store.set_alias(alice["id"], "Main")
    store.save()  # rolling backup includes the saved alias
    path.write_bytes(damaged)
    broken = Store(str(path))
    assert broken.load_error
    with pytest.raises(OSError):
        broken.add(jwt({"sub": BOB}), "bob")
    assert path.read_bytes() == damaged
    preserved = broken.recover()
    assert Path(preserved).read_bytes() == damaged
    assert broken.load_error is None
    assert broken.accounts[0]["alias"] == "Main"
    assert broken.selected_account_id == alice["id"]
    broken.set_private_login(True)
    assert Store(str(path)).preferences["private_login"]


def test_invalid_backup_never_replaces_current_file(tmp_path):
    path = tmp_path / "accounts.json"
    store = Store(str(path))
    store.add(jwt(), "alice")
    before = path.read_bytes()
    backup = tmp_path / "bad.json"
    backup.write_text('{"accounts":[{"token":1}]}')
    with pytest.raises(OSError, match="invalid"):
        store.recover(str(backup))
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("accounts-preserved-*.json"))


def test_save_detects_file_damage_while_program_is_open(tmp_path):
    path = tmp_path / "accounts.json"
    store = Store(str(path))
    store.add(jwt(), "alice")
    backup = Path(str(path) + ".bak").read_bytes()
    path.write_bytes(b"damaged outside app")
    with pytest.raises(OSError, match="no files were replaced"):
        store.set_private_login(True)
    assert path.read_bytes() == b"damaged outside app"
    assert Path(str(path) + ".bak").read_bytes() == backup
    assert store.preferences["private_login"] is False


def test_account_preferences_inherit_unmodified_defaults_and_reset(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "accounts.json"))
    alice = store.add(jwt(), "alice")
    bob = store.add(jwt({"sub": BOB}), "bob")
    store.set_account_preferences(alice["id"], {"private_login": True, "cs2_launch_options": "-console"})
    store.set_disable_cloud_sync(False)
    assert store.effective_preferences(alice["id"])["private_login"]
    assert not store.effective_preferences(alice["id"])["disable_cloud_sync"]
    assert not store.effective_preferences(bob["id"])["private_login"]
    reloaded = Store(store.path)
    assert reloaded.effective_preferences(alice["id"])["cs2_launch_options"] == "-console"
    original = dict(alice["preferences"])
    monkeypatch.setattr(store, "save", lambda: (_ for _ in ()).throw(PermissionError("synthetic")))
    with pytest.raises(OSError):
        store.reset_account_preferences(alice["id"])
    assert alice["preferences"] == original
    monkeypatch.undo()
    store.reset_account_preferences(alice["id"])
    assert store.effective_preferences(alice["id"]) == store.preferences


def test_diagnostics_never_logs_exception_text_or_exports_injected_content(tmp_path):
    diagnostics = Diagnostics(str(tmp_path))
    secret = jwt()
    diagnostics.record("login.failed", ValueError(f"token={secret} password=secret alice@domain.test"))
    diagnostics.record(secret, ValueError("not an allowed event"))
    raw = Path(diagnostics.path).read_text()
    assert "login.failed" in raw and "ValueError" in raw
    assert secret not in raw and "password" not in raw and "alice" not in raw
    with open(diagnostics.path, "a", encoding="utf-8") as handle:
        handle.write("\n" + secret + "\n")
    report = diagnostics.report()
    assert secret not in report
    assert "login.failed" in report
    diagnostics.close()
