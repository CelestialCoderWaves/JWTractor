"""Native login tests: synthetic tokens, temp files, mocked Steam processes."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import steam_login as sl

ALICE = "76561198000000000"
BOB = "76561198000000001"
DAVE = "76561198000000002"


def jwt(payload=None):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    claims = {"sub": ALICE, "exp": 9999999999, "iss": "steam", "aud": ["client", "web", "renew", "derive"]}
    claims.update(payload or {})
    return ".".join([encode({"alg": "RS256"}), encode(claims), "synthetic_signature"])


def remembered():
    return [
        sl.parse_vdf(f'InstallConfigStore {{ Software {{ Valve {{ Steam {{ Accounts {{ alice {{ SteamID "{ALICE}" Extra keep }} bob {{ SteamID "{BOB}" }} }} Rate 123 }} }} }} }}'),
        sl.parse_vdf(f'users {{ {ALICE} {{ AccountName alice PersonaName "My name" RememberPassword 1 AllowAutoLogin 1 MostRecent 1 Timestamp 100 }} {BOB} {{ AccountName bob RememberPassword 1 AllowAutoLogin 1 MostRecent 0 Timestamp 101 Custom keep }} }}'),
        sl.parse_vdf(f'MachineUserConfigStore {{ Software {{ Valve {{ Steam {{ ConnectCache {{ {sl.cache_key("alice")} synthetic-alice {sl.cache_key("bob")} synthetic-bob }} Other keep }} }} }} }}'),
    ]


def current_remembered():
    documents = remembered()
    for user in documents[1]["users"].values():
        user["AutoLogin"] = user.pop("MostRecent")
        user.pop("AllowAutoLogin")
    return documents


def configs_on_disk(tmp_path):
    files = [tmp_path / name for name in ("config.vdf", "loginusers.vdf", "local.vdf")]
    for path, data in zip(files, remembered()):
        path.write_bytes(sl.serialize_vdf(data).encode())
    return [sl.read_config(path) for path in files]


@pytest.mark.parametrize("claims", [{"sub": "x"}, {"sub": int(ALICE)}, {"sub": ALICE, "exp": True}, {"sub": ALICE, "exp": "2000"}, {"sub": ALICE, "exp": 1000}, {"sub": ALICE, "nbf": 2000}, {"sub": ALICE, "exp": None}, {"sub": ALICE, "exp": 8640000000001}])
def test_token_preflight_rejects_invalid_or_unusable_claims(claims):
    with pytest.raises(sl.SteamLoginError):
        sl.validate_token(jwt(claims), now=1000)


def test_token_and_name_validation():
    assert sl.validate_token(jwt(), now=1000)["sub"] == ALICE
    for name in ("", "Alice Smith", "a\nb", "a&b", "x" * 65):
        with pytest.raises(sl.SteamLoginError):
            sl.validate_account_name(name)
    with pytest.raises(sl.SteamLoginError):
        sl.validate_token("a.b.c")
    assert sl.cache_key("123456789") == "cbf439261"


@pytest.mark.parametrize("claims", [{"iss": "other"}, {"aud": None}, {"aud": "client"},
                                  {"aud": ["client", 1]}, {"aud": ["web", "renew"]},
                                  {"aud": ["client", "web", "renew"]}])
def test_login_requires_a_steam_client_refresh_token(claims):
    with pytest.raises(sl.SteamLoginError):
        sl.validate_token(jwt(claims))


def test_refresh_token_without_renewal_permission_can_still_log_in():
    assert sl.validate_token(jwt({"aud": ["client", "derive"]}))["sub"] == ALICE


def response(steam_id=ALICE, result="OK"):
    account = f"U:1:{int(steam_id) & 0xffffffff}" if result == "OK" else "I:0:0"
    return f"[2026-10-05 08:30:43] CClientConnectionMgr::OnClientLogOnResponse() : [{account}] '{result}'\n"


def test_logon_parser_confirms_only_the_selected_account_and_sanitizes_errors():
    assert sl.parse_logon_result(response(), ALICE) == ("confirmed", None)
    assert sl.parse_logon_result(response(BOB), ALICE) == ("other_account", None)
    assert sl.parse_logon_result(response(result="Access Denied"), ALICE) == ("rejected", "Access denied")
    assert sl.parse_logon_result(response(result="sensitive unknown error"), ALICE) == ("rejected", "Login rejected")
    assert sl.parse_logon_result("processing complete\n", ALICE) is None


def test_sign_in_monitor_ignores_old_success_and_reads_only_new_response(tmp_path):
    path = tmp_path / "connection_log.txt"
    path.write_text(response())
    checkpoint = sl.log_checkpoint(path)
    with path.open("a") as handle:
        handle.write(response(result="Access Denied"))
    assert sl.wait_for_sign_in(path, ALICE, checkpoint, timeout=1) == ("rejected", "Access denied")


def test_sign_in_monitor_waits_for_a_complete_line(tmp_path):
    path = tmp_path / "connection_log.txt"
    path.write_text("")
    checkpoint = sl.log_checkpoint(path)
    partial = response().rstrip("\n")
    path.write_text(partial)
    assert sl.wait_for_sign_in(path, ALICE, checkpoint, timeout=0.01) == ("unconfirmed", None)
    path.write_text(partial + "\n")
    assert sl.wait_for_sign_in(path, ALICE, checkpoint, timeout=1) == ("confirmed", None)


def test_sign_in_monitor_handles_truncated_logs_missing_logs_and_cancel(tmp_path):
    path = tmp_path / "connection_log.txt"
    path.write_text("old log " * 1000)
    checkpoint = sl.log_checkpoint(path)
    path.write_text(response())
    assert sl.wait_for_sign_in(path, ALICE, checkpoint, timeout=1) == ("confirmed", None)
    assert sl.wait_for_sign_in(tmp_path / "missing.txt", ALICE, (None, 0), timeout=0.01) == ("unconfirmed", None)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(sl.LoginCancelled, match="already running"):
        sl.wait_for_sign_in(path, ALICE, checkpoint, cancel)


def test_vdf_round_trip_and_case_preservation():
    text = '// comment\nRoot { "Escaped" "quote\\\" slash\\\\ tab\\t line\\n café" Empty "" } "__proto__" safe'
    assert sl.parse_vdf(sl.serialize_vdf(sl.parse_vdf(text))) == sl.parse_vdf(text)
    before = remembered()
    before = [sl.parse_vdf(sl.serialize_vdf(data).replace('"Software"', '"SOFTWARE"').replace('"users"', '"USERS"')) for data in before]
    after = sl.merge_account(before, DAVE, "dave", "synthetic-dave", 200)
    sl.assert_preservation(before, after, DAVE, "dave")
    assert "USERS" in after[1]
    assert "Software" not in after[0]["InstallConfigStore"]


@pytest.mark.parametrize("text", ['}', 'root {', 'key', 'key }', '"unfinished', 'root { x 1 X 2 }', '#base "other.vdf"', 'key value [$WIN32]', 'root { x "bad\x00" }'])
def test_ambiguous_or_broken_vdf_is_rejected(text):
    with pytest.raises(sl.SteamLoginError):
        sl.parse_vdf(text)


def test_merging_preserves_every_other_account_and_leaves_originals_untouched():
    before = remembered()
    original = copy.deepcopy(before)
    after = sl.merge_account(before, DAVE, "dave", "synthetic-dave", 200)
    assert before == original
    assert after[1]["users"][BOB] == before[1]["users"][BOB]
    assert after[1]["users"][ALICE] == {**before[1]["users"][ALICE], "MostRecent": "0"}
    old_cache = before[2]["MachineUserConfigStore"]["Software"]["Valve"]["Steam"]["ConnectCache"]
    new_cache = after[2]["MachineUserConfigStore"]["Software"]["Valve"]["Steam"]["ConnectCache"]
    assert all(new_cache[key] == value for key, value in old_cache.items())
    selected = sl.merge_account(after, ALICE, "alice", "updated-synthetic-alice", 201)
    assert selected[1]["users"][ALICE]["PersonaName"] == "My name"
    assert selected[1]["users"][DAVE]["RememberPassword"] == "1"


@pytest.mark.parametrize("steam_id,name", [(ALICE, "alice"), (DAVE, "dave")])
def test_current_steam_selects_account_using_autologin_and_preserves_credentials(steam_id, name):
    before = current_remembered()
    original = copy.deepcopy(before)
    after = sl.merge_account(before, steam_id, name, "synthetic-selected", 200)
    assert before == original
    users = after[1]["users"]
    assert users[steam_id]["AutoLogin"] == "1"
    assert sum(user.get("AutoLogin") == "1" for user in users.values()) == 1
    assert all("MostRecent" not in user and "AllowAutoLogin" not in user for user in users.values())
    for user_id, user in before[1]["users"].items():
        if user_id != steam_id:
            assert users[user_id] == {**user, "AutoLogin": "0"}
    old_cache = sl._section(before[2], "MachineUserConfigStore", "Software", "Valve", "Steam", "ConnectCache")
    new_cache = sl._section(after[2], "MachineUserConfigStore", "Software", "Valve", "Steam", "ConnectCache")
    assert all(new_cache[key] == value for key, value in old_cache.items() if key != sl.cache_key(name))
    sl.assert_preservation(before, after, steam_id, name)


def test_new_and_mixed_steam_formats_have_one_selected_account():
    fresh = sl.merge_account([{}, {}, {}], DAVE, "dave", "synthetic", 200)
    assert fresh[1]["users"][DAVE]["AutoLogin"] == "1"
    assert "MostRecent" not in fresh[1]["users"][DAVE]
    before = current_remembered()
    before[1]["users"][ALICE]["MostRecent"] = "1"
    after = sl.merge_account(before, DAVE, "dave", "synthetic", 200)
    assert after[1]["users"][ALICE]["AutoLogin"] == "0"
    assert after[1]["users"][ALICE]["MostRecent"] == "0"
    assert after[1]["users"][DAVE]["AutoLogin"] == "1"
    assert "MostRecent" not in after[1]["users"][BOB]


def test_preservation_guard_rejects_enabling_another_modern_account():
    before = current_remembered()
    after = sl.merge_account(before, DAVE, "dave", "synthetic", 200)
    after[1]["users"][BOB]["AutoLogin"] = "1"
    with pytest.raises(sl.SteamLoginError, match="preservation"):
        sl.assert_preservation(before, after, DAVE, "dave")


@pytest.mark.parametrize("value", [None, "0", "1"])
def test_login_disables_startup_chooser_without_changing_other_auth_settings(value):
    before = remembered()
    auth = sl._section(before[0], "InstallConfigStore", "WebStorage", "Auth", create=True)
    auth["KeepThisPreference"] = "synthetic-unchanged"
    if value is not None:
        auth["alwaysshowuserchooser"] = value
    original = copy.deepcopy(before)
    after = sl.merge_account(before, DAVE, "dave", "synthetic-dave", 200)
    assert before == original
    updated_auth = sl._section(after[0], "InstallConfigStore", "WebStorage", "Auth")
    assert updated_auth[sl._key(updated_auth, "AlwaysShowUserChooser")] == "0"
    assert updated_auth["KeepThisPreference"] == "synthetic-unchanged"
    assert len(updated_auth) == 2
    assert after[1]["users"][BOB] == before[1]["users"][BOB]
    assert sl._section(after[2], "MachineUserConfigStore", "Software", "Valve", "Steam", "ConnectCache")[sl.cache_key("bob")] == "synthetic-bob"
    sl.assert_preservation(before, after, DAVE, "dave")
    updated_auth["KeepThisPreference"] = "changed"
    with pytest.raises(sl.SteamLoginError, match="preservation"):
        sl.assert_preservation(before, after, DAVE, "dave")


def test_preservation_guard_cannot_enable_chooser_or_replace_an_auth_section():
    before = remembered()
    after = sl.merge_account(before, DAVE, "dave", "synthetic-dave", 200)
    auth = sl._section(after[0], "InstallConfigStore", "WebStorage", "Auth")
    auth["AlwaysShowUserChooser"] = "1"
    with pytest.raises(sl.SteamLoginError, match="preservation"):
        sl.assert_preservation(before, after, DAVE, "dave")
    auth = sl._section(before[0], "InstallConfigStore", "WebStorage", "Auth", create=True)
    auth["AlwaysShowUserChooser"] = {"Unexpected": "keep"}
    with pytest.raises(sl.SteamLoginError, match="preservation"):
        sl.merge_account(before, DAVE, "dave", "synthetic-dave", 200)


def test_chooser_preference_is_at_install_root_not_under_software():
    before = current_remembered()
    root_auth = sl._section(before[0], "InstallConfigStore", "WebStorage", "Auth", create=True)
    root_auth["AlwaysShowUserChooser"] = "1"
    other_auth = sl._section(before[0], "InstallConfigStore", "Software", "WebStorage", "Auth", create=True)
    other_auth["AlwaysShowUserChooser"] = "1"
    after = sl.merge_account(before, DAVE, "dave", "synthetic-dave", 200)
    assert sl._section(after[0], "InstallConfigStore", "WebStorage", "Auth")["AlwaysShowUserChooser"] == "0"
    assert sl._section(after[0], "InstallConfigStore", "Software", "WebStorage", "Auth") == other_auth
    sl.assert_preservation(before, after, DAVE, "dave")


def test_preservation_guard_rejects_deleted_credentials_or_changed_login_flags():
    before = remembered()
    for field in ("RememberPassword", "AllowAutoLogin", "Timestamp"):
        after = sl.merge_account(before, DAVE, "dave", "synthetic-dave", 200)
        after[1]["users"][BOB][field] = "changed"
        with pytest.raises(sl.SteamLoginError, match="preservation"):
            sl.assert_preservation(before, after, DAVE, "dave")
    after = sl.merge_account(before, DAVE, "dave", "synthetic-dave", 200)
    del after[2]["MachineUserConfigStore"]["Software"]["Valve"]["Steam"]["ConnectCache"][sl.cache_key("bob")]
    with pytest.raises(sl.SteamLoginError, match="preservation"):
        sl.assert_preservation(before, after, DAVE, "dave")


def test_identity_conflicts_and_hash_collisions_are_refused(monkeypatch):
    before = remembered()
    for steam_id, name in [(DAVE, "alice"), (ALICE, "different_name")]:
        with pytest.raises(sl.SteamLoginError, match="different saved|conflicts"):
            sl.merge_account(before, steam_id, name, "synthetic", 200)
    monkeypatch.setattr(sl, "cache_key", lambda name: "collision1")
    with pytest.raises(sl.SteamLoginError, match="collision"):
        sl.merge_account(before, DAVE, "dave", "synthetic", 200)


def test_file_writes_backup_exact_bytes_and_preserve_accounts_across_switches(tmp_path):
    configs = configs_on_disk(tmp_path)
    configs[0].path.write_bytes(b"\xef\xbb\xbf// original comment\r\n" + configs[0].original)
    original = configs[0].path.read_bytes()
    for steam_id, name in [(DAVE, "dave"), (ALICE, "alice"), (DAVE, "dave")]:
        configs = [sl.read_config(config.path) for config in configs]
        before = [config.data for config in configs]
        after = sl.merge_account(before, steam_id, name, "synthetic-new-token", 200)
        for config, data in zip(configs, after):
            config.data = data
        with sl.config_lock(configs[0].path):
            backups = sl.write_configs(configs)
        if name == "dave" and steam_id == DAVE and len(list(tmp_path.glob("config.vdf.*.bak"))) == 1:
            assert backups[0].read_bytes() == original
        saved = [sl.read_config(config.path).data for config in configs]
        sl.assert_preservation(before, saved, steam_id, name)
        assert saved[1]["users"][BOB]["RememberPassword"] == "1"
        assert saved[1]["users"][BOB]["AllowAutoLogin"] == "1"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.lock"))


def test_read_config_rejects_bad_encoding_directories_and_duplicates(tmp_path):
    path = tmp_path / "bad.vdf"
    for content in (b'key "\xff"', b'key x KEY y'):
        path.write_bytes(content)
        with pytest.raises(sl.SteamLoginError, match="Cannot read"):
            sl.read_config(path)
    with pytest.raises(sl.SteamLoginError, match="regular"):
        sl.read_config(tmp_path)


def test_concurrent_edits_are_not_overwritten(tmp_path):
    configs = configs_on_disk(tmp_path)
    configs[0].path.write_bytes(b"external edit")
    with pytest.raises(sl.SteamLoginError, match="changed during login"):
        sl.write_configs(configs)
    assert configs[0].path.read_bytes() == b"external edit"


def test_second_write_failure_rolls_back_first_and_cleans_temps(tmp_path, monkeypatch):
    configs = configs_on_disk(tmp_path)
    for config in configs:
        config.data["extra"] = "synthetic change"
    replace = sl.os.replace
    def fail_second(source, target):
        if target == configs[1].path:
            raise PermissionError("Synthetic disk failure")
        replace(source, target)
    monkeypatch.setattr(sl.os, "replace", fail_second)
    with sl.config_lock(configs[0].path):
        with pytest.raises(sl.SteamLoginError, match="Synthetic disk failure"):
            sl.write_configs(configs)
    assert all(config.path.read_bytes() == config.original for config in configs)
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.lock"))


def test_failed_rollback_preserves_external_edits_and_retains_lock(tmp_path, monkeypatch):
    configs = configs_on_disk(tmp_path)
    for config in configs:
        config.data["extra"] = "change"
    replace = sl.os.replace
    def external_edit_then_fail(source, target):
        if target == configs[1].path:
            configs[0].path.write_bytes(b"external first file")
            raise PermissionError("Synthetic failure")
        replace(source, target)
    monkeypatch.setattr(sl.os, "replace", external_edit_then_fail)
    with pytest.raises(sl.SteamLoginError, match="Could not safely restore") as failure:
        with sl.config_lock(configs[0].path):
            sl.write_configs(configs)
    assert failure.value.recovery_required
    assert configs[0].path.read_bytes() == b"external first file"
    assert Path(str(configs[0].path) + ".steam-nfa.lock").exists()


def test_cancellation_after_last_write_restores_originals(tmp_path, monkeypatch):
    configs = configs_on_disk(tmp_path)
    cancel = threading.Event()
    for config in configs:
        config.data["extra"] = "change"
    replace = sl.os.replace
    def cancel_after_write(source, target):
        replace(source, target)
        if target == configs[-1].path:
            cancel.set()
    monkeypatch.setattr(sl.os, "replace", cancel_after_write)
    with pytest.raises(sl.LoginCancelled):
        sl.write_configs(configs, cancel)
    assert all(config.path.read_bytes() == config.original for config in configs)


def test_shared_lock_refuses_overlapping_runs(tmp_path):
    path = tmp_path / "config.vdf"
    with sl.config_lock(path):
        with pytest.raises(sl.SteamLoginError, match="Another login"):
            with sl.config_lock(path):
                pytest.fail("Must not acquire overlapping lock")
    assert not Path(str(path) + ".steam-nfa.lock").exists()


def test_normal_steam_shutdown_never_force_kills(monkeypatch):
    checks = iter([True, True, False])
    calls = []
    monkeypatch.setattr(sl, "_steam_running", lambda: next(checks))
    monkeypatch.setattr(sl, "_run", lambda args: calls.append(args))
    monkeypatch.setattr(sl.time, "sleep", lambda duration: None)
    sl.close_steam(Path("fake-steam"))
    assert calls == [[str(Path("fake-steam") / "steam.exe"), "-shutdown"]]


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "IS_WINDOWS", True)
    installation = tmp_path / "Steam"
    (installation / "config").mkdir(parents=True)
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    paths = [installation / "config" / "config.vdf", installation / "config" / "loginusers.vdf", local / "Steam" / "local.vdf"]
    for path, data in zip(paths, remembered()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(sl.serialize_vdf(data).encode())
    calls = []
    monkeypatch.setattr(sl, "_find_installation", lambda: installation)
    monkeypatch.setattr(sl, "encrypt_with_dpapi", lambda token, name: "synthetic-encrypted")
    monkeypatch.setattr(sl, "close_steam", lambda path, cancel: calls.append("close"))
    monkeypatch.setattr(sl, "_set_autologin", lambda name: calls.append(("registry", name)))
    monkeypatch.setattr(sl, "_launch_steam", lambda path: calls.append("launch"))
    monkeypatch.setattr(sl, "wait_for_sign_in", lambda *args: ("unconfirmed", None))
    return SimpleNamespace(paths=paths, calls=calls)


def test_complete_workflow_preserves_accounts_and_returns_backup_paths(workflow):
    result = sl.login_account("dave", jwt({"sub": DAVE, "exp": 9999999999}))
    assert result["preserved_accounts"] == 2
    assert len(result["backups"]) == 3
    assert workflow.calls == ["close", ("registry", "dave"), "launch"]
    users = sl.read_config(workflow.paths[1]).data["users"]
    assert users[BOB]["RememberPassword"] == "1"
    auth = sl._section(sl.read_config(workflow.paths[0]).data, "InstallConfigStore", "WebStorage", "Auth")
    assert auth["AlwaysShowUserChooser"] == "0"


def test_login_normalizes_the_name_and_reports_the_client_result(workflow, monkeypatch):
    monkeypatch.setattr(sl, "wait_for_sign_in", lambda *args: ("rejected", "Access denied"))
    result = sl.login_account("DaVe", jwt({"sub": DAVE}))
    assert ("registry", "dave") in workflow.calls
    assert result["sign_in"] == "rejected"
    assert result["reason"] == "Access denied"
    assert sl.read_config(workflow.paths[1]).data["users"][BOB]["RememberPassword"] == "1"


def test_complete_workflow_writes_current_autologin_selection(workflow, monkeypatch):
    for path, data in zip(workflow.paths, current_remembered()):
        path.write_bytes(sl.serialize_vdf(data).encode())
    monkeypatch.setattr(sl, "wait_for_sign_in", lambda *args: ("confirmed", None))
    result = sl.login_account("dave", jwt({"sub": DAVE}))
    users = sl.read_config(workflow.paths[1]).data["users"]
    assert result["sign_in"] == "confirmed"
    assert users[DAVE]["AutoLogin"] == "1"
    assert users[ALICE]["AutoLogin"] == "0"
    assert users[BOB]["RememberPassword"] == "1"
    assert all("MostRecent" not in user for user in users.values())


def test_encryption_or_invalid_config_failure_never_closes_steam(workflow, monkeypatch):
    def fail(*args):
        raise sl.SteamLoginError("Synthetic encryption failure")
    monkeypatch.setattr(sl, "encrypt_with_dpapi", fail)
    with pytest.raises(sl.SteamLoginError, match="encryption"):
        sl.login_account("alice", jwt())
    assert workflow.calls == []
    monkeypatch.setattr(sl, "encrypt_with_dpapi", lambda *args: "synthetic")
    workflow.paths[0].write_bytes(b"broken {")
    with pytest.raises(sl.SteamLoginError, match="Cannot read"):
        sl.login_account("alice", jwt())
    assert workflow.calls == []


def test_cancellation_after_save_reports_saved_state_and_does_not_launch(workflow, monkeypatch):
    cancel = threading.Event()
    write = sl.write_configs
    def save_then_cancel(*args):
        backups = write(*args)
        cancel.set()
        return backups
    monkeypatch.setattr(sl, "write_configs", save_then_cancel)
    with pytest.raises(sl.LoginCancelled, match="after configuration was saved"):
        sl.login_account("dave", jwt({"sub": DAVE}), cancel=cancel)
    assert workflow.calls == ["close"]
    assert DAVE in sl.read_config(workflow.paths[1]).data["users"]


def test_registry_and_launch_failures_distinguish_saved_configuration(workflow, monkeypatch):
    def fail(*args):
        raise OSError("Synthetic subprocess failure")
    monkeypatch.setattr(sl, "_set_autologin", fail)
    result = sl.login_account("alice", jwt())
    assert "Select the account" in result["warning"]
    assert workflow.calls[-1] == "launch"
    monkeypatch.setattr(sl, "_launch_steam", fail)
    with pytest.raises(sl.SteamLoginError, match="Configuration was saved"):
        sl.login_account("alice", jwt())


def test_shutdown_failure_never_changes_configuration(workflow, monkeypatch):
    original = [path.read_bytes() for path in workflow.paths]
    def refuse_shutdown(*args):
        raise sl.SteamLoginError("Steam is still running.")
    monkeypatch.setattr(sl, "close_steam", refuse_shutdown)
    with pytest.raises(sl.SteamLoginError, match="still running"):
        sl.login_account("alice", jwt())
    assert [path.read_bytes() for path in workflow.paths] == original
    assert workflow.calls == []
    assert not list(workflow.paths[0].parent.glob("*.bak"))


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_native_dpapi_matches_dotnet_and_username_entropy():
    encrypted = sl.encrypt_with_dpapi("synthetic-test-token", "test_account")
    script = "; ".join([
        "$ErrorActionPreference = 'Stop'", "Add-Type -AssemblyName System.Security",
        "$hex = [Console]::In.ReadLine()", "$bytes = New-Object byte[] ($hex.Length / 2)",
        "for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($hex.Substring($i * 2, 2), 16) }",
        "$entropy = [Text.Encoding]::UTF8.GetBytes('test_account')",
        "$plain = [Security.Cryptography.ProtectedData]::Unprotect($bytes, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser)",
        "[Text.Encoding]::UTF8.GetString($plain)",
    ])
    executable = sl._windows_command(r"WindowsPowerShell\v1.0\powershell.exe")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", script],
                            input=encrypted + "\n", capture_output=True, text=True, timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    assert result.stdout.strip() == "synthetic-test-token"
