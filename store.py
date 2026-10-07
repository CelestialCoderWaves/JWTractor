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
import re
import time

from extractor import DEFAULT_SEPARATOR, decode_token
from steam_login import SteamLoginError, validate_cs2_launch_options

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
        self.preferences = {"private_login": False, "disable_cloud_sync": True,
                            "use_cs2_launch_options": False, "cs2_launch_options": ""}
        self.cooldowns = {}
        self.accounts: list[dict] = self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []  # missing or corrupt file -> start empty, never crash
        cooldowns = data.get("cooldowns") if isinstance(data, dict) else None
        if isinstance(cooldowns, dict):
            self.cooldowns = {steam_id: dict(result) for steam_id, result in cooldowns.items()
                              if self._valid_cooldown(steam_id, result)}
        if isinstance(data, dict) and isinstance(data.get("preferences"), dict):
            for name, default in self.preferences.items():
                value = data["preferences"].get(name, default)
                if type(default) is bool:
                    self.preferences[name] = value if type(value) is bool else default
            try:
                self.preferences["cs2_launch_options"] = validate_cs2_launch_options(
                    data["preferences"].get("cs2_launch_options"))
            except SteamLoginError:
                self.preferences["use_cs2_launch_options"] = False
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, list):
            return []
        return [a for a in accounts if isinstance(a, dict) and a.get("token")]

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        payload = {"version": SCHEMA_VERSION, "accounts": self.accounts, "preferences": self.preferences,
                   "cooldowns": self.cooldowns}
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
    @staticmethod
    def _valid_cooldown(steam_id, result):
        return (isinstance(steam_id, str) and re.fullmatch(r"[0-9]{17}", steam_id) is not None
                and isinstance(result, dict) and result.get("state") in ("active", "clear")
                and isinstance(result.get("message"), str) and 0 < len(result["message"]) <= 512
                and type(result.get("checked_at")) is int and 0 < result["checked_at"] <= time.time() + 300
                and (result.get("expires_at") is None or
                     (type(result["expires_at"]) is int and 0 < result["expires_at"] <= 253402300799)))

    def set_cooldown(self, steam_id, result, *, checked_at=None):
        """Only successful checks replace the last-known account result."""
        cached = {"state": result.get("state"), "message": result.get("message"),
                  "checked_at": int(time.time()) if checked_at is None else checked_at,
                  "expires_at": result.get("expires_at")}
        if not self._valid_cooldown(steam_id, cached):
            raise ValueError("Invalid cooldown result.")
        previous = self.cooldowns.get(steam_id)
        self.cooldowns[steam_id] = cached
        try:
            self.save()
        except OSError:
            if previous is None:
                self.cooldowns.pop(steam_id, None)
            else:
                self.cooldowns[steam_id] = previous
            raise

    def set_private_login(self, enabled: bool) -> None:
        self._set_preference("private_login", bool(enabled))

    def set_disable_cloud_sync(self, enabled: bool) -> None:
        self._set_preference("disable_cloud_sync", bool(enabled))

    def set_use_cs2_launch_options(self, enabled: bool) -> None:
        self._set_preference("use_cs2_launch_options", bool(enabled))

    def set_cs2_launch_options(self, options: str) -> None:
        self._set_preference("cs2_launch_options", validate_cs2_launch_options(options))

    def _set_preference(self, name: str, value) -> None:
        previous = self.preferences[name]
        self.preferences[name] = value
        try:
            self.save()
        except OSError:
            self.preferences[name] = previous
            raise

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
            previous = dict(existing)
            existing.update(
                username=username, separator=separator, last_used=now,
                issuer=issuer, subject=subject, exp=exp,
            )
            try:
                self.save()
            except OSError:
                existing.clear()
                existing.update(previous)
                raise
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
        try:
            self.save()
        except OSError:
            self.accounts.remove(account)
            raise
        return account

    def set_alias(self, account_id: str, alias: str) -> None:
        acc = self.get(account_id)
        if acc is not None:
            previous = acc.get("alias", "")
            acc["alias"] = (alias or "").strip()
            try:
                self.save()
            except OSError:
                acc["alias"] = previous
                raise

    def touch(self, account_id: str) -> None:
        """Mark an account as just used (moves it to the top of ``ordered``)."""
        acc = self.get(account_id)
        if acc is not None:
            previous = acc.get("last_used", 0)
            acc["last_used"] = int(time.time())
            try:
                self.save()
            except OSError:
                acc["last_used"] = previous
                raise

    def remove(self, account_id: str) -> None:
        previous = self.accounts
        self.accounts = [a for a in previous if a.get("id") != account_id]
        if len(self.accounts) != len(previous):
            try:
                self.save()
            except OSError:
                self.accounts = previous
                raise
