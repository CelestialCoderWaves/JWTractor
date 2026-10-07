"""Tests for the on-disk account store (``store.py``).

Every Store here is pointed at a temp file, so nothing touches the real config
directory. Tokens are synthetic — no real account data.
"""

import base64
import json
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import (  # noqa: E402
    Store,
    account_combined,
    account_label,
    account_status,
    default_store_path,
)


def _mk_token(header, payload, sig="AAAABBBBCCCCDDDD"):
    def enc(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{enc(header)}.{enc(payload)}.{sig}"


ACTIVE = _mk_token({"alg": "EdDSA", "typ": "JWT"}, {"iss": "example", "exp": 9999999999})
EXPIRED = _mk_token({"alg": "EdDSA", "typ": "JWT"}, {"iss": "example", "exp": 1000})
NO_EXP = _mk_token({"alg": "none"}, {"iss": "example"})


def test_add_and_persist(tmp_path):
    path = str(tmp_path / "accounts.json")
    store = Store(path)
    acc = store.add(ACTIVE, "ChadGreen")
    assert acc["username"] == "ChadGreen"
    assert acc["issuer"] == "example"

    # A fresh Store on the same path sees the saved account.
    assert len(Store(path).accounts) == 1


@pytest.mark.parametrize("operation", ["touch", "remove"])
def test_account_mutation_save_failure_rolls_back(tmp_path, monkeypatch, operation):
    store = Store(str(tmp_path / 'accounts.json'))
    account = store.add(ACTIVE, "Alice")
    account["last_used"] = 123
    def fail():
        raise PermissionError("Synthetic failure")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError):
        getattr(store, operation)(account["id"])
    assert store.accounts == [account]
    assert account["last_used"] == 123


def test_add_dedupes_same_token(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    store.add(ACTIVE, "OldName")
    store.add(ACTIVE, "NewName")  # same token -> update, not duplicate
    assert len(store.accounts) == 1
    assert store.accounts[0]["username"] == "NewName"


def test_distinct_tokens_kept_separate(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    store.add(ACTIVE, "A")
    store.add(EXPIRED, "B")
    assert len(store.accounts) == 2


def test_set_alias_and_label(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    acc = store.add(ACTIVE, "18005551234")
    assert account_label(acc) == "18005551234"  # falls back to username
    store.set_alias(acc["id"], "  My burner  ")
    assert account_label(store.get(acc["id"])) == "My burner"  # trimmed alias


def test_status(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    active = store.add(ACTIVE, "A")
    expired = store.add(EXPIRED, "B")
    unknown = store.add(NO_EXP, "C")
    assert account_status(active) == "not expired"
    assert account_status(expired) == "expired"
    assert account_status(unknown) == "unknown"


def test_combined_roundtrip(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    acc = store.add(ACTIVE, "ChadGreen")
    assert account_combined(acc) == f"ChadGreen----{ACTIVE}"


def test_remove(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    acc = store.add(ACTIVE, "A")
    store.remove(acc["id"])
    assert store.accounts == []
    assert Store(str(tmp_path / "accounts.json")).accounts == []


def test_ordered_by_last_used(tmp_path):
    store = Store(str(tmp_path / "accounts.json"))
    first = store.add(ACTIVE, "First")
    second = store.add(EXPIRED, "Second")
    first["last_used"] = int(time.time()) + 100  # pretend "First" was just used
    store.save()
    assert [a["id"] for a in store.ordered()] == [first["id"], second["id"]]


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert Store(str(path)).accounts == []


def test_default_store_path_env_override(monkeypatch):
    monkeypatch.setenv("JWTRACTOR_STORE", r"X:\custom\accounts.json")
    assert default_store_path() == r"X:\custom\accounts.json"


def test_failed_add_does_not_leave_an_unsaved_account(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "accounts.json"))
    def fail():
        raise PermissionError("Synthetic write failure")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(PermissionError):
        store.add(ACTIVE, "alice")
    assert store.accounts == []


def test_failed_update_keeps_existing_account_and_alias(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "accounts.json"))
    account = store.add(ACTIVE, "alice")
    store.set_alias(account["id"], "Main")
    before = dict(account)
    def fail():
        raise PermissionError("Synthetic write failure")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(PermissionError):
        store.add(ACTIVE, "different_name")
    assert account == before
    assert store.accounts == [account]
