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
import copy
import json
import os
import re
import time
import tempfile

from extractor import DEFAULT_SEPARATOR, ExtractionError, decode_token
from steam_login import SteamLoginError, validate_cs2_launch_options, validate_cs2_settings_source

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
                            "use_cs2_launch_options": False, "cs2_launch_options": "",
                            "cs2_settings_source": ""}
        self.cooldowns = {}
        self.selected_account_id = None
        self.load_error = None
        self.last_add_action = "added"
        self.accounts: list[dict] = self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            data = self._read_snapshot(self.path)
        except FileNotFoundError:
            return []
        except (OSError, ValueError, SteamLoginError, RecursionError):
            self.load_error = "Saved data could not be loaded. Recover a backup in Settings; the existing file is unchanged."
            return []
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
            try:
                self.preferences["cs2_settings_source"] = validate_cs2_settings_source(
                    data["preferences"].get("cs2_settings_source", ""))
            except SteamLoginError:
                self.preferences["cs2_settings_source"] = ""
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, list):
            return []
        accounts = [dict(a) for a in accounts]
        for account in accounts:
            # Older versions offered a toggle. It can no longer disable copying.
            if isinstance(account.get("preferences"), dict):
                account["preferences"].pop("keep_cs2_settings", None)
            account.setdefault("id", _account_id(account["token"]))
            account.setdefault("alias", "")
            account.setdefault("last_used", 0)
            account.setdefault("separator", DEFAULT_SEPARATOR)
        selected = data.get("selected_account")
        if isinstance(selected, str) and any(a.get("id") == selected for a in accounts):
            self.selected_account_id = selected
        return accounts

    @staticmethod
    def _read_snapshot(path):
        with open(path, "rb") as handle:
            raw = handle.read(64 * 1024 * 1024 + 1)
        return Store._decode_snapshot(raw)

    @staticmethod
    def _decode_snapshot(raw):
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError("Saved data is too large.")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate saved-data key.")
                result[key] = value
            return result
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise ValueError("Invalid saved-data structure.")
        if type(data.get("version", 1)) is not int or data.get("version", 1) != SCHEMA_VERSION:
            raise ValueError("Unsupported saved-data version.")
        ids = set()
        for account in data["accounts"]:
            if (not isinstance(account, dict) or not isinstance(account.get("token"), str)
                    or not account["token"] or not isinstance(account.get("username"), str)
                    or not account["username"] or not isinstance(account.get("alias", ""), str)
                    or not isinstance(account.get("separator", DEFAULT_SEPARATOR), str)
                    or type(account.get("last_used", 0)) is not int):
                raise ValueError("Invalid saved account.")
            account_id = account.get("id", _account_id(account["token"]))
            if not isinstance(account_id, str) or not account_id or account_id in ids:
                raise ValueError("Invalid saved account identity.")
            ids.add(account_id)
            overrides = account.get("preferences", {})
            Store._validate_preferences(overrides)
        return data

    @staticmethod
    def _validate_preferences(values):
        if not isinstance(values, dict) or set(values) - {"private_login", "disable_cloud_sync", "use_cs2_launch_options", "cs2_launch_options", "keep_cs2_settings", "cs2_settings_source"}:
            raise ValueError("Invalid account settings.")
        for name, value in values.items():
            if name == "cs2_launch_options":
                validate_cs2_launch_options(value)
            elif name == "cs2_settings_source":
                validate_cs2_settings_source(value)
            elif type(value) is not bool:
                raise ValueError("Invalid account setting value.")

    @staticmethod
    def _atomic_bytes(path, content):
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".jwtractor-", dir=folder)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self) -> None:
        if self.load_error:
            raise OSError(self.load_error)
        payload = {"version": SCHEMA_VERSION, "accounts": self.accounts, "preferences": self.preferences,
                   "cooldowns": self.cooldowns, "selected_account": self.selected_account_id}
        content = json.dumps(payload, indent=2, allow_nan=False).encode("utf-8")
        if os.path.exists(self.path):
            try:
                with open(self.path, "rb") as handle:
                    previous = handle.read(64 * 1024 * 1024 + 1)
                self._decode_snapshot(previous)
            except (OSError, ValueError, SteamLoginError, RecursionError):
                raise OSError("The saved data changed or became unreadable. Restart JWTractor to recover it; no files were replaced.") from None
            self._atomic_bytes(self.path + ".bak", previous)
        else:
            self._atomic_bytes(self.path + ".bak", content)
        self._atomic_bytes(self.path, content)

    def recover(self, source=None):
        """Validate first, preserve the original, then atomically restore a snapshot."""
        source = os.path.abspath(source or self.path + ".bak")
        if os.path.normcase(source) == os.path.normcase(os.path.abspath(self.path)):
            raise OSError("Choose a backup, rather than the current accounts file.")
        try:
            with open(source, "rb") as handle:
                content = handle.read(64 * 1024 * 1024 + 1)
            self._decode_snapshot(content)
        except (OSError, ValueError, SteamLoginError, RecursionError):
            raise OSError("That backup is unreadable or invalid. The current saved data is unchanged.") from None
        preserved = None
        if os.path.exists(self.path):
            with open(self.path, "rb") as handle:
                original = handle.read()
            descriptor, preserved = tempfile.mkstemp(prefix="accounts-preserved-", suffix=".json",
                                                     dir=os.path.dirname(os.path.abspath(self.path)))
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
        self._atomic_bytes(self.path, content)
        self.preferences = {"private_login": False, "disable_cloud_sync": True,
                            "use_cs2_launch_options": False, "cs2_launch_options": "",
                            "cs2_settings_source": ""}
        self.cooldowns = {}
        self.selected_account_id = None
        self.load_error = None
        self.accounts = self._load()
        return preserved

    def effective_preferences(self, account_id=None):
        account = self.get(account_id)
        return dict(self.preferences, **(account.get("preferences", {}) if account else {}))

    def set_account_preferences(self, account_id, values):
        self._validate_preferences(values)
        values = dict(values)
        values.pop("keep_cs2_settings", None)
        account = self.get(account_id)
        if account is None:
            raise ValueError("Select a saved account first.")
        previous = copy.deepcopy(account.get("preferences"))
        account["preferences"] = dict(values)
        try:
            self.save()
        except OSError:
            if previous is None:
                account.pop("preferences", None)
            else:
                account["preferences"] = previous
            raise

    def reset_account_preferences(self, account_id):
        account = self.get(account_id)
        if account and "preferences" in account:
            previous = account.pop("preferences")
            try:
                self.save()
            except OSError:
                account["preferences"] = previous
                raise

    # -- queries -------------------------------------------------------------
    def get(self, account_id: str) -> dict | None:
        for acc in self.accounts:
            if acc.get("id") == account_id:
                return acc
        return None

    def ordered(self) -> list[dict]:
        """All accounts, most-recently-used first."""
        return [account for _, account in sorted(enumerate(self.accounts),
                key=lambda item: (item[1].get("last_used", 0), item[0]), reverse=True)]

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

    def set_cs2_settings_source(self, source: str) -> None:
        self._set_preference("cs2_settings_source", validate_cs2_settings_source(source))

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

        existing = next((a for a in self.accounts if a["token"] == token), None)
        matches = []
        if issuer == "steam" and isinstance(subject, str) and re.fullmatch(r"[0-9]{17}", subject):
            for account in self.ordered():
                try:
                    claims = decode_token(account["token"]).get("payload")
                except ExtractionError:
                    continue
                if isinstance(claims, dict) and claims.get("iss") == "steam" and claims.get("sub") == subject:
                    matches.append(account)
            existing = existing or next((a for a in matches if a["id"] == self.selected_account_id), None) or (matches[0] if matches else None)
        previous_selection = self.selected_account_id
        if existing is not None:
            if type(exp) is int and any(type(a.get("exp")) is int and exp < a["exp"] for a in matches):
                raise ExtractionError("This account already has a newer token. Its saved token is unchanged.")
            previous = copy.deepcopy(existing)
            previous_accounts = self.accounts
            existing.update(
                token=token, username=previous["username"] if previous["token"] != token and matches else username,
                separator=separator, last_used=now,
                issuer=issuer, subject=subject, exp=exp,
            )
            self.accounts = [a for a in self.accounts if a is existing or a not in matches]
            self.selected_account_id = existing["id"]
            try:
                self.save()
            except OSError:
                existing.clear()
                existing.update(previous)
                self.accounts = previous_accounts
                self.selected_account_id = previous_selection
                raise
            self.last_add_action = "refreshed" if previous["token"] != token else "existing"
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
        self.selected_account_id = account["id"]
        try:
            self.save()
        except OSError:
            self.accounts.remove(account)
            self.selected_account_id = previous_selection
            raise
        self.last_add_action = "added"
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
            previous_selection = self.selected_account_id
            acc["last_used"] = int(time.time())
            self.selected_account_id = acc["id"]
            try:
                self.save()
            except OSError:
                acc["last_used"] = previous
                self.selected_account_id = previous_selection
                raise

    def remove(self, account_id: str) -> None:
        previous = self.accounts
        previous_selection = self.selected_account_id
        self.accounts = [a for a in previous if a.get("id") != account_id]
        if len(self.accounts) != len(previous):
            if self.selected_account_id == account_id:
                self.selected_account_id = None
            try:
                self.save()
            except OSError:
                self.accounts = previous
                self.selected_account_id = previous_selection
                raise
