"""CS2 preferences use synthetic data and mocked Steam, never real accounts."""
import copy
import json
from pathlib import Path

import pytest

import steam_login as sl
from store import Store
from test_steam_login import ALICE, DAVE, jwt, workflow
from test_login_preferences import preferences_document, selected_path


OPTIONS = '-console +exec "custom settings.cfg" -w 1920 -h 1080'


def document():
    data = preferences_document()
    apps = data["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]
    apps["730"] = {"LaunchOptions": "-old", "cloud": {"keep": "yes"}, "LastPlayed": "123"}
    return data


def test_merge_preserves_everything_except_cs2_options_and_round_trips_quotes():
    before = document()
    original = copy.deepcopy(before)
    after = sl.merge_cs2_launch_options(before, OPTIONS)
    expected = copy.deepcopy(before)
    expected["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]["730"]["LaunchOptions"] = OPTIONS
    assert after == expected
    assert before == original
    assert sl.parse_vdf(sl.serialize_vdf(after)) == after


def test_merge_missing_and_case_insensitive_sections():
    data = {"userlocalconfigstore": {"software": {"valve": {"steam": {"Apps": {"730": {"launchoptions": "-old"}}}}}}}
    after = sl.merge_cs2_launch_options(data, "")
    assert after["userlocalconfigstore"]["software"]["valve"]["steam"]["Apps"]["730"] == {"launchoptions": ""}
    after = sl.merge_cs2_launch_options({}, OPTIONS)
    assert after["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]["730"]["LaunchOptions"] == OPTIONS


@pytest.mark.parametrize("options", [1, {}, "x" * 4097, "-console\n-exec", "bad\x00value", "\t-console", "\ud800"])
def test_invalid_options_fail_before_any_steam_operation(workflow, options):
    with pytest.raises(sl.SteamLoginError, match="CS2 launch options"):
        sl.login_account("dave", jwt({"sub": DAVE}), cs2_launch_options=options)
    assert workflow.calls == []
    assert not selected_path(workflow).exists()


@pytest.mark.parametrize("private_login,disable_cloud_sync", [(False, False), (True, False), (True, True)])
def test_login_saves_options_before_launch_preserves_accounts_and_combines_preferences(workflow, monkeypatch, private_login, disable_cloud_sync):
    path = selected_path(workflow)
    path.parent.mkdir(parents=True)
    original = sl.serialize_vdf(document()).encode()
    path.write_bytes(original)
    other = selected_path(workflow, ALICE)
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other account unchanged")
    expected = sl.merge_cs2_launch_options(document(), OPTIONS)
    if private_login:
        expected = sl.merge_private_login(expected, DAVE)
    def launch(*args):
        assert sl.read_config(path).data == expected
        workflow.calls.append("launch")
    monkeypatch.setattr(sl, "_launch_steam", launch)
    result = sl.login_account("dave", jwt({"sub": DAVE}), cs2_launch_options=OPTIONS,
                              private_login=private_login, disable_cloud_sync=disable_cloud_sync)
    assert result["cs2_launch_options_applied"] is True
    assert len(result["backups"]) == 4
    assert Path(result["backups"][-1]).read_bytes() == original
    assert other.read_bytes() == b"other account unchanged"
    assert workflow.calls[-1] == "launch"


def test_off_leaves_unparseable_localconfig_untouched_and_empty_explicitly_clears(workflow):
    path = selected_path(workflow)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bad data must be left alone when options are off")
    result = sl.login_account("dave", jwt({"sub": DAVE}))
    assert result["cs2_launch_options_applied"] is False
    assert path.read_bytes() == b"bad data must be left alone when options are off"
    path.write_bytes(sl.serialize_vdf(document()).encode())
    sl.login_account("dave", jwt({"sub": DAVE}), cs2_launch_options="")
    apps = sl.read_config(path).data["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]
    assert apps["730"]["LaunchOptions"] == ""


def test_malformed_launch_options_section_is_rejected_before_shutdown(workflow):
    path = selected_path(workflow)
    path.parent.mkdir(parents=True)
    data = document()
    data["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]["730"]["LaunchOptions"] = {"unexpected": "section"}
    path.write_bytes(sl.serialize_vdf(data).encode())
    with pytest.raises(sl.SteamLoginError, match="preference type"):
        sl.login_account("dave", jwt({"sub": DAVE}), cs2_launch_options=OPTIONS)
    assert workflow.calls == []


def test_failed_options_write_rolls_back_credentials_and_does_not_launch(workflow, monkeypatch):
    path = selected_path(workflow)
    path.parent.mkdir(parents=True)
    path.write_bytes(sl.serialize_vdf(document()).encode())
    originals = [entry.read_bytes() for entry in workflow.paths + [path]]
    replace = sl.os.replace
    def fail(source, target):
        if target == path:
            raise PermissionError("Synthetic CS2 write failure")
        replace(source, target)
    monkeypatch.setattr(sl.os, "replace", fail)
    with pytest.raises(sl.SteamLoginError, match="Synthetic CS2 write failure"):
        sl.login_account("dave", jwt({"sub": DAVE}), cs2_launch_options=OPTIONS)
    assert [entry.read_bytes() for entry in workflow.paths + [path]] == originals
    assert workflow.calls == ["close"]


def test_store_persists_options_and_enable_flag_and_restores_after_failed_save(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    store = Store(str(path))
    assert store.preferences["use_cs2_launch_options"] is False
    assert store.preferences["cs2_launch_options"] == ""
    store.set_cs2_launch_options(OPTIONS)
    store.set_use_cs2_launch_options(True)
    reloaded = Store(str(path))
    assert reloaded.preferences["use_cs2_launch_options"] is True
    assert reloaded.preferences["cs2_launch_options"] == OPTIONS
    def fail():
        raise PermissionError("Synthetic save failure")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(PermissionError):
        store.set_cs2_launch_options("-different")
    assert store.preferences["cs2_launch_options"] == OPTIONS
    assert Store(str(path)).preferences["cs2_launch_options"] == OPTIONS


@pytest.mark.parametrize("preferences", [
    {"use_cs2_launch_options": True},
    {"use_cs2_launch_options": True, "cs2_launch_options": 123},
    {"use_cs2_launch_options": True, "cs2_launch_options": "bad\noptions"},
])
def test_corrupt_or_incomplete_preferences_disable_applying_options(tmp_path, preferences):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": [], "preferences": preferences}), encoding="utf-8")
    store = Store(str(path))
    assert store.preferences["use_cs2_launch_options"] is False
    assert store.preferences["cs2_launch_options"] == ""
