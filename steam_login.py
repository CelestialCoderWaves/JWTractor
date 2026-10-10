"""SteamNFATool's local login workflow, implemented without Node.js.

Only explicit calls to login_account change Steam. Parsing, merging, and file
replacement are separate so tests can use synthetic accounts and temp files.
JWT claims are checked locally; the signature and actual sign-in are not verified.
"""

from __future__ import annotations

import base64
import copy
import ctypes
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass

from steam import find_steam_dir

MAX_INPUT = 64 * 1024
MAX_CONFIG = 16 * 1024 * 1024
IS_WINDOWS = os.name == "nt"


class SteamLoginError(Exception):
    """A user-facing failure; recovery_required keeps the installation locked."""

    def __init__(self, message, *, recovery_required=False):
        super().__init__(message)
        self.recovery_required = recovery_required


class LoginCancelled(SteamLoginError):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise LoginCancelled("Cancelled.")


def validate_account_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", name):
        raise SteamLoginError("Use the Steam login name (letters, numbers, _, -, . or @; up to 64 characters).")
    return name


def validate_token(token, now=None):
    if not isinstance(token, str) or len(token) > MAX_INPUT or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
        raise SteamLoginError("Invalid JWT format.")
    def decode(part):
        raw = base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != part:
            raise ValueError("Noncanonical base64url")
        return json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    try:
        header, payload = [decode(part) for part in token.split(".")[:2]]
        if not isinstance(header, dict):
            raise ValueError("Invalid header")
    except (ValueError, UnicodeError, RecursionError):
        raise SteamLoginError("Invalid JWT header or payload.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str) or not re.fullmatch(r"[0-9]{17}", payload["sub"]):
        raise SteamLoginError('This is not a Steam login token: "sub" must be a 17-digit SteamID.')
    for claim in ("exp", "nbf"):
        if claim in payload and (type(payload[claim]) is not int or not 0 <= payload[claim] <= 8640000000000):
            raise SteamLoginError(f'Token has an invalid "{claim}" timestamp.')
    now = time.time() if now is None else now
    if "exp" in payload and now >= payload["exp"]:
        raise SteamLoginError("Token expired. Extract a current token before logging in.")
    if "nbf" in payload and now < payload["nbf"]:
        raise SteamLoginError("Token is not valid yet.")
    if payload.get("iss") != "steam":
        raise SteamLoginError("Use a Steam-issued token for Steam login.")
    audiences = payload.get("aud")
    if not isinstance(audiences, list) or not all(isinstance(item, str) for item in audiences):
        raise SteamLoginError("Token is missing its Steam audience claims.")
    if "client" not in audiences:
        raise SteamLoginError("This token cannot sign in to the Steam desktop client. Use a client refresh token.")
    if "derive" not in audiences:
        raise SteamLoginError("This is an access token. Steam desktop login needs a refresh token.")
    return payload


def cache_key(name):
    return format(zlib.crc32(name.encode("utf-8")) & 0xffffffff, "x") + "1"


def parse_vdf(text):
    """Parse strict KeyValues, refusing duplicates or unsupported directives."""
    if len(text.encode("utf-8")) > MAX_CONFIG or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise SteamLoginError("VDF is too large or contains unsupported control characters.")
    position = 0
    def next_token():
        nonlocal position
        while position < len(text):
            if text[position].isspace():
                position += 1
            elif text.startswith("//", position):
                end = text.find("\n", position)
                position = len(text) if end < 0 else end + 1
            else:
                break
        if position == len(text):
            return None
        char = text[position]
        position += 1
        if char in "{}":
            return (char, char)
        if char == '"':
            value = []
            while position < len(text):
                char = text[position]
                position += 1
                if char == '"':
                    return ("string", "".join(value))
                if char == "\\":
                    if position == len(text):
                        raise SteamLoginError("Unterminated VDF escape.")
                    escaped = text[position]
                    position += 1
                    value.append({"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(escaped, "\\" + escaped))
                else:
                    value.append(char)
            raise SteamLoginError("Unterminated VDF string.")
        start = position - 1
        while position < len(text) and not text[position].isspace() and text[position] not in '{}"' and not text.startswith("//", position):
            position += 1
        value = text[start:position]
        if any(char in value for char in "[]#"):
            raise SteamLoginError("VDF directives and conditionals are unsupported; file was not changed.")
        return ("string", value)
    def read_object(nested=False, depth=0):
        if depth > 64:
            raise SteamLoginError("VDF nesting is too deep.")
        result, keys = {}, set()
        while True:
            key = next_token()
            if key is None:
                if nested:
                    raise SteamLoginError("Missing closing VDF brace.")
                return result
            if key[0] == "}":
                if not nested:
                    raise SteamLoginError("Unexpected closing VDF brace.")
                return result
            if key[0] != "string":
                raise SteamLoginError("Expected a VDF key.")
            normalized = key[1].lower()
            if normalized in keys:
                raise SteamLoginError("Duplicate VDF key; file was not changed.")
            keys.add(normalized)
            value = next_token()
            if value is None or value[0] not in ("{", "string"):
                raise SteamLoginError("Missing VDF value.")
            result[key[1]] = read_object(True, depth + 1) if value[0] == "{" else value[1]
    return read_object()


def serialize_vdf(data, depth=0):
    if not isinstance(data, dict) or depth > 64:
        raise SteamLoginError("Invalid or excessively nested VDF object.")
    def quote(value):
        return '"' + re.sub(r'[\\"\n\r\t]', lambda match: {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}[match[0]], value) + '"'
    tab, lines = "\t" * depth, []
    for key, value in data.items():
        if not isinstance(key, str):
            raise SteamLoginError("VDF keys must be strings.")
        if isinstance(value, dict):
            lines.append(f"{tab}{quote(key)}\n{tab}{{\n{serialize_vdf(value, depth + 1)}{tab}}}\n")
        elif isinstance(value, str):
            lines.append(f"{tab}{quote(key)}\t\t{quote(value)}\n")
        else:
            raise SteamLoginError("VDF values must be strings or objects.")
    return "".join(lines)


def _key(root, key):
    return next((existing for existing in root if existing.lower() == key.lower()), key)


def _set(root, key, value):
    root[_key(root, key)] = value


def _section(root, *keys, create=False):
    for requested in keys:
        key = _key(root, requested)
        if key not in root:
            if not create:
                return {}
            root[key] = {}
        if not isinstance(root[key], dict):
            raise SteamLoginError("Unexpected VDF section type; file was not changed.")
        root = root[key]
    return root


def assert_preservation(before, after, steam_id, account_name):
    """Allow selected-login fields, account selection and disabling the chooser."""
    account_path = ("installconfigstore", "software", "valve", "steam", "accounts", account_name.lower(), "steamid")
    chooser_path = ("installconfigstore", "webstorage", "auth", "alwaysshowuserchooser")
    token_path = ("machineuserconfigstore", "software", "valve", "steam", "connectcache", cache_key(account_name))
    selected_fields = {"accountname", "personaname", "rememberpassword", "wantsofflinemode", "skipofflinemodewarning", "allowautologin", "autologin", "mostrecent", "timestamp"}
    def compare(old, new, document, parts=()):
        if old == new:
            return
        if document == 0 and parts == account_path and new == steam_id:
            return
        if document == 0 and parts == chooser_path and new == "0" and (old is None or isinstance(old, str)):
            return
        if document == 2 and parts == token_path and isinstance(new, str):
            return
        if document == 1 and len(parts) == 3 and parts[0] == "users":
            if parts[1] == steam_id and parts[2] in selected_fields and isinstance(new, str):
                return
            if parts[1] != steam_id and parts[2] in {"autologin", "mostrecent"} and new == "0":
                return
        if isinstance(new, dict) and (isinstance(old, dict) or old is None):
            old = {key.lower(): value for key, value in (old or {}).items()}
            new = {key.lower(): value for key, value in new.items()}
            for key in old.keys() | new.keys():
                compare(old.get(key), new.get(key), document, parts + (key,))
            return
        raise SteamLoginError("Account preservation check failed: unrelated settings or credentials would change. No files were written.")
    if len(before) != 3 or len(after) != 3:
        raise SteamLoginError("Expected all three Steam configuration documents.")
    for index, (old, new) in enumerate(zip(before, after)):
        compare(old, new, index)


def merge_account(documents, steam_id, account_name, encrypted, timestamp):
    """Return new documents; leave originals untouched even on failure."""
    config, login_users, local = copy.deepcopy(documents)
    accounts = _section(config, "InstallConfigStore", "Software", "Valve", "Steam", "Accounts", create=True)
    users = _section(login_users, "users", create=True)
    selected = _section(accounts, account_name)
    known_id = selected.get(_key(selected, "SteamID"))
    if known_id and known_id != steam_id:
        raise SteamLoginError("This login name belongs to a different saved SteamID. Existing accounts were not changed.")
    names = set(accounts)
    for user_id, user in users.items():
        if not isinstance(user, dict):
            raise SteamLoginError("Unexpected loginusers.vdf entry; file was not changed.")
        name = user.get(_key(user, "AccountName"))
        if not isinstance(name, str) or not name:
            continue
        names.add(name)
        same_name = name.lower() == account_name.lower()
        if (user_id == steam_id and not same_name) or (user_id != steam_id and same_name):
            raise SteamLoginError("The selected account conflicts with a saved login. Existing accounts were not changed.")
    for name in names:
        if name.lower() != account_name.lower() and cache_key(name) == cache_key(account_name):
            raise SteamLoginError("Account credential-cache collision. Existing accounts were not changed.")
    _set(_section(accounts, account_name, create=True), "SteamID", steam_id)
    # The chooser overrides automatic selection even with a saved token.
    auth = _section(config, "InstallConfigStore", "WebStorage", "Auth", create=True)
    _set(auth, "AlwaysShowUserChooser", "0")
    # Current Steam uses AutoLogin in place of MostRecent/AllowAutoLogin.
    # Keep legacy documents in their existing format; a new document uses the
    # current format. Clear only selection flags which already exist.
    modern_selection = (not users or any(_key(user, "AutoLogin") in user for user in users.values())
                        or not any(_key(user, "MostRecent") in user for user in users.values()))
    for user in users.values():
        for flag in ("AutoLogin", "MostRecent"):
            if _key(user, flag) in user:
                _set(user, flag, "0")
    user = _section(users, steam_id, create=True)
    fields = {"AccountName": account_name, "PersonaName": user.get(_key(user, "PersonaName"), account_name),
              "RememberPassword": "1", "WantsOfflineMode": "0", "SkipOfflineModeWarning": "0",
              "Timestamp": str(timestamp)}
    fields.update({"AutoLogin": "1"} if modern_selection else {"AllowAutoLogin": "1", "MostRecent": "1"})
    for flag in ("AutoLogin", "MostRecent", "AllowAutoLogin"):
        if _key(user, flag) in user:
            fields[flag] = "1"
    for key, value in fields.items():
        _set(user, key, value)
    _set(_section(local, "MachineUserConfigStore", "Software", "Valve", "Steam", "ConnectCache", create=True), cache_key(account_name), encrypted)
    result = [config, login_users, local]
    assert_preservation(documents, result, steam_id, account_name)
    return result


def merge_private_login(document, steam_id):
    """Start Friends & Chat signed out and disable this account's Remote Play."""
    account_id = str(int(steam_id) & 0xffffffff)
    result = copy.deepcopy(document)
    streaming = _section(result, "UserLocalConfigStore", "streaming_v2", create=True)
    if not isinstance(streaming.get(_key(streaming, "EnableStreaming"), "0"), str):
        raise SteamLoginError("Unexpected Remote Play preference type. No files were written.")
    _set(streaming, "EnableStreaming", "0")
    friends = _section(result, "UserLocalConfigStore", "friends", create=True)
    for name in ("AutoSignIntoFriends", "PersonaStateDesired"):
        key = _key(friends, name)
        if key in friends and not isinstance(friends[key], str):
            raise SteamLoginError("Unexpected Friends & Chat startup preference type. No files were written.")
        _set(friends, name, "0")
    storage = _section(result, "UserLocalConfigStore", "WebStorage", create=True)
    preference_key = _key(storage, "FriendStoreLocalPrefs_" + account_id)
    original = storage.get(preference_key, "{}")
    def unique_object(pairs):
        parsed = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError("Duplicate preference key")
            parsed[key] = value
        return parsed
    try:
        preferences = json.loads(original, object_pairs_hook=unique_object,
                                 parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(preferences, dict):
            raise ValueError("Expected an object")
    except (TypeError, ValueError, RecursionError):
        raise SteamLoginError("Cannot read this account's friends preferences. No Steam configuration was changed.") from None
    preferences["ePersonaState"] = 0  # Offline in Friends & Chat; Steam login remains online.
    storage[preference_key] = json.dumps(preferences, separators=(",", ":"), ensure_ascii=False)
    # Verify that all other settings survive, including other JSON fields.
    restored = copy.deepcopy(result)
    restored_streaming = _section(restored, "UserLocalConfigStore", "streaming_v2")
    old_streaming = _section(document, "UserLocalConfigStore", "streaming_v2")
    restored_streaming.pop(_key(restored_streaming, "EnableStreaming"))
    if _key(old_streaming, "EnableStreaming") in old_streaming:
        old_key = _key(old_streaming, "EnableStreaming")
        restored_streaming[old_key] = old_streaming[old_key]
    restored_friends = _section(restored, "UserLocalConfigStore", "friends")
    old_friends = _section(document, "UserLocalConfigStore", "friends")
    for name in ("AutoSignIntoFriends", "PersonaStateDesired"):
        restored_friends.pop(_key(restored_friends, name))
        if _key(old_friends, name) in old_friends:
            old_key = _key(old_friends, name)
            restored_friends[old_key] = old_friends[old_key]
    restored_storage = _section(restored, "UserLocalConfigStore", "WebStorage")
    restored_storage.pop(preference_key)
    old_storage = _section(document, "UserLocalConfigStore", "WebStorage")
    if preference_key in old_storage:
        restored_storage[preference_key] = original
    def leaves(node, path=()):
        return {entry: value for key, value in node.items()
                for entry, value in (leaves(value, path + (key,)).items() if isinstance(value, dict)
                                     else [(path + (key,), value)])}
    if leaves(restored) != leaves(document):
        raise SteamLoginError("Account preference preservation check failed. No files were written.")
    return result


def validate_cs2_launch_options(options):
    if not isinstance(options, str) or len(options) > 4096:
        raise SteamLoginError("CS2 launch options must be text of at most 4096 characters.")
    if re.search(r"[\x00-\x1f\x7f]", options):
        raise SteamLoginError("CS2 launch options must be a single line without control characters.")
    try:
        options.encode("utf-8")
    except UnicodeError:
        raise SteamLoginError("CS2 launch options contain invalid text.") from None
    return options


def merge_cs2_launch_options(document, options):
    """Set only CS2's per-account LaunchOptions, preserving the exact text."""
    options = validate_cs2_launch_options(options)
    result = copy.deepcopy(document)
    app = _section(result, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", "730", create=True)
    key = _key(app, "LaunchOptions")
    if key in app and not isinstance(app[key], str):
        raise SteamLoginError("Unexpected CS2 launch-options preference type. No files were written.")
    app[key] = options
    return result


def merge_disable_cloud_sync(document):
    """Disable account-wide Cloud sync, preserving per-game and other settings.

    Steam stores the account-wide switch in its roaming sharedconfig.vdf,
    separately from each game's apps/<appid>/cloudenabled preference.
    """
    result = copy.deepcopy(document)
    steam = _section(result, "UserRoamingConfigStore", "Software", "Valve", "Steam", create=True)
    key = _key(steam, "cloudenabled")
    if key in steam and not isinstance(steam[key], str):
        raise SteamLoginError("Unexpected Steam Cloud preference type. No files were written.")
    steam[key] = "0"
    return result


@dataclass
class ConfigFile:
    path: Path
    original: bytes | None
    data: dict


@dataclass
class RawConfigFile:
    """A game configuration copied without changing its format or encoding."""
    path: Path
    original: bytes | None
    content: bytes


def validate_cs2_settings_source(value):
    if value == "":
        return value
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{17}", value)
            or not 76561197960265728 < int(value) < 76561197960265728 + 2 ** 32):
        raise SteamLoginError("Choose a valid Steam account as the CS2 settings source.")
    return value


def _read_original(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise SteamLoginError("Expected a regular configuration file, not a directory or symbolic link.")
    if info.st_size > MAX_CONFIG:
        raise SteamLoginError("Configuration exceeds 16 MiB.")
    with path.open("rb") as handle:
        content = handle.read(MAX_CONFIG + 1)
    if len(content) > MAX_CONFIG:
        raise SteamLoginError("Configuration exceeds 16 MiB.")
    return content


def read_config(path):
    path = Path(path)
    try:
        original = _read_original(path)
        data = {} if original is None else parse_vdf(original.decode("utf-8-sig"))
        return ConfigFile(path, original, data)
    except (OSError, UnicodeError, SteamLoginError) as exc:
        raise SteamLoginError(f"Cannot read {path.name}: {exc}") from None


def _write_exclusive(path, content):
    # Never remove a pre-existing file when exclusive creation itself fails.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def _write_atomic(path, content, expected, cancel=None):
    temp = Path(str(path) + f".{uuid.uuid4()}.tmp")
    _write_exclusive(temp, content)
    try:
        if _read_original(path) != expected:
            raise SteamLoginError(f"{path.name} changed during login; file was not overwritten.")
        _check_cancel(cancel)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def config_lock(path):
    # Share the lock name with SteamNFATool so the two apps cannot overlap.
    lock = Path(str(path) + ".steam-nfa.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_exclusive(lock, json.dumps({"pid": os.getpid(), "started": time.time()}).encode())
    except FileExistsError:
        raise SteamLoginError(f"Another login may be active. After a crash, close Steam and review the configuration and backups before removing {lock}.") from None
    recovery_required = False
    try:
        yield
    except BaseException as exc:
        recovery_required = getattr(exc, "recovery_required", False)
        raise
    finally:
        if not recovery_required:
            lock.unlink(missing_ok=True)


def write_configs(configs, cancel=None, *, source_checks=()):
    """Call under config_lock; back up exact bytes and roll back failed writes."""
    _check_cancel(cancel)
    paths = [os.path.normcase(os.path.abspath(config.path)) for config in configs]
    if len(set(paths)) != len(paths):
        raise SteamLoginError("Duplicate configuration paths.")
    prepared = []
    for config in configs:
        if isinstance(config, RawConfigFile):
            content = config.content
        else:
            text = serialize_vdf(config.data)
            parse_vdf(text)
            content = text.encode("utf-8")
        if len(content) > MAX_CONFIG:
            raise SteamLoginError("Updated configuration exceeds 16 MiB.")
        prepared.append((config, content))
    backups, written = {}, []
    def check_sources():
        for path, expected in source_checks:
            if _read_original(path) != expected:
                raise SteamLoginError("Source CS2 settings changed during login. Close CS2 and retry.")
    try:
        check_sources()
        for config, content in prepared:
            _check_cancel(cancel)
            if _read_original(config.path) != config.original:
                raise SteamLoginError(f"{config.path.name} changed during login; retry with Steam closed.")
            config.path.parent.mkdir(parents=True, exist_ok=True)
            if config.original is not None:
                backup = Path(str(config.path) + f".{uuid.uuid4()}.bak")
                _write_exclusive(backup, config.original)
                backups[config.path] = backup
        for config, content in prepared:
            check_sources()
            _write_atomic(config.path, content, config.original, cancel)
            written.append((config, content))
        _check_cancel(cancel)
        check_sources()
        return list(backups.values())
    except Exception as exc:
        failed = []
        for config, content in reversed(written):
            try:
                if _read_original(config.path) != content:
                    raise SteamLoginError("File changed after replacement.")
                if config.original is None:
                    config.path.unlink()
                else:
                    _write_atomic(config.path, config.original, content)
            except Exception:
                failed.append(f"{config.path} (backup: {backups.get(config.path, 'none; new file')})")
        recovery = " Could not safely restore: " + "; ".join(failed) if failed else ""
        error_type = LoginCancelled if isinstance(exc, LoginCancelled) else SteamLoginError
        raise error_type(f"Configuration update failed: {exc}{recovery}", recovery_required=bool(failed)) from None


def encrypt_with_dpapi(token, account_name):
    """Encrypt under the current Windows user with username entropy, as in Node."""
    if not IS_WINDOWS:
        raise SteamLoginError("Steam login requires Windows.")
    account_name = validate_account_name(account_name).lower()
    if not isinstance(token, str) or not 0 < len(token) <= MAX_INPUT:
        raise SteamLoginError("Token is missing or too long.")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    def blob(value):
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        return Blob(len(value), buffer), buffer
    plain, plain_buffer = blob(token.encode("utf-8"))
    entropy, entropy_buffer = blob(account_name.encode("utf-8"))
    encrypted = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    protect = crypt32.CryptProtectData
    protect.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    protect.restype = wintypes.BOOL
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        if not protect(ctypes.byref(plain), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(encrypted)):
            raise SteamLoginError("Windows token encryption failed; no Steam configuration was changed.")
        return ctypes.string_at(encrypted.data, encrypted.size).hex()
    finally:
        if encrypted.data:
            kernel32.LocalFree(ctypes.cast(encrypted.data, ctypes.c_void_p))
        ctypes.memset(plain_buffer, 0, len(plain_buffer))
        ctypes.memset(entropy_buffer, 0, len(entropy_buffer))


def _windows_command(name):
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name)


def _run(args):
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          errors="replace", check=True, timeout=15,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _steam_running():
    result = _run([_windows_command("tasklist.exe"), "/FI", "IMAGENAME eq steam*", "/FO", "CSV", "/NH"])
    return bool(re.search(r'^"(?:steam|steamwebhelper)\.exe"', result.stdout, re.I | re.M))


def close_steam(steam_dir, cancel=None):
    _check_cancel(cancel)
    try:
        if not _steam_running():
            return
        try:
            _run([str(Path(steam_dir) / "steam.exe"), "-shutdown"])
        except (OSError, subprocess.SubprocessError):
            _check_cancel(cancel)
            if _steam_running():
                raise SteamLoginError("Could not close Steam. Exit Steam and any running games, then retry.") from None
            return
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            _check_cancel(cancel)
            if not _steam_running():
                return
            if cancel is not None:
                cancel.wait(0.5)
            else:
                time.sleep(0.5)
    except (OSError, subprocess.SubprocessError):
        raise SteamLoginError("Could not check whether Steam is closed. No configuration was changed.") from None
    raise SteamLoginError("Steam is still running. Exit Steam and any running games, then retry. No configuration was changed.")


def _find_installation():
    candidates = []
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "SteamPath")
            candidates.append(value)
    except OSError:
        pass
    candidates.append(find_steam_dir())
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates.append(str(Path(base) / "Steam"))
    for candidate in candidates:
        if isinstance(candidate, str) and (Path(candidate) / "steam.exe").is_file():
            return Path(candidate)
    raise SteamLoginError("Steam installation not found. Install Steam and run it once.")


def _set_autologin(account_name):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
        winreg.SetValueEx(key, "AutoLoginUser", 0, winreg.REG_SZ, account_name)


def _launch_steam(steam_dir):
    subprocess.Popen([str(Path(steam_dir) / "steam.exe"), "-cef-enable-debugging"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def parse_logon_result(text, steam_id):
    """Read only Steam's response marker; never expose log arguments or tokens."""
    pattern = r"LogOnResponse\(\)\s*:\s*\[(?:U:1:(\d+)|I:0:0)\]\s*'([^'\r\n]+)'"
    for match in re.finditer(pattern, text):
        account_id, response = match.groups()
        if response == "OK":
            return ("confirmed" if account_id == str(int(steam_id) & 0xffffffff) else "other_account", None)
        reasons = {"Access Denied": "Access denied", "Invalid Password": "Invalid credentials",
                   "Expired": "Session expired", "No Connection": "No connection",
                   "Service Unavailable": "Service unavailable", "Rate Limit Exceeded": "Too many attempts",
                   "Account Logon Denied": "Additional account approval required"}
        return "rejected", reasons.get(response, "Login rejected")
    return None


def log_checkpoint(path):
    try:
        info = Path(path).stat()
        return info.st_ino, info.st_size
    except OSError:
        return None, 0


def wait_for_sign_in(path, steam_id, checkpoint, cancel=None, timeout=30):
    """Follow new connection-log bytes only, with bounded reads and cancellation."""
    identity, offset = checkpoint
    pending = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise LoginCancelled("Cancelled while checking sign-in. Steam is already running; the saved configuration was kept.")
        try:
            with Path(path).open("rb") as handle:
                info = os.fstat(handle.fileno())
                if identity != info.st_ino or info.st_size < offset:
                    offset, pending = 0, ""
                identity = info.st_ino
                handle.seek(offset)
                chunk = handle.read(64 * 1024)
                offset = handle.tell()
            pending += chunk.decode("utf-8", errors="replace")
            end = pending.rfind("\n")
            if end >= 0:
                outcome = parse_logon_result(pending[:end + 1], steam_id)
                pending = pending[end + 1:][-4096:]
                if outcome:
                    return outcome
            else:
                pending = pending[-4096:]
        except OSError:
            pass
        if cancel is not None:
            cancel.wait(0.2)
        else:
            time.sleep(0.2)
    return "unconfirmed", None


def login_account(account_name, token, *, cancel=None, progress=None, private_login=False,
                  disable_cloud_sync=False, cs2_launch_options=None,
                  cs2_settings_source=""):
    """Log in the selected account; never use a display alias as the login name."""
    if not IS_WINDOWS:
        raise SteamLoginError("Steam login is available on Windows only.")
    progress = progress or (lambda message: None)
    _check_cancel(cancel)
    account_name = validate_account_name(account_name).lower()
    payload = validate_token(token)
    if cs2_launch_options is not None:
        cs2_launch_options = validate_cs2_launch_options(cs2_launch_options)
    cs2_settings_source = validate_cs2_settings_source(cs2_settings_source)
    from cs2_settings import ensure_cs2_closed, source_for_login, prepare_settings
    installation = _find_installation()
    local_base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    paths = [installation / "config" / "config.vdf", installation / "config" / "loginusers.vdf", local_base / "Steam" / "local.vdf"]
    account_dir = installation / "userdata" / str(int(payload["sub"]) & 0xffffffff)
    with config_lock(paths[0]):
        progress("Preparing Steam login…")
        encrypted = encrypt_with_dpapi(token, account_name)
        source_id = source_for_login(installation, cs2_settings_source, read_config(paths[1]).data)
        if source_id is not None:
            ensure_cs2_closed()
            prepare_settings(installation, source_id, payload["sub"])
            disable_cloud_sync = True
        preference_merges = []
        if private_login or cs2_launch_options is not None:
            def merge_local_preferences(data):
                if private_login:
                    data = merge_private_login(data, payload["sub"])
                if cs2_launch_options is not None:
                    data = merge_cs2_launch_options(data, cs2_launch_options)
                return data
            preference_merges.append((account_dir / "config" / "localconfig.vdf", merge_local_preferences))
        if disable_cloud_sync:
            preference_merges.append((account_dir / "7" / "remote" / "sharedconfig.vdf", merge_disable_cloud_sync))
            # Older clients also keep a local roaming-config copy. Update it
            # when present so it cannot restore an enabled setting at startup.
            legacy = account_dir / "config" / "sharedconfig.vdf"
            if legacy.exists():
                preference_merges.append((legacy, merge_disable_cloud_sync))
        paths.extend(path for path, merge in preference_merges)
        for path in paths:
            read_config(path)  # Reject invalid files before closing the client.
        for path, merge in preference_merges:
            merge(read_config(path).data)
        _check_cancel(cancel)
        progress("Closing Steam…")
        close_steam(installation, cancel)
        _check_cancel(cancel)
        game_configs, source_checks = [], []
        if source_id is not None:
            ensure_cs2_closed()
            # Steam can flush files during shutdown; use the latest bytes.
            game_configs, source_checks = prepare_settings(installation, source_id, payload["sub"])
        configs = [read_config(path) for path in paths]
        validate_token(token)  # Recheck after waiting for shutdown.
        originals = [config.data for config in configs[:3]]
        updated = merge_account(originals, payload["sub"], account_name, encrypted, int(time.time()))
        for config, (_, merge) in zip(configs[3:], preference_merges):
            updated.append(merge(config.data))
        if private_login:
            progress("Starting Friends & Chat offline and disabling Remote Play…")
        if disable_cloud_sync:
            progress("Disabling Steam Cloud Sync for the selected account…")
        if cs2_launch_options is not None:
            progress("Setting custom CS2 launch options…")
        for config, data in zip(configs, updated):
            config.data = data
        if source_id is not None:
            progress("Keeping CS2 video settings and keyboard/mouse controls…")
            configs.extend(game_configs)
        progress("Saving account; keeping other remembered accounts…")
        backups = (write_configs(configs, cancel, source_checks=source_checks) if source_checks
                   else write_configs(configs, cancel))
        def check_after_save():
            if cancel is not None and cancel.is_set():
                raise LoginCancelled("Cancelled after configuration was saved. Steam was not launched.")
        check_after_save()
        warning = None
        try:
            _set_autologin(account_name)
        except OSError:
            warning = "Select the account in Steam; the automatic selection could not be updated."
        check_after_save()
        progress("Launching Steam…")
        log_path = installation / "logs" / "connection_log.txt"
        checkpoint = log_checkpoint(log_path)
        try:
            _launch_steam(installation)
        except OSError:
            raise SteamLoginError("Configuration was saved, but Steam could not be launched. Open Steam manually.") from None
        progress("Waiting for Steam to confirm sign-in…")
        sign_in, reason = wait_for_sign_in(log_path, payload["sub"], checkpoint, cancel)
        users = _section(originals[1], "users")
        return {"steam_id": payload["sub"], "backups": [str(path) for path in backups],
                "preserved_accounts": sum(user_id != payload["sub"] for user_id in users), "warning": warning,
                "sign_in": sign_in, "reason": reason, "private_login": bool(private_login),
                "disable_cloud_sync": bool(disable_cloud_sync),
                "cs2_launch_options_applied": cs2_launch_options is not None,
                "cs2_settings_applied": source_id is not None,
                "cs2_settings_copied": len(game_configs),
                "cs2_settings_state": ("unavailable" if source_id is None else "same_account" if source_id == payload["sub"]
                                       else "copied" if game_configs else "unchanged")}
