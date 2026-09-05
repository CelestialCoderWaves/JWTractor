"""Backup and restore Steam's login-related config files.

When you use extracted tokens with a Steam loader, the loader modifies
several files that together make up Steam's login state. Restoring only
some of them leaves Steam in an inconsistent state — accounts may show up
but require re-entering your password.

This module backs up and restores the **full set** of files:

- ``<Steam>/config/config.vdf`` — accounts list, machine auth tokens
- ``<Steam>/config/loginusers.vdf`` — remembered accounts, auto-login user
- ``<Steam>/ssfn*`` — this machine's Steam Guard authorization files
- Windows registry login preferences (auto-login user / remember password)

Each backup is a timestamped directory under
``%APPDATA%\\JWTractor\\backups\\`` containing copies of every file that
existed at snapshot time, plus a ``meta.json`` recording when and why.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time

__all__ = [
    "find_steam_dir",
    "steam_config_dir",
    "backup_dir",
    "create_backup",
    "list_backups",
    "restore_backup",
    "delete_backup",
    "has_connect_cache",
    "CONFIG_DIR_FILES",
]

CONFIG_DIR_FILES = ("config.vdf", "loginusers.vdf")
REGISTRY_VALUE_NAMES = ("AutoLoginUser", "RememberPassword")

_CONNECT_CACHE_EMPTY = re.compile(
    r'"ConnectCache"\s*\{\s*\}', re.DOTALL
)


def has_connect_cache(config_vdf: str | None = None) -> bool:
    """Check if Steam has a non-empty ConnectCache.

    With no explicit path, both ``config.vdf`` and Steam's Windows
    ``local.vdf`` are checked because current installations may keep the
    cache in either location.  When *config_vdf* is supplied, only that file
    is checked.

    Returns ``False`` if no checked file contains a non-empty ConnectCache.
    A ``True`` means there are refresh tokens that enable passwordless login.
    """
    if config_vdf is None:
        paths = (config_vdf_path(), _local_vdf_path())
    else:
        paths = (config_vdf,)

    for path in paths:
        if path is None or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        if "ConnectCache" in content and _CONNECT_CACHE_EMPTY.search(content) is None:
            return True
    return False


def _local_vdf_path() -> str | None:
    """Return ``%LOCALAPPDATA%\\Steam\\local.vdf`` if it exists."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            p = os.path.join(local, "Steam", "local.vdf")
            if os.path.isfile(p):
                return p
    return None


def find_steam_dir() -> str | None:
    """Locate the Steam installation directory, or ``None`` if not found.

    Checks the Windows registry first, then falls back to common default
    paths. On non-Windows platforms only the default paths are tried.
    """
    if os.name == "nt":
        try:
            import winreg

            for hive, subkey in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        val, _ = winreg.QueryValueEx(key, "InstallPath")
                        if val and os.path.isdir(str(val)):
                            return str(val)
                except OSError:
                    continue
        except ImportError:
            pass

    candidates = []
    if os.name == "nt":
        for drive in ("C", "D", "E"):
            candidates.append(rf"{drive}:\Program Files (x86)\Steam")
            candidates.append(rf"{drive}:\Program Files\Steam")
    else:
        candidates.append(os.path.expanduser("~/.steam/steam"))
        candidates.append(os.path.expanduser("~/.local/share/Steam"))

    for path in candidates:
        if os.path.isdir(path):
            return path

    return None


def steam_config_dir(steam_dir: str | None = None) -> str | None:
    """Return Steam's ``config/`` directory, or ``None`` if not found."""
    if steam_dir is None:
        steam_dir = find_steam_dir()
    if steam_dir is None:
        return None
    cfg = os.path.join(steam_dir, "config")
    return cfg if os.path.isdir(cfg) else None


def config_vdf_path(steam_dir: str | None = None) -> str | None:
    """Return the path to ``config.vdf``, or ``None`` if it doesn't exist."""
    cfg = steam_config_dir(steam_dir)
    if cfg is None:
        return None
    vdf = os.path.join(cfg, "config.vdf")
    return vdf if os.path.isfile(vdf) else None


def backup_dir() -> str:
    """Directory where Steam config backups are stored."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    return os.path.join(base, "JWTractor", "backups")


def _collect_login_files(config_dir: str) -> list[dict]:
    """Build a list of ``{key, src}`` entries for every login file found.

    *key* is the name used inside the backup directory (e.g. ``config.vdf``,
    ``local.vdf``).  *src* is the full source path.
    """
    entries = []
    for fname in CONFIG_DIR_FILES:
        src = os.path.join(config_dir, fname)
        if os.path.isfile(src):
            entries.append({"key": fname, "src": src})
    steam_dir = os.path.dirname(config_dir)
    try:
        names = os.listdir(steam_dir)
    except OSError:
        names = []
    for name in names:
        if name.lower().startswith("ssfn"):
            src = os.path.join(steam_dir, name)
            if os.path.isfile(src) and not os.path.islink(src):
                entries.append({"key": name, "src": src})
    # Retain support for installations which do have this legacy/local file.
    local = _local_vdf_path()
    if local:
        entries.append({"key": "local.vdf", "src": local})
    return entries


def _read_registry_login_state() -> dict:
    """Read Steam's per-user login preferences, including registry types."""
    if os.name != "nt":
        return {}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            result = {}
            for name in REGISTRY_VALUE_NAMES:
                try:
                    value, value_type = winreg.QueryValueEx(key, name)
                    result[name] = {"value": value, "type": value_type}
                except OSError:
                    pass
            return result
    except OSError:
        return {}


def _restore_registry_login_state(state: dict) -> list[str]:
    """Restore login preferences and return display names of changed values."""
    if os.name != "nt" or not state:
        return []
    import winreg
    restored = []
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
        for name in REGISTRY_VALUE_NAMES:
            item = state.get(name)
            if isinstance(item, dict) and "value" in item:
                value_type = int(item.get("type", winreg.REG_SZ))
                winreg.SetValueEx(key, name, 0, value_type, item["value"])
                restored.append(f"registry:{name}")
    return restored


def create_backup(
    steam_dir: str | None = None, label: str = "",
) -> dict:
    """Snapshot all login-related files and return the backup record.

    Returns a dict with keys ``id`` (directory name), ``backup_path``,
    ``config_dir`` (source), ``created`` (epoch), ``files`` (list of
    ``{key, src}`` entries), and ``label``.

    Raises ``FileNotFoundError`` if Steam's config directory can't be found
    or contains none of the expected files.
    """
    config_dir = steam_config_dir(steam_dir)
    if config_dir is None:
        raise FileNotFoundError(
            "Could not find Steam's config directory. "
            "Make sure Steam is installed."
        )

    entries = _collect_login_files(config_dir)
    if not entries:
        raise FileNotFoundError(
            f"No login files found in {config_dir}."
        )

    bdir = backup_dir()
    ts = int(time.time())
    snapshot_name = f"steam_{ts}"
    snapshot_dir = os.path.join(bdir, snapshot_name)

    counter = 0
    while os.path.exists(snapshot_dir):
        counter += 1
        snapshot_name = f"steam_{ts}_{counter}"
        snapshot_dir = os.path.join(bdir, snapshot_name)

    os.makedirs(snapshot_dir, exist_ok=True)

    total_size = 0
    file_manifest = []
    for entry in entries:
        shutil.copy2(entry["src"], os.path.join(snapshot_dir, entry["key"]))
        size = os.path.getsize(entry["src"])
        total_size += size
        file_manifest.append({"key": entry["key"], "src": entry["src"]})

    record = {
        "id": snapshot_name,
        "config_dir": config_dir,
        "backup_path": snapshot_dir,
        "created": ts,
        "files": file_manifest,
        "total_size": total_size,
        "label": label,
        "registry": _read_registry_login_state(),
    }
    meta_path = os.path.join(snapshot_dir, "meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)

    return record


def _file_keys(record: dict) -> list[str]:
    """Return the backup key names from a record's ``files`` field.

    Handles both the new manifest format (list of dicts with ``key``/``src``)
    and the legacy format (list of plain filename strings).
    """
    files = record.get("files") or []
    if files and isinstance(files[0], dict):
        return [f["key"] for f in files]
    return list(files)


def list_backups() -> list[dict]:
    """Return all backup records, newest first."""
    bdir = backup_dir()
    if not os.path.isdir(bdir):
        return []

    backups = []
    for name in os.listdir(bdir):
        snap = os.path.join(bdir, name)
        if not os.path.isdir(snap) or not name.startswith("steam_"):
            continue
        meta_path = os.path.join(snap, "meta.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    record = json.load(f)
                record["id"] = name
                record["backup_path"] = snap
                backups.append(record)
                continue
            except (OSError, ValueError):
                pass
        # Fallback: meta.json missing or unreadable — infer from directory
        keys = [f for f in os.listdir(snap)
                if f != "meta.json" and os.path.isfile(os.path.join(snap, f))]
        backups.append({
            "id": name,
            "config_dir": "",
            "backup_path": snap,
            "created": int(os.path.getmtime(snap)),
            "files": [{"key": k, "src": ""} for k in keys],
            "total_size": sum(
                os.path.getsize(os.path.join(snap, f)) for f in keys
            ),
            "label": "",
        })

    backups.sort(key=lambda b: (b.get("created", 0), b.get("id", "")), reverse=True)
    return backups


def restore_backup(backup_id: str | None = None) -> dict:
    """Copy a backup's files back to their original locations.

    Each file is restored to the source path recorded at backup time.
    ``config.vdf`` and ``loginusers.vdf`` go back to Steam's ``config/``
    directory; ``local.vdf`` goes back to ``%LOCALAPPDATA%\\Steam\\``.

    If *backup_id* is ``None``, the most recent backup is used.

    Returns the backup record with an added ``restored_files`` list.
    """
    backups = list_backups()
    if not backups:
        raise FileNotFoundError("No Steam config backups found.")

    if backup_id is None:
        record = backups[0]
    else:
        record = next((b for b in backups if b["id"] == backup_id), None)
        if record is None:
            raise FileNotFoundError(f"Backup '{backup_id}' not found.")

    snap = record["backup_path"]
    if not os.path.isdir(snap):
        raise FileNotFoundError(f"Backup directory missing: {snap}")

    # Build a map of key -> destination path.
    files = record.get("files") or []
    restore_map: dict[str, str] = {}

    for entry in files:
        if isinstance(entry, dict):
            key = entry["key"]
            src = entry.get("src", "")
        else:
            key = entry
            src = ""

        if src and os.path.isdir(os.path.dirname(src)):
            restore_map[key] = src
        else:
            # Infer destination from the key name.
            if key in CONFIG_DIR_FILES:
                cfg = record.get("config_dir") or steam_config_dir()
                if cfg is None:
                    raise ValueError(
                        "Cannot determine where to restore — Steam's "
                        "config directory was not found."
                    )
                restore_map[key] = os.path.join(cfg, key)
            elif key.lower().startswith("ssfn"):
                cfg = record.get("config_dir") or steam_config_dir()
                if cfg is None:
                    raise ValueError(
                        "Cannot determine where to restore Steam Guard files."
                    )
                restore_map[key] = os.path.join(os.path.dirname(cfg), key)
            elif key == "local.vdf":
                local = _local_vdf_path()
                if local:
                    restore_map[key] = local
                elif os.name == "nt":
                    local_dir = os.environ.get("LOCALAPPDATA", "")
                    if local_dir:
                        dest = os.path.join(local_dir, "Steam", "local.vdf")
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        restore_map[key] = dest

    restored = []
    for key, dest in restore_map.items():
        backup_file = os.path.join(snap, key)
        if os.path.isfile(backup_file):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(backup_file, dest)
            restored.append(key)

    restored.extend(_restore_registry_login_state(record.get("registry") or {}))
    record["restored_files"] = restored
    return record


def delete_backup(backup_id: str) -> bool:
    """Delete a backup directory. Returns ``True`` if it existed."""
    bdir = backup_dir()
    snap = os.path.join(bdir, backup_id)
    if os.path.isdir(snap):
        shutil.rmtree(snap)
        return True
    return False
