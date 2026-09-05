"""Tests for Steam config backup and restore (``steam.py``).

Every test uses temp directories — nothing touches the real Steam install or
the real backup directory.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import steam as steam_module  # noqa: E402

from steam import (  # noqa: E402
    CONFIG_DIR_FILES,
    backup_dir,
    create_backup,
    delete_backup,
    list_backups,
    restore_backup,
    has_connect_cache,
)

SAMPLE_CONFIG_VDF = """\
"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"Accounts"
\t\t\t\t{
\t\t\t\t\t"MyRealAccount"
\t\t\t\t\t{
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""

SAMPLE_LOGINUSERS_VDF = """\
"users"
{
\t"76561198000000000"
\t{
\t\t"AccountName"\t\t"MyRealAccount"
\t\t"PersonaName"\t\t"MyDisplayName"
\t\t"RememberPassword"\t\t"1"
\t\t"MostRecent"\t\t"1"
\t}
}
"""

SAMPLE_LOCAL_VDF = """\
"MachineUserConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"ConnectCache"
\t\t\t\t{
\t\t\t\t\t"MyRealAccount"
\t\t\t\t\t{
\t\t\t\t\t\t"Token"\t\t"eyJfake_refresh_token"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


def _file_keys(record):
    """Extract key names from a backup record's files list."""
    files = record.get("files") or []
    if files and isinstance(files[0], dict):
        return [f["key"] for f in files]
    return list(files)


@pytest.fixture(autouse=True)
def _isolate_dirs(tmp_path, monkeypatch):
    """Point backup_dir() and _local_vdf_path() at temp directories."""
    fake_appdata = str(tmp_path / "appdata")
    fake_local = str(tmp_path / "localappdata")
    monkeypatch.setenv("APPDATA", fake_appdata)
    monkeypatch.setenv("LOCALAPPDATA", fake_local)
    if os.name != "nt":
        monkeypatch.setenv("XDG_CONFIG_HOME", fake_appdata)
    # Unit tests must never read or write the user's real Steam registry state.
    monkeypatch.setattr(steam_module, "_read_registry_login_state", lambda: {})
    monkeypatch.setattr(steam_module, "_restore_registry_login_state", lambda state: [])


def _write_steam_config(tmp_path, *, with_local=False):
    """Create a fake Steam config directory with login files."""
    steam = tmp_path / "Steam"
    cfg = steam / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.vdf").write_text(SAMPLE_CONFIG_VDF, encoding="utf-8")
    (cfg / "loginusers.vdf").write_text(SAMPLE_LOGINUSERS_VDF, encoding="utf-8")
    if with_local:
        local_dir = tmp_path / "localappdata" / "Steam"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "local.vdf").write_text(SAMPLE_LOCAL_VDF, encoding="utf-8")
        os.environ["LOCALAPPDATA"] = str(tmp_path / "localappdata")
    return str(steam)


def test_connect_cache_is_read_from_config_vdf(tmp_path):
    steam_dir = _write_steam_config(tmp_path)
    config = os.path.join(steam_dir, "config", "config.vdf")
    with open(config, "a", encoding="utf-8") as f:
        f.write('\n"ConnectCache" { "account" { "Token" "secret" } }\n')
    assert has_connect_cache(config)


def test_connect_cache_default_lookup_includes_local_vdf(tmp_path, monkeypatch):
    steam_dir = _write_steam_config(tmp_path, with_local=True)
    config = os.path.join(steam_dir, "config", "config.vdf")
    local = os.path.join(os.environ["LOCALAPPDATA"], "Steam", "local.vdf")

    monkeypatch.setattr(steam_module, "config_vdf_path", lambda: config)
    monkeypatch.setattr(steam_module, "_local_vdf_path", lambda: local)

    assert not has_connect_cache(config)
    assert has_connect_cache()


def test_backup_and_restore_steam_guard_files(tmp_path):
    steam_dir = _write_steam_config(tmp_path)
    ssfn = os.path.join(steam_dir, "ssfn123456789")
    with open(ssfn, "wb") as f:
        f.write(b"machine authorization")

    record = create_backup(steam_dir)
    assert "ssfn123456789" in _file_keys(record)
    os.remove(ssfn)

    restored = restore_backup(record["id"])
    assert "ssfn123456789" in restored["restored_files"]
    assert open(ssfn, "rb").read() == b"machine authorization"


def test_create_backup(tmp_path):
    steam_dir = _write_steam_config(tmp_path)
    record = create_backup(steam_dir)

    assert record["id"].startswith("steam_")
    assert os.path.isdir(record["backup_path"])
    keys = _file_keys(record)
    assert "config.vdf" in keys
    assert "loginusers.vdf" in keys
    assert record["total_size"] > 0

    backed_config = open(
        os.path.join(record["backup_path"], "config.vdf"), encoding="utf-8"
    ).read()
    assert backed_config == SAMPLE_CONFIG_VDF


def test_create_backup_includes_local_vdf(tmp_path):
    steam_dir = _write_steam_config(tmp_path, with_local=True)
    record = create_backup(steam_dir)

    keys = _file_keys(record)
    assert "local.vdf" in keys
    backed = open(
        os.path.join(record["backup_path"], "local.vdf"), encoding="utf-8"
    ).read()
    assert "ConnectCache" in backed


def test_create_backup_with_label(tmp_path):
    steam_dir = _write_steam_config(tmp_path)
    record = create_backup(steam_dir, label="before loader")
    assert record["label"] == "before loader"


def test_create_backup_no_steam_raises():
    with pytest.raises(FileNotFoundError):
        create_backup("/nonexistent/steam")


def test_list_backups_empty():
    assert list_backups() == []


def test_list_backups_ordered(tmp_path):
    steam_dir = _write_steam_config(tmp_path)
    first = create_backup(steam_dir, label="first")
    time.sleep(0.05)
    second = create_backup(steam_dir, label="second")

    backups = list_backups()
    assert len(backups) >= 2
    assert backups[0]["id"] == second["id"]
    assert backups[1]["id"] == first["id"]


def test_restore_backup(tmp_path):
    steam_dir = _write_steam_config(tmp_path)
    cfg = os.path.join(steam_dir, "config")

    original_config = open(os.path.join(cfg, "config.vdf"), encoding="utf-8").read()
    original_login = open(os.path.join(cfg, "loginusers.vdf"), encoding="utf-8").read()

    record = create_backup(steam_dir)

    # Simulate a loader trashing both files
    with open(os.path.join(cfg, "config.vdf"), "w", encoding="utf-8") as f:
        f.write('"modified by loader"')
    with open(os.path.join(cfg, "loginusers.vdf"), "w", encoding="utf-8") as f:
        f.write('"users"\n{\n}\n')

    restored = restore_backup(record["id"])
    assert "config.vdf" in restored["restored_files"]
    assert "loginusers.vdf" in restored["restored_files"]
    assert open(os.path.join(cfg, "config.vdf"), encoding="utf-8").read() == original_config
    assert open(os.path.join(cfg, "loginusers.vdf"), encoding="utf-8").read() == original_login


def test_restore_includes_local_vdf(tmp_path):
    steam_dir = _write_steam_config(tmp_path, with_local=True)
    local_path = os.path.join(
        os.environ["LOCALAPPDATA"], "Steam", "local.vdf"
    )

    original_local = open(local_path, encoding="utf-8").read()
    record = create_backup(steam_dir)

    # Loader empties ConnectCache
    with open(local_path, "w", encoding="utf-8") as f:
        f.write('"MachineUserConfigStore"\n{\n}\n')

    assert open(local_path, encoding="utf-8").read() != original_local

    restored = restore_backup(record["id"])
    assert "local.vdf" in restored["restored_files"]
    assert open(local_path, encoding="utf-8").read() == original_local


def test_restore_latest(tmp_path):
    steam_dir = _write_steam_config(tmp_path)
    cfg = os.path.join(steam_dir, "config")

    create_backup(steam_dir, label="first")
    time.sleep(0.05)

    with open(os.path.join(cfg, "loginusers.vdf"), "w", encoding="utf-8") as f:
        f.write('"second version"')
    second = create_backup(steam_dir, label="second")

    with open(os.path.join(cfg, "loginusers.vdf"), "w", encoding="utf-8") as f:
        f.write('"trashed"')

    restored = restore_backup()
    assert restored["id"] == second["id"]
    assert open(os.path.join(cfg, "loginusers.vdf"), encoding="utf-8").read() == '"second version"'


def test_restore_nonexistent_raises():
    with pytest.raises(FileNotFoundError, match="No Steam config backups"):
        restore_backup()


def test_restore_unknown_id_raises(tmp_path):
    steam_dir = _write_steam_config(tmp_path)
    create_backup(steam_dir)
    with pytest.raises(FileNotFoundError, match="not found"):
        restore_backup("steam_0000000000")


def test_delete_backup(tmp_path):
    steam_dir = _write_steam_config(tmp_path)
    record = create_backup(steam_dir)

    assert delete_backup(record["id"]) is True
    assert not os.path.isdir(record["backup_path"])
    assert list_backups() == []


def test_delete_nonexistent():
    assert delete_backup("steam_0000000000") is False


def test_list_backups_without_meta(tmp_path):
    """A backup whose meta.json is missing still appears in the listing."""
    steam_dir = _write_steam_config(tmp_path)
    record = create_backup(steam_dir)

    os.remove(os.path.join(record["backup_path"], "meta.json"))

    backups = list_backups()
    assert len(backups) == 1
    keys = _file_keys(backups[0])
    assert "config.vdf" in keys


def test_backup_dir_uses_appdata(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\Test\AppData\Roaming")
    expected = os.path.join(r"C:\Users\Test\AppData\Roaming", "JWTractor", "backups")
    assert backup_dir() == expected


def test_partial_backup_only_existing_files(tmp_path):
    """If loginusers.vdf doesn't exist, only config.vdf is backed up."""
    steam_dir = _write_steam_config(tmp_path)
    os.remove(os.path.join(steam_dir, "config", "loginusers.vdf"))

    record = create_backup(steam_dir)
    keys = _file_keys(record)
    assert "config.vdf" in keys
    assert "loginusers.vdf" not in keys
