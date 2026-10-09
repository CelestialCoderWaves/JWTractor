"""Best-effort public Steam presence without a key, login, or saved token."""
from __future__ import annotations

import re
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit
from xml.etree import ElementTree

MAX_PROFILE = 512 * 1024
UNKNOWN = {"state": "unknown", "message": "Public presence unavailable. The profile may be private or Steam may be unavailable."}
_retry_after = 0.0


def parse_presence(raw, steam_id):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_PROFILE or re.search(br"<!\s*(DOCTYPE|ENTITY)\b", raw, re.I):
        return dict(UNKNOWN)
    try:
        profile = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ValueError):
        return dict(UNKNOWN)
    if profile.tag != "profile" or profile.findtext("steamID64") != steam_id:
        return dict(UNKNOWN)
    if profile.findtext("privacyState", "").lower() != "public":
        return dict(UNKNOWN, message="Public presence unavailable for this private profile.")
    state = profile.findtext("onlineState", "").lower()
    messages = {"online": "Online", "in-game": "In game", "offline": "Appears offline (may be Invisible)"}
    return {"state": state, "message": messages[state]} if state in messages else dict(UNKNOWN)


def fetch_presence(steam_id, *, timeout=6):
    global _retry_after
    if not isinstance(steam_id, str) or re.fullmatch(r"[0-9]{17}", steam_id) is None:
        return dict(UNKNOWN)
    if time.monotonic() < _retry_after:
        return dict(UNKNOWN, message="Steam rate-limited public status checks. Retry in a minute.", checked_at=int(time.time()))
    class SteamRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, newurl):
            parsed = urlsplit(newurl)
            if parsed.scheme != "https" or parsed.hostname != "steamcommunity.com":
                raise ValueError("Unexpected Steam profile redirect")
            return super().redirect_request(request, response, code, message, headers, newurl)
    try:
        request = Request(f"https://steamcommunity.com/profiles/{steam_id}/?xml=1")
        with build_opener(SteamRedirect()).open(request, timeout=timeout) as response:
            result = parse_presence(response.read(MAX_PROFILE + 1), steam_id)
    except HTTPError as exc:
        if exc.code == 429:
            try:
                delay = max(60, min(600, int(exc.headers.get("Retry-After", "60"))))
            except (AttributeError, ValueError, TypeError):
                delay = 60
            _retry_after = time.monotonic() + delay
            result = dict(UNKNOWN, message="Steam rate-limited public status checks. Retry in a minute.")
        else:
            result = dict(UNKNOWN, message=f"Steam could not provide public presence (HTTP {exc.code}).")
    except Exception:
        result = dict(UNKNOWN)
    return dict(result, checked_at=int(time.time()))
