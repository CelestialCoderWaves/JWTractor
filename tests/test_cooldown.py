"""Synthetic Steam pages and local caches; no real sign-in or account data."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cooldown import matchmaking_url, parse_matchmaking_page
from store import Store

STEAM_ID = "76561199749125703"
OTHER = "76561198000000001"
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
URL = matchmaking_url(STEAM_ID)


def page(table, identity=STEAM_ID):
    return (f'<html><body><script>var g_steamID = "{identity}";</script>'
            '<h1>Personal Game Data</h1>' + table + '</body></html>')


RANKS = '<table class="generic_kv_table"><tr><th>Matchmaking Mode</th><th>Wins</th></tr><tr><td>Premier</td><td>20</td></tr></table>'


def cooldown_table(expiry):
    return ('<table class="generic_kv_table"><tr><th>Competitive Cooldown Expiration</th>'
            '<th>Competitive Cooldown Level</th></tr>'
            f'<tr><td>{expiry}</td><td>2</td></tr></table>')


def parse(html, url=URL, **kwargs):
    return parse_matchmaking_page(html, url, STEAM_ID, now=NOW, **kwargs)


def test_clear_only_on_recognized_authenticated_matchmaking_page():
    assert parse(page(RANKS))["state"] == "clear"
    assert parse(page('<table><tr><th>Unrelated data</th></tr></table>'))["state"] == "unknown"
    assert parse('<html><body>Personal Game Data</body></html>')["state"] == "unknown"


@pytest.mark.parametrize("expiry", ["2026-10-09 12:30:00 GMT", "2026-10-09 12:30:00", "2026-10-09 12:30 GMT", "2026-10-09"])
def test_active_cooldown_dates(expiry):
    result = parse(page(cooldown_table(expiry)))
    assert result["state"] == "active"
    assert result["expires_at"] > NOW.timestamp()
    assert "2026-10-09" in result["message"]


@pytest.mark.parametrize("expiry", ["2026-10-07 12:30:00 GMT", "", "None", "N/A"])
def test_expired_or_empty_cooldown_is_clear(expiry):
    assert parse(page(cooldown_table(expiry)))["state"] == "clear"


@pytest.mark.parametrize("expiry", ["Permanent", "10000-01-01 00:00:00", "Unexpected value"])
def test_unreadable_expiry_does_not_clear_reported_cooldown(expiry):
    assert parse(page(cooldown_table(expiry)))["state"] == "active"


def test_heading_without_expiry_is_still_reported():
    assert parse(page('<h2>Matchmaking Cooldown</h2>' + RANKS))["state"] == "active"


def test_script_text_cannot_create_fake_cooldown():
    assert parse(page('<script>let heading="Matchmaking Cooldown";</script>' + RANKS))["state"] == "clear"


def test_wrong_account_and_signed_out():
    assert parse(page(RANKS, OTHER))["state"] == "wrong_account"
    assert parse(page(RANKS), signed_in_id=OTHER)["state"] == "wrong_account"
    assert parse(page(RANKS).replace(f'"{STEAM_ID}"', "false"))["state"] == "sign_in"
    assert parse(page(RANKS), "https://steamcommunity.com/login/home/")["state"] == "sign_in"


@pytest.mark.parametrize("url", [URL.replace(STEAM_ID, OTHER), URL.replace("https:", "http:"),
                                     URL.replace("steamcommunity.com", "steamcommunity.com.evil.test"),
                                     URL.replace("matchmaking", "inventory")])
def test_wrong_source_is_unknown(url):
    assert parse(page(RANKS), url)["state"] == "unknown"


def test_unclosed_table_and_oversized_page_are_unknown():
    assert parse(page(RANKS + '<table><tr><th>Cooldown Expiration'))["state"] == "unknown"
    assert parse(page(RANKS) + " " * (2 * 1024 * 1024))["state"] == "unknown"


def test_steam_id_must_be_exact():
    with pytest.raises(ValueError):
        matchmaking_url('../other')


def test_cache_survives_restart_and_preserves_timestamp(tmp_path):
    store = Store(str(tmp_path / 'accounts.json'))
    store.set_cooldown(STEAM_ID, {"state": "clear", "message": "No active matchmaking cooldown"}, checked_at=1700000000)
    reopened = Store(store.path)
    assert reopened.cooldowns[STEAM_ID]["checked_at"] == 1700000000
    assert reopened.cooldowns[STEAM_ID]["state"] == "clear"
    assert OTHER not in reopened.cooldowns


def test_failed_save_preserves_previous_check(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'accounts.json'))
    store.set_cooldown(STEAM_ID, {"state": "clear", "message": "Previous"}, checked_at=1700000000)
    previous = dict(store.cooldowns[STEAM_ID])
    def fail():
        raise PermissionError("Synthetic failure")
    monkeypatch.setattr(store, 'save', fail)
    with pytest.raises(OSError):
        store.set_cooldown(STEAM_ID, {"state": "active", "message": "New result"})
    assert store.cooldowns[STEAM_ID] == previous
    assert Store(store.path).cooldowns[STEAM_ID] == previous


@pytest.mark.parametrize("result", [{"state": "unknown", "message": "Not signed in", "checked_at": 1700000000},
                                    {"state": "clear", "message": "Bad timestamp", "checked_at": True},
                                    {"state": "clear", "message": "Far future", "checked_at": 9999999999999},
                                    {"state": "clear", "message": [], "checked_at": 1700000000}])
def test_invalid_cache_is_ignored(tmp_path, result):
    path = tmp_path / 'accounts.json'
    path.write_text(json.dumps({"accounts": [], "cooldowns": {STEAM_ID: result}}))
    assert Store(str(path)).cooldowns == {}


def test_unknown_checks_cannot_replace_saved_data(tmp_path):
    store = Store(str(tmp_path / 'accounts.json'))
    store.set_cooldown(STEAM_ID, {"state": "clear", "message": "Previous"}, checked_at=1700000000)
    with pytest.raises(ValueError):
        store.set_cooldown(STEAM_ID, {"state": "unknown", "message": "Login required"})
    assert store.cooldowns[STEAM_ID]["checked_at"] == 1700000000


CLIENT_PAGE = {'type': 'page', 'url': 'https://steamcommunity.com/my/',
               'webSocketDebuggerUrl': 'ws://127.0.0.1:8080/devtools/page/test'}


def mock_client(monkeypatch, *, active=STEAM_ID):
    import cooldown
    monkeypatch.setattr(cooldown, 'current_steam_session', lambda: (active, 100) if active else None)
    monkeypatch.setattr(cooldown, '_client_targets', lambda: [CLIENT_PAGE])
    monkeypatch.setattr(cooldown, '_open_client_page', lambda sid: pytest.fail('Existing Steam Community page should be used'))
    monkeypatch.setattr(cooldown, '_evaluate', lambda target, expression: {
        'url': URL, 'html': page(RANKS), 'signedIn': STEAM_ID})
    return cooldown


def test_check_uses_existing_signed_in_steam_page(monkeypatch):
    client = mock_client(monkeypatch)
    assert client.check_client_cooldown(STEAM_ID)['state'] == 'clear'


@pytest.mark.parametrize('active,state', [(None, 'unknown'), (OTHER, 'wrong_account')])
def test_check_requires_same_active_steam_account(monkeypatch, active, state):
    client = mock_client(monkeypatch, active=active)
    monkeypatch.setattr(client, '_client_targets', lambda: pytest.fail('Wrong account must not be queried'))
    assert client.check_client_cooldown(STEAM_ID)['state'] == state


def test_account_change_during_check_cannot_cache_result(monkeypatch):
    client = mock_client(monkeypatch)
    sessions = iter([(STEAM_ID, 100), (STEAM_ID, 100), (OTHER, 100)])
    monkeypatch.setattr(client, 'current_steam_session', lambda: next(sessions))
    assert client.check_client_cooldown(STEAM_ID)['state'] == 'wrong_account'


def test_missing_community_page_opens_in_steam_only(monkeypatch):
    client = mock_client(monkeypatch)
    targets = iter([[], [CLIENT_PAGE]])
    opened = []
    monkeypatch.setattr(client, '_client_targets', lambda: next(targets))
    monkeypatch.setattr(client, '_open_client_page', opened.append)
    monkeypatch.setattr(client.time, 'sleep', lambda seconds: None)
    assert client.check_client_cooldown(STEAM_ID)['state'] == 'clear'
    assert opened == [STEAM_ID]


def test_delayed_steam_interface_retries_without_opening_external_browser(monkeypatch):
    client = mock_client(monkeypatch)
    calls = []
    def targets():
        calls.append(True)
        if len(calls) == 1:
            raise client.ClientCheckError('Still starting')
        return [CLIENT_PAGE]
    monkeypatch.setattr(client, '_client_targets', targets)
    monkeypatch.setattr(client.time, 'sleep', lambda seconds: None)
    assert client.check_client_cooldown(STEAM_ID)['state'] == 'clear'
    assert len(calls) == 2


def test_cancelled_check_cannot_read_page(monkeypatch):
    import threading
    client = mock_client(monkeypatch)
    monkeypatch.setattr(client, '_client_targets', lambda: pytest.fail('Cancelled check should not query Steam'))
    cancel = threading.Event()
    cancel.set()
    assert client.check_client_cooldown(STEAM_ID, cancel=cancel)['state'] == 'cancelled'


@pytest.mark.parametrize('address', ['ws://evil.test:8080/devtools/page/test', 'wss://localhost:8080/devtools/page/test',
                                     'ws://127.0.0.1:8081/devtools/page/test', 'ws://user:secret@localhost:8080/devtools/page/test',
                                     'ws://localhost:bad/devtools/page/test'])
def test_page_transport_stays_on_loopback(address):
    import cooldown
    with pytest.raises(cooldown.ClientCheckError, match='invalid local'):
        cooldown._evaluate({'webSocketDebuggerUrl':address}, 'test')


def test_transport_awaits_page_data_and_disables_proxy(monkeypatch):
    import cooldown, types
    captured = {}
    class Connection:
        sent = None
        def send(self, message): self.sent = json.loads(message)
        def recv(self): return json.dumps({'id':1,'result':{'result':{'value':{'html':'synthetic game data'}}}})
        def close(self): captured['closed'] = True
    connection = Connection()
    def connect(address, **kwargs):
        captured.update(address=address, kwargs=kwargs)
        return connection
    monkeypatch.setitem(sys.modules, 'websocket', types.SimpleNamespace(create_connection=connect))
    assert cooldown._evaluate(CLIENT_PAGE, 'synthetic expression') == {'html':'synthetic game data'}
    assert connection.sent['params']['awaitPromise'] is True
    assert captured['address'].startswith('ws://127.0.0.1:8080/')
    assert '127.0.0.1' in captured['kwargs']['http_no_proxy']
    assert captured['closed']


def test_game_request_returns_only_game_content():
    import cooldown
    script = cooldown._game_data_expression(STEAM_ID)
    assert 'credentials:' in script
    assert 'script,style,input,form,template,noscript' in script
    assert 'document.cookie' not in script
    assert 'Network.getCookies' not in script
    assert 'SteamClient.Auth' not in script
    assert URL in script


@pytest.mark.parametrize('steam_marker', [True, False])
def test_client_interface_does_not_use_another_browsers_debug_port(monkeypatch, steam_marker):
    import cooldown
    targets = [CLIENT_PAGE]
    if steam_marker:
        targets.append({'title':'SharedJSContext', 'type':'other', 'url':'about:blank'})
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return cooldown.CLIENT_ENDPOINT
        def read(self, limit): return json.dumps(targets).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == cooldown.CLIENT_ENDPOINT
            return Response()
    monkeypatch.setattr(cooldown, 'build_opener', lambda *handlers: Opener())
    if steam_marker:
        assert cooldown._client_targets() == [CLIENT_PAGE]
    else:
        with pytest.raises(cooldown.ClientCheckError, match='not Steam'):
            cooldown._client_targets()
