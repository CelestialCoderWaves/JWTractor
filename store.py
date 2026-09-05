"""Local, on-disk store of extracted accounts (tokens + your own aliases).

JWTractor saves each token you pull so you can re-select an account you've used
before — one that may still be active — instead of hunting down the original
``.exe``. You can give each one a friendly **alias** (e.g. "main" or "burner")
so a cryptic username like a spam phone number is easy to recognise later.

The data lives as plain JSON in your user config directory (see
``default_store_path``). Treat that file as sensitive: it contains real tokens,
which are credentials. This module is deliberately GUI-free so it can be unit
tested and reused.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from extractor import DEFAULT_SEPARATOR, decode_token

__all__ = [
    "Store",
    "default_store_path",
    "account_combined",
    "account_label",
    "account_status",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1


def default_store_path() -> str:
    """Where the accounts file lives (override with ``JWTRACTOR_STORE``)."""
    override = os.environ.get("JWTRACTOR_STORE")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    return os.path.join(base, "JWTractor", "accounts.json")


def _account_id(token: str) -> str:
    """A short, stable id for a token (so the same token de-duplicates)."""
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:12]


def account_combined(acc: dict) -> str:
    """The ``<username><sep><token>`` string this account copies to the clipboard."""
    return f"{acc['username']}{acc.get('separator', DEFAULT_SEPARATOR)}{acc['token']}"


def account_label(acc: dict) -> str:
    """The name to show for an account: its alias if set, else the username."""
    return (acc.get("alias") or "").strip() or acc.get("username") or "(unnamed)"


def account_status(acc: dict, now: float | None = None) -> str:
    """``"not expired"``, ``"expired"`` or ``"unknown"`` from the token's ``exp`` claim.

    "not expired" means the ``exp`` timestamp hasn't passed — the token may
    still have been revoked server-side (password change, session deauth).
    """
    exp = acc.get("exp")
    if exp is None:
        return "unknown"
    if now is None:
        now = time.time()
    try:
        return "expired" if int(exp) < now else "not expired"
    except (TypeError, ValueError):
        return "unknown"


class Store:
    """A collection of saved accounts, backed by a JSON file."""

    def __init__(self, path: str | None = None):
        self.path = path or default_store_path()
        self.accounts: list[dict] = self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []  # missing or corrupt file -> start empty, never crash
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, list):
            return []
        return [a for a in accounts if isinstance(a, dict) and a.get("token")]

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        payload = {"version": SCHEMA_VERSION, "accounts": self.accounts}
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, self.path)  # atomic swap so a crash can't truncate it

    # -- queries -------------------------------------------------------------
    def get(self, account_id: str) -> dict | None:
        for acc in self.accounts:
            if acc.get("id") == account_id:
                return acc
        return None

    def ordered(self) -> list[dict]:
        """All accounts, most-recently-used first."""
        return sorted(
            self.accounts, key=lambda a: a.get("last_used", 0), reverse=True
        )

    # -- mutations -----------------------------------------------------------
    def add(self, token: str, username: str, separator: str = DEFAULT_SEPARATOR) -> dict:
        """Add ``token`` (or refresh it if already stored) and return the account."""
        now = int(time.time())
        issuer = subject = exp = None
        try:  # cache a few claims for display; best-effort
            payload = decode_token(token).get("payload")
            if isinstance(payload, dict):
                issuer, subject, exp = payload.get("iss"), payload.get("sub"), payload.get("exp")
        except Exception:
            pass

        existing = self.get(_account_id(token))
        if existing is not None:
            existing.update(
                username=username, separator=separator, last_used=now,
                issuer=issuer, subject=subject, exp=exp,
            )
            self.save()
            return existing

        account = {
            "id": _account_id(token),
            "token": token,
            "username": username,
            "alias": "",
            "separator": separator,
            "added": now,
            "last_used": now,
            "issuer": issuer,
            "subject": subject,
            "exp": exp,
        }
        self.accounts.append(account)
        self.save()
        return account

    def set_alias(self, account_id: str, alias: str) -> None:
        acc = self.get(account_id)
        if acc is not None:
            acc["alias"] = (alias or "").strip()
            self.save()

    def touch(self, account_id: str) -> None:
        """Mark an account as just used (moves it to the top of ``ordered``)."""
        acc = self.get(account_id)
        if acc is not None:
            acc["last_used"] = int(time.time())
            self.save()

    def remove(self, account_id: str) -> None:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.get("id") != account_id]
        if len(self.accounts) != before:
            self.save()
