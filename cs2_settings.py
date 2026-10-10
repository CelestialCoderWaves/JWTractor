"""Copy local CS2 preferences between Steam accounts, never whole userdata trees."""
from pathlib import Path
import re
import stat
import subprocess

from cooldown import current_steam_session
from steam_login import (RawConfigFile, SteamLoginError, _key, _read_original,
                         _run, _section, _windows_command, validate_cs2_settings_source,
                         _find_installation, read_config, validate_account_name)


REQUIRED_FILES = ("cs2_video.txt", "cs2_user_keys_0_slot0.vcfg", "cs2_user_convars_0_slot0.vcfg")
OPTIONAL_FILES = ("cs2_machine_convars.vcfg",) + tuple(
    f"cs2_user_{kind}_0_slot{slot}.vcfg" for slot in range(1, 4) for kind in ("keys", "convars"))
MAX_SETTINGS_FILE = 1024 * 1024


class NoPreviousAccountError(SteamLoginError):
    pass


def source_for_login(installation, selected, loginusers):
    """A fresh installation has nothing to transfer; an explicit source must work."""
    try:
        source = resolve_source(selected, loginusers)
    except NoPreviousAccountError:
        return None
    if not selected:
        directory = settings_directory(installation, source)
        if not any((directory / name).exists() for name in REQUIRED_FILES):
            return None
    return source


def list_settings_sources(installation=None):
    """Offer remembered Steam accounts even when no token is saved in JWTractor."""
    try:
        installation = Path(installation) if installation is not None else _find_installation()
        users = _section(read_config(installation / "config" / "loginusers.vdf").data, "users")
        sources = []
        for steam_id, user in users.items():
            if not isinstance(user, dict):
                continue
            try:
                directory = settings_directory(installation, steam_id)
                name = validate_account_name(user.get(_key(user, "AccountName")))
                if all((directory / filename).is_file() and not (directory / filename).is_symlink()
                       for filename in REQUIRED_FILES):
                    sources.append({"steam_id": steam_id, "name": name})
            except (SteamLoginError, OSError):
                continue
        return sorted(sources, key=lambda source: source["name"].casefold())
    except (SteamLoginError, OSError):
        return []


def ensure_cs2_closed():
    try:
        result = _run([_windows_command("tasklist.exe"), "/FI", "IMAGENAME eq cs2.exe", "/FO", "CSV", "/NH"])
    except (OSError, subprocess.SubprocessError):
        raise SteamLoginError("Could not check whether CS2 is running. Close CS2 and retry.") from None
    if re.search(r'^"cs2\.exe"', result.stdout, re.I | re.M):
        raise SteamLoginError("Close CS2 before switching accounts with shared video and controls.")


def resolve_source(selected, loginusers):
    """Capture the source before Steam closes or the target becomes most recent."""
    if selected:
        return validate_cs2_settings_source(selected)
    session = current_steam_session()
    if session:
        return validate_cs2_settings_source(session[0])
    candidates = []
    for steam_id, user in _section(loginusers, "users").items():
        try:
            validate_cs2_settings_source(steam_id)
        except SteamLoginError:
            continue
        if not steam_id or not isinstance(user, dict):
            continue
        def value(name):
            return user.get(_key(user, name), "")
        stamp = value("Timestamp")
        stamp = int(stamp) if isinstance(stamp, str) and re.fullmatch(r"[0-9]{1,20}", stamp) else 0
        candidates.append(((value("AutoLogin") == "1", value("MostRecent") == "1", stamp), steam_id))
    if candidates:
        candidates.sort(reverse=True)
        if len(candidates) == 1 or candidates[0][0] > candidates[1][0]:
            return candidates[0][1]
    raise NoPreviousAccountError("No previous Steam account could be identified. Choose a CS2 settings source in Settings.")


def settings_directory(installation, steam_id):
    steam_id = validate_cs2_settings_source(steam_id)
    if not steam_id:
        raise SteamLoginError("Choose a CS2 settings source in Settings.")
    root = Path(installation).resolve()
    path = root / "userdata" / str(int(steam_id) & 0xffffffff) / "730" / "local" / "cfg"
    # Avoid following redirects into another user's files. Compatible with Python 3.9.
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if (current.is_symlink() or getattr(info, "st_file_attributes", 0) &
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise SteamLoginError("CS2 settings folders must not be symbolic links or junctions.")
        if not stat.S_ISDIR(info.st_mode):
            raise SteamLoginError("Expected a CS2 settings folder, but found a file.")
    return path


def prepare_settings(installation, source_id, target_id):
    source = settings_directory(installation, source_id)
    target = settings_directory(installation, target_id)
    if source == target:
        return [], []
    configs, checks = [], []
    for name in REQUIRED_FILES + OPTIONAL_FILES:
        try:
            content = _read_original(source / name)
            if content is None:
                if name in REQUIRED_FILES:
                    raise SteamLoginError(f"Source account is missing {name}. Run CS2 on that account first, or choose another source.")
                continue
            if not content.strip() or len(content) > MAX_SETTINGS_FILE or b"\x00" in content:
                raise SteamLoginError(f"Source {name} is empty, invalid or exceeds 1 MiB. Choose another source.")
            content.decode("utf-8-sig")
            original = _read_original(target / name)
        except (OSError, UnicodeError):
            raise SteamLoginError(f"Could not read CS2 settings file {name}. Check access or choose another source.") from None
        checks.append((source / name, content))
        if original != content:
            configs.append(RawConfigFile(target / name, original, content))
    return configs, checks
