"""Public presence is tested without Steam, credentials or network calls."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import presence

STEAM_ID = "76561198000000000"


@pytest.fixture(autouse=True)
def reset_rate_limit(monkeypatch):
    monkeypatch.setattr(presence, "_retry_after", 0.0)


def profile(state="online", privacy="public", identity=STEAM_ID):
    return f"<profile><steamID64>{identity}</steamID64><privacyState>{privacy}</privacyState><onlineState>{state}</onlineState></profile>"


@pytest.mark.parametrize("state", ["online", "in-game", "offline"])
def test_presence_states_are_distinct_and_offline_does_not_rule_out_invisible(state):
    result = presence.parse_presence(profile(state), STEAM_ID)
    assert result["state"] == state
    if state == "offline":
        assert "Invisible" in result["message"]


@pytest.mark.parametrize("raw", [profile(privacy="private"), profile(identity="76561198000000001"),
                                 profile(state="unexpected"), "<html>Sign in</html>", "<profile>",
                                 '<!DOCTYPE profile [<!ENTITY x "online">]><profile/>', b"x" * (presence.MAX_PROFILE + 1)],
                         ids=["private", "wrong-account", "unexpected-state", "html", "truncated", "doctype", "oversized"])
def test_hidden_invalid_or_wrong_account_presence_is_unknown_not_offline(raw):
    assert presence.parse_presence(raw, STEAM_ID)["state"] == "unknown"


def test_network_request_only_sends_public_steam_id_and_has_bounded_read(monkeypatch):
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            assert size == presence.MAX_PROFILE + 1
            return profile("in-game").encode()
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 6
            assert not request.has_header("Authorization")
            assert not request.has_header("Cookie")
            return Response()
    monkeypatch.setattr(presence, "build_opener", lambda *args: Opener())
    result = presence.fetch_presence(STEAM_ID)
    assert result["state"] == "in-game" and result["checked_at"] > 0
    assert calls[0].full_url == f"https://steamcommunity.com/profiles/{STEAM_ID}/?xml=1"


def test_unavailable_network_is_unknown_and_invalid_identity_never_requests(monkeypatch):
    monkeypatch.setattr(presence, "build_opener", lambda *a: (_ for _ in ()).throw(OSError("offline")))
    assert presence.fetch_presence(STEAM_ID)["state"] == "unknown"
    assert presence.fetch_presence("bad")["state"] == "unknown"


def test_rate_limit_honors_retry_after_without_repeating_requests(monkeypatch):
    calls = []
    now = [100.0]
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise presence.HTTPError(request.full_url, 429, "Rate limited", {"Retry-After": "120"}, None)
    monkeypatch.setattr(presence, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(presence.time, "monotonic", lambda: now[0])
    assert presence.fetch_presence(STEAM_ID)["state"] == "unknown"
    assert presence._retry_after == 220.0
    now[0] = 219.0
    assert "rate-limited" in presence.fetch_presence(STEAM_ID)["message"]
    assert len(calls) == 1
    now[0] = 221.0
    presence.fetch_presence(STEAM_ID)
    assert len(calls) == 2


def test_profile_redirect_rejects_an_unrelated_host(monkeypatch):
    def opener(redirect):
        redirect.redirect_request(presence.Request("https://steamcommunity.com/"), None, 302,
                                  "Redirect", {}, "https://example.com/profile")
        pytest.fail("Unrelated host was accepted")
    monkeypatch.setattr(presence, "build_opener", opener)
    assert presence.fetch_presence(STEAM_ID)["state"] == "unknown"
