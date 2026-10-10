"""Selected-account preferences: synthetic VDFs, no real Steam changes."""
import copy
import json
from pathlib import Path

import pytest

import steam_login as sl
from store import Store
from test_steam_login import ALICE, DAVE, jwt, workflow


def preferences_document(steam_id=DAVE):
    account_id = str(int(steam_id) & 0xffffffff)
    return {"UserLocalConfigStore": {
        "streaming_v2": {"EnableStreaming": "1", "EnableHardwareEncoding": "1"},
        "WebStorage": {
            "FriendStoreLocalPrefs_" + account_id: json.dumps({"ePersonaState": 1, "strNonFriendsAllowedToMsg": "synthetic", "custom": {"keep": True}}),
            "FriendStoreLocalPrefs_999": '{"ePersonaState":1}',
            "Keep": "value"},
        "friends": {"PersonaName": "synthetic", "AutoSignIntoFriends": "1", "PersonaStateDesired": "7"},
        "Software": {"Valve": {"Steam": {"apps": {"440": {"keep": "yes"}}}}}}}


@pytest.mark.parametrize("previous_presence", [1, 7])
def test_private_login_changes_only_presence_and_streaming(previous_presence):
    before = preferences_document()
    key = "FriendStoreLocalPrefs_" + str(int(DAVE) & 0xffffffff)
    prefs = json.loads(before["UserLocalConfigStore"]["WebStorage"][key])
    prefs["ePersonaState"] = previous_presence
    before["UserLocalConfigStore"]["WebStorage"][key] = json.dumps(prefs)
    original = copy.deepcopy(before)
    after = sl.merge_private_login(before, DAVE)
    assert before == original
    root = after["UserLocalConfigStore"]
    assert root["streaming_v2"] == {"EnableStreaming": "0", "EnableHardwareEncoding": "1"}
    assert json.loads(root["WebStorage"][key]) == {
        "ePersonaState": 0, "strNonFriendsAllowedToMsg": "synthetic", "custom": {"keep": True}}
    assert root["friends"] == {"PersonaName": "synthetic", "AutoSignIntoFriends": "0", "PersonaStateDesired": "0"}
    assert root["Software"] == before["UserLocalConfigStore"]["Software"]
    for entry in ("Keep", "FriendStoreLocalPrefs_999"):
        assert root["WebStorage"][entry] == before["UserLocalConfigStore"]["WebStorage"][entry]
    assert sl.merge_private_login(after, DAVE) == after


def test_private_login_supports_missing_file_and_case_insensitive_vdf():
    after = sl.merge_private_login({}, DAVE)
    assert after["UserLocalConfigStore"]["streaming_v2"]["EnableStreaming"] == "0"
    assert after["UserLocalConfigStore"]["friends"] == {"AutoSignIntoFriends": "0", "PersonaStateDesired": "0"}
    before = {"userlocalconfigstore": {"Streaming_V2": {"enablestreaming": "1"},
                                      "FRIENDS": {"AUTOSIGNINTOFRIENDS": "1", "PERSONASTATEDESIRED": "7", "Keep": "yes"}}}
    after = sl.merge_private_login(before, DAVE)
    assert after["userlocalconfigstore"]["Streaming_V2"] == {"enablestreaming": "0"}
    assert after["userlocalconfigstore"]["FRIENDS"] == {
        "AUTOSIGNINTOFRIENDS": "0", "PERSONASTATEDESIRED": "0", "Keep": "yes"}
    assert len(after) == 1


@pytest.mark.parametrize("raw", ["bad JSON", "[]", '{"ePersonaState":1,"ePersonaState":2}', '{"ePersonaState":NaN}', {"unexpected": "section"}])
def test_malformed_friends_preferences_are_not_replaced(raw):
    doc = preferences_document()
    key = "FriendStoreLocalPrefs_" + str(int(DAVE) & 0xffffffff)
    doc["UserLocalConfigStore"]["WebStorage"][key] = raw
    original = copy.deepcopy(doc)
    with pytest.raises(sl.SteamLoginError, match="friends preferences"):
        sl.merge_private_login(doc, DAVE)
    assert doc == original


def selected_path(workflow, steam_id=DAVE):
    return workflow.paths[0].parent.parent / "userdata" / str(int(steam_id) & 0xffffffff) / "config" / "localconfig.vdf"


def test_workflow_adds_selected_preferences_to_same_transaction(workflow, monkeypatch):
    path = selected_path(workflow)
    path.parent.mkdir(parents=True)
    before = sl.serialize_vdf(preferences_document()).encode()
    path.write_bytes(before)
    other_path = selected_path(workflow, ALICE)
    other_path.parent.mkdir(parents=True)
    other_path.write_bytes(b"other account unchanged")
    monkeypatch.setattr(sl, "wait_for_sign_in", lambda *args: ("confirmed", None))
    result = sl.login_account("dave", jwt({"sub": DAVE}), private_login=True)
    assert result["private_login"] is True
    assert len(result["backups"]) == 4
    assert Path(result["backups"][-1]).read_bytes() == before
    assert other_path.read_bytes() == b"other account unchanged"
    assert sl.read_config(path).data == sl.merge_private_login(preferences_document(), DAVE)


def test_disabled_option_does_not_touch_preferences(workflow):
    path = selected_path(workflow)
    result = sl.login_account("dave", jwt({"sub": DAVE}))
    assert not path.exists()
    assert result["private_login"] is False


def test_invalid_preferences_fail_before_closing_steam(workflow):
    path = selected_path(workflow)
    path.parent.mkdir(parents=True)
    doc = preferences_document()
    doc["UserLocalConfigStore"]["WebStorage"]["FriendStoreLocalPrefs_" + str(int(DAVE) & 0xffffffff)] = "broken"
    path.write_bytes(sl.serialize_vdf(doc).encode())
    with pytest.raises(sl.SteamLoginError, match="friends preferences"):
        sl.login_account("dave", jwt({"sub": DAVE}), private_login=True)
    assert workflow.calls == []


@pytest.mark.parametrize("name", ["AutoSignIntoFriends", "PersonaStateDesired"])
def test_invalid_startup_preferences_fail_before_closing_steam(workflow, name):
    path = selected_path(workflow)
    path.parent.mkdir(parents=True)
    doc = preferences_document()
    doc["UserLocalConfigStore"]["friends"][name] = {"unexpected": "section"}
    original = sl.serialize_vdf(doc).encode()
    path.write_bytes(original)
    with pytest.raises(sl.SteamLoginError, match="Friends & Chat startup preference"):
        sl.login_account("dave", jwt({"sub": DAVE}), private_login=True)
    assert workflow.calls == []
    assert path.read_bytes() == original


def test_failed_preferences_write_rolls_back_login_files(workflow, monkeypatch):
    path = selected_path(workflow)
    originals = [entry.read_bytes() for entry in workflow.paths]
    replace = sl.os.replace
    def fail_preferences(source, target):
        if target == path:
            raise PermissionError("Synthetic preference failure")
        replace(source, target)
    monkeypatch.setattr(sl.os, "replace", fail_preferences)
    with pytest.raises(sl.SteamLoginError, match="Synthetic preference failure"):
        sl.login_account("dave", jwt({"sub": DAVE}), private_login=True)
    assert [entry.read_bytes() for entry in workflow.paths] == originals
    assert not path.exists()
    assert workflow.calls == ["close"]


def test_store_remembers_toggle_and_old_stores_default_off(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text('{"version":1,"accounts":[]}', encoding="utf-8")
    store = Store(str(path))
    assert store.preferences["private_login"] is False
    store.set_private_login(True)
    assert Store(str(path)).preferences["private_login"] is True
    def fail():
        raise PermissionError("Synthetic save failure")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(PermissionError):
        store.set_private_login(False)
    assert store.preferences["private_login"] is True
    assert Store(str(path)).preferences["private_login"] is True


def cloud_document():
    return {"UserRoamingConfigStore": {"Software": {"Valve": {"Steam": {
        "cloudenabled": "1", "SteamDefaultDialog": "#app_games",
        "apps": {"440": {"cloudenabled": "1", "tags": {"0": "favorite"}},
                 "730": {"cloudenabled": "0"}}}}}, "Keep": "yes"}}


def cloud_path(workflow, steam_id=DAVE, legacy=False):
    account = selected_path(workflow, steam_id).parent.parent
    return account / ("config" if legacy else "7/remote") / "sharedconfig.vdf"


def test_cloud_merge_changes_only_account_wide_switch():
    before = cloud_document()
    original = copy.deepcopy(before)
    after = sl.merge_disable_cloud_sync(before)
    steam = after["UserRoamingConfigStore"]["Software"]["Valve"]["Steam"]
    assert steam.pop("cloudenabled") == "0"
    expected = copy.deepcopy(before)
    expected["UserRoamingConfigStore"]["Software"]["Valve"]["Steam"].pop("cloudenabled")
    assert after == expected
    assert before == original
    merged = sl.merge_disable_cloud_sync(before)
    assert sl.merge_disable_cloud_sync(merged) == merged


def test_cloud_merge_supports_missing_file_and_case_insensitive_keys():
    assert sl.merge_disable_cloud_sync({}) == {"UserRoamingConfigStore": {
        "Software": {"Valve": {"Steam": {"cloudenabled": "0"}}}}}
    before = {"userroamingconfigstore": {"software": {"VALVE": {"steam": {"CloudEnabled": "1"}}}}}
    after = sl.merge_disable_cloud_sync(before)
    assert after["userroamingconfigstore"]["software"]["VALVE"]["steam"] == {"CloudEnabled": "0"}


@pytest.mark.parametrize("private_login", [False, True])
def test_cloud_disabled_before_launch_with_backups_and_other_accounts_preserved(workflow, monkeypatch, private_login):
    paths = [cloud_path(workflow), cloud_path(workflow, legacy=True)]
    original = sl.serialize_vdf(cloud_document()).encode()
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
    other = cloud_path(workflow, ALICE)
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other account unchanged")
    if private_login:
        local = selected_path(workflow)
        local.write_bytes(sl.serialize_vdf(preferences_document()).encode())
    def launch(installation):
        for path in paths:
            assert sl.read_config(path).data == sl.merge_disable_cloud_sync(cloud_document())
        workflow.calls.append("launch")
    monkeypatch.setattr(sl, "_launch_steam", launch)
    result = sl.login_account("dave", jwt({"sub": DAVE}), private_login=private_login, disable_cloud_sync=True)
    assert result["disable_cloud_sync"] is True
    assert result["private_login"] is private_login
    assert len(result["backups"]) == 5 + private_login
    assert [Path(path).read_bytes() for path in result["backups"][-2:]] == [original, original]
    assert other.read_bytes() == b"other account unchanged"
    if private_login:
        assert sl.read_config(local).data == sl.merge_private_login(preferences_document(), DAVE)
    else:
        assert not selected_path(workflow).exists()


def test_cloud_option_creates_missing_account_config_without_creating_legacy_copy(workflow):
    result = sl.login_account("dave", jwt({"sub": DAVE}), disable_cloud_sync=True)
    assert sl.read_config(cloud_path(workflow)).data == sl.merge_disable_cloud_sync({})
    assert not cloud_path(workflow, legacy=True).exists()
    assert len(result["backups"]) == 3


def test_disabled_cloud_option_leaves_existing_configs_untouched(workflow):
    path = cloud_path(workflow)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"Unparsed and untouched")
    result = sl.login_account("dave", jwt({"sub": DAVE}), disable_cloud_sync=False)
    assert path.read_bytes() == b"Unparsed and untouched"
    assert result["disable_cloud_sync"] is False
    assert len(result["backups"]) == 3


@pytest.mark.parametrize("document", [
    {"UserRoamingConfigStore": "unexpected"},
    {"UserRoamingConfigStore": {"Software": {"Valve": {"Steam": {"cloudenabled": {"unexpected": "section"}}}}}},
])
def test_invalid_cloud_config_fails_before_closing_steam(workflow, document):
    path = cloud_path(workflow)
    path.parent.mkdir(parents=True)
    original = sl.serialize_vdf(document).encode()
    path.write_bytes(original)
    with pytest.raises(sl.SteamLoginError):
        sl.login_account("dave", jwt({"sub": DAVE}), disable_cloud_sync=True)
    assert workflow.calls == []
    assert path.read_bytes() == original


def test_cloud_config_reread_after_shutdown_preserves_latest_settings(workflow, monkeypatch):
    path = cloud_path(workflow)
    path.parent.mkdir(parents=True)
    path.write_bytes(sl.serialize_vdf(cloud_document()).encode())
    latest = cloud_document()
    latest["UserRoamingConfigStore"]["Keep"] = "changed during shutdown"
    def close(*args):
        path.write_bytes(sl.serialize_vdf(latest).encode())
    monkeypatch.setattr(sl, "close_steam", close)
    sl.login_account("dave", jwt({"sub": DAVE}), disable_cloud_sync=True)
    assert sl.read_config(path).data == sl.merge_disable_cloud_sync(latest)


@pytest.mark.parametrize("existing", [False, True])
def test_failed_cloud_write_restores_entire_transaction_and_does_not_launch(workflow, monkeypatch, existing):
    path = cloud_path(workflow)
    legacy = cloud_path(workflow, legacy=True)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(sl.serialize_vdf(cloud_document()).encode())
    if existing:
        path.parent.mkdir(parents=True)
        path.write_bytes(sl.serialize_vdf(cloud_document()).encode())
    tracked = workflow.paths + [path, legacy]
    originals = [entry.read_bytes() if entry.exists() else None for entry in tracked]
    replace = sl.os.replace
    def fail_legacy(source, target):
        if target == legacy:
            raise PermissionError("Synthetic Cloud write failure")
        replace(source, target)
    monkeypatch.setattr(sl.os, "replace", fail_legacy)
    with pytest.raises(sl.SteamLoginError, match="Synthetic Cloud write failure"):
        sl.login_account("dave", jwt({"sub": DAVE}), disable_cloud_sync=True)
    assert [entry.read_bytes() if entry.exists() else None for entry in tracked] == originals
    assert workflow.calls == ["close"]


def test_cloud_preference_defaults_on_remembers_off_and_rolls_back_save_failure(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text('{"version":1,"accounts":[],"preferences":{"private_login":true}}', encoding="utf-8")
    store = Store(str(path))
    assert store.preferences["disable_cloud_sync"] is True
    store.set_disable_cloud_sync(False)
    assert Store(str(path)).preferences == {"private_login": True, "disable_cloud_sync": False,
                                           "use_cs2_launch_options": False, "cs2_launch_options": "",
                                           "cs2_settings_source": ""}
    def fail():
        raise PermissionError("Synthetic save failure")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(PermissionError):
        store.set_disable_cloud_sync(True)
    assert store.preferences["disable_cloud_sync"] is False
    assert Store(str(path)).preferences["disable_cloud_sync"] is False


@pytest.mark.parametrize("raw", ['"false"', '0', 'null', '{}'])
def test_invalid_cloud_store_preference_keeps_default_on(tmp_path, raw):
    path = tmp_path / "accounts.json"
    path.write_text('{"accounts":[],"preferences":{"disable_cloud_sync":' + raw + '}}', encoding="utf-8")
    assert Store(str(path)).preferences["disable_cloud_sync"] is True
