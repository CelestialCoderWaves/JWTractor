"""Read matchmaking data through the already signed-in Steam client.

The local page interface performs requests inside Steam. No separate browser,
password, or session-cookie database is used. Only game data is returned.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.parse import parse_qs, urlsplit

from steam import find_steam_dir

MAX_PAGE = 2 * 1024 * 1024
STEAM_ID_BASE = 76561197960265728
CLIENT_ENDPOINT = "http://127.0.0.1:8080/json/list"


class ClientCheckError(Exception):
    """A safe, user-facing failure without protocol payloads or credentials."""


def _steam_process_alive(pid):
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        size = ctypes.c_ulong(32768)
        path = ctypes.create_unicode_buffer(size.value)
        return bool(kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size))
                    and Path(path.value).name.lower() == "steam.exe")
    finally:
        kernel.CloseHandle(handle)


def current_steam_session():
    """Only accept Steam's active-account marker when its client is alive."""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as key:
            account = winreg.QueryValueEx(key, "ActiveUser")[0]
            pid = winreg.QueryValueEx(key, "pid")[0]
        if type(account) is int and 0 < account < 2 ** 32 and _steam_process_alive(pid):
            return str(STEAM_ID_BASE + account), pid
    except (OSError, ImportError):
        pass
    return None


def _client_targets():
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ClientCheckError("Steam’s local page interface returned an unexpected address.")
    try:
        # Loopback traffic must never be sent through a configured HTTP proxy.
        with build_opener(ProxyHandler({}), NoRedirect()).open(Request(CLIENT_ENDPOINT), timeout=2) as response:
            if response.geturl() != CLIENT_ENDPOINT:
                raise ClientCheckError("Steam’s local page interface returned an unexpected address.")
            raw = response.read(MAX_PAGE + 1)
        if len(raw) > MAX_PAGE:
            raise ValueError
        targets = json.loads(raw)
        if not isinstance(targets, list):
            raise ValueError
        if not any(isinstance(target, dict) and
                   ("SharedJSContext" in str(target.get("title", "")) or
                    urlsplit(target.get("url", "")).hostname == "steamloopback.host") for target in targets):
            raise ClientCheckError("Port 8080 is not Steam’s page interface. Close the conflicting app and restart Steam through JWTractor.")
        return [target for target in targets if isinstance(target, dict) and target.get("type") == "page"]
    except ClientCheckError:
        raise
    except Exception:
        raise ClientCheckError("Steam’s page interface is unavailable. Log in through JWTractor once to enable it.") from None


def _evaluate(target, expression):
    """Read only page data over the local CEF interface, without cookie APIs."""
    address = target.get("webSocketDebuggerUrl", "")
    try:
        parsed = urlsplit(address)
        valid = (parsed.scheme == "ws" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
                 and parsed.port == 8080 and parsed.username is None and parsed.password is None
                 and parsed.path.startswith("/devtools/page/"))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ClientCheckError("Steam returned an invalid local page address.")
    # Normalize the host, so even the advertised URL cannot leave loopback.
    address = "ws://127.0.0.1:8080" + parsed.path
    connection = None
    try:
        import websocket
        connection = websocket.create_connection(address, timeout=4, suppress_origin=True,
                                                   http_no_proxy=["localhost", "127.0.0.1", "::1"])
        connection.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
            "timeout": 3500}}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            raw = connection.recv()
            if len(raw) > 3 * MAX_PAGE:
                raise ValueError
            message = json.loads(raw)
            if message.get("id") != 1:
                continue
            result = message.get("result", {})
            if "error" in message or "exceptionDetails" in result:
                raise ValueError
            value = result.get("result", {}).get("value")
            if not isinstance(value, dict):
                raise ValueError
            return value
        raise ValueError
    except Exception:
        raise ClientCheckError("Could not read the matchmaking page from Steam. Retry after Steam finishes loading.") from None
    finally:
        if connection is not None:
            connection.close()


def _game_data_expression(steam_id):
    # Requests execute in Steam's own Community page and use its existing login.
    # Return only visible text/tables, excluding scripts and session identifiers.
    url = json.dumps(matchmaking_url(steam_id))
    return """(async () => {
        if (location.origin !== 'https://steamcommunity.com') return {state: 'unknown'};
        const response = await fetch(URL, {credentials:'include', cache:'no-store'});
        if (!response.ok) return {state:'unknown'};
        const raw = await response.text();
        if (raw.length > 2097152) return {state:'unknown'};
        const doc = new DOMParser().parseFromString(raw, 'text/html');
        const id = raw.match(/\\bg_steamID\\s*=\\s*['\"]([0-9]{17})['\"]/);
        const signedIn = id ? id[1] : null;
        if (signedIn !== STEAMID) return {url:response.url, signedIn, html:''};
        doc.querySelectorAll('script,style,input,form,template,noscript').forEach(node => node.remove());
        const escape = value => value.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        const text = escape(doc.body?.textContent || '');
        const tables = Array.from(doc.querySelectorAll('table')).map(table => {
            const clean = table.cloneNode(true);
            clean.querySelectorAll('script,style,input,form').forEach(node => node.remove());
            return clean.outerHTML;
        }).join('');
        return {url:response.url, signedIn,
            html:'<html><body><p>'+text+'</p>'+tables+'</body></html>'};
    })()""".replace("URL", url).replace("STEAMID", json.dumps(steam_id))


def _open_client_page(steam_id):
    installation = find_steam_dir()
    if not installation:
        raise ClientCheckError("Steam installation was not found.")
    executable = Path(installation) / "steam.exe"
    if not executable.is_file():
        raise ClientCheckError("Steam installation was not found.")
    try:
        subprocess.Popen([str(executable), "steam://openurl/" + matchmaking_url(steam_id)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise ClientCheckError("Could not open the matchmaking page in Steam.") from None


def check_client_cooldown(steam_id, *, cancel=None, timeout=25):
    """Check only the account logged into Steam; leave all login state alone."""
    matchmaking_url(steam_id)
    session = current_steam_session()
    if session is None:
        return _result("unknown", "Open Steam and log into this account to refresh its data.")
    if session[0] != steam_id:
        return _result("wrong_account", "A different account is logged into Steam. Saved data is unchanged.")
    deadline = time.monotonic() + timeout
    opened = False
    attempts = 0
    last = _result("unknown", "Steam has not provided readable matchmaking data yet.")
    try:
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return _result("cancelled", "Check cancelled. Saved data is unchanged.")
            if current_steam_session() != session:
                return _result("wrong_account", "Steam’s logged-in account changed. Saved data is unchanged.")
            try:
                targets = _client_targets()
            except ClientCheckError as exc:
                last = _result("unknown", str(exc))
                if cancel is not None:
                    cancel.wait(0.5)
                else:
                    time.sleep(0.5)
                continue
            for target in targets:
                address = urlsplit(target.get("url", ""))
                if address.scheme != "https" or address.netloc != "steamcommunity.com" or "/login" in address.path:
                    continue
                try:
                    attempts += 1
                    snapshot = _evaluate(target, _game_data_expression(steam_id))
                except ClientCheckError as exc:
                    last = _result("unknown", str(exc))
                    if attempts >= 3:
                        return last
                    continue
                last = parse_matchmaking_page(snapshot.get("html"), snapshot.get("url", ""), steam_id,
                                              snapshot.get("signedIn"))
                if last["state"] in ("active", "clear"):
                    if current_steam_session() != session:
                        return _result("wrong_account", "Steam’s logged-in account changed. Saved data is unchanged.")
                    return last
                if attempts >= 3:
                    return last
                break
            if not opened:
                _open_client_page(steam_id)
                opened = True
            if cancel is not None:
                cancel.wait(2 if attempts else 0.5)
            else:
                time.sleep(2 if attempts else 0.5)
    except ClientCheckError as exc:
        return _result("unknown", str(exc))
    return last


def matchmaking_url(steam_id):
    if not isinstance(steam_id, str) or not re.fullmatch(r"[0-9]{17}", steam_id):
        raise ValueError("Select an account with a valid SteamID.")
    return f"https://steamcommunity.com/profiles/{steam_id}/gcpd/730/?tab=matchmaking&l=english"


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.text = [], []
        self.table = self.row = self.cell = None
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, text):
        if not self.hidden:
            self.text.append(text)
            if self.cell is not None:
                self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def _result(state, message):
    return {"state": state, "message": message}


def parse_matchmaking_page(html, url, steam_id, signed_in_id=None, *, now=None):
    """Fail closed: unreadable, signed-out, or wrong-account pages are unknown."""
    expected = urlsplit(matchmaking_url(steam_id))
    address = urlsplit(url)
    if address.scheme != "https" or address.netloc != expected.netloc:
        return _result("unknown", "Open the selected account’s Steam matchmaking page.")
    if "/login" in address.path:
        return _result("sign_in", "Sign in to the selected account in the Steam window.")
    if address.path.rstrip("/") != expected.path.rstrip("/") or parse_qs(address.query).get("tab") != ["matchmaking"]:
        return _result("unknown", "Open the selected account’s matchmaking tab in Steam.")
    if not isinstance(html, str) or len(html) > MAX_PAGE or "</html>" not in html.lower():
        return _result("unknown", "Steam’s page is incomplete. Retry the check.")
    identity = re.search(r"\bg_steamID\s*=\s*['\"]([0-9]{17})['\"]", html)
    signed_in_id = signed_in_id or (identity[1] if identity else None)
    if re.search(r"\bg_steamID\s*=\s*false\b", html):
        return _result("sign_in", "Sign in to the selected account in the Steam window.")
    if signed_in_id and signed_in_id != steam_id:
        return _result("wrong_account", "Steam is signed in to another account. Switch to the selected account.")
    if signed_in_id != steam_id:
        return _result("unknown", "Could not confirm which account is signed in to Steam.")
    parser = _Tables()
    try:
        parser.feed(html)
    except (ValueError, RecursionError):
        return _result("unknown", "Could not read Steam’s matchmaking data.")
    text = " ".join(parser.text).lower()
    if parser.table is not None or parser.row is not None or parser.cell is not None:
        return _result("unknown", "Steam’s page is incomplete. Retry the check.")
    if "personal game data" not in text or not parser.tables:
        return _result("unknown", "Steam has not provided readable matchmaking data.")
    now = datetime.now(timezone.utc) if now is None else now
    detected, expiries, unparsed = False, [], False
    for table in parser.tables:
        if not table:
            continue
        header = table[0]
        columns = [i for i, cell in enumerate(header) if "cooldown" in cell.lower() and
                   any(word in cell.lower() for word in ("expiration", "expiry", "expires"))]
        if not columns:
            continue
        detected = True
        for row in table[1:]:
            for index in columns:
                if index >= len(row):
                    unparsed = True
                    continue
                value = row[index].strip()
                if value.lower() in ("", "none", "n/a", "no cooldown", "not applicable"):
                    continue
                expiry = None
                for fmt in ("%Y-%m-%d %H:%M:%S GMT", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M GMT",
                            "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        expiry = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        pass
                if expiry is None:
                    unparsed = True
                elif expiry > now:
                    expiries.append(expiry)
    if expiries:
        expiry = max(expiries)
        return {"state": "active", "message": f"Active — expires {expiry.strftime('%Y-%m-%d %H:%M UTC')}",
                "expires_at": int(expiry.timestamp())}
    if unparsed or (not detected and "matchmaking cooldown" in text):
        return _result("active", "Matchmaking cooldown reported; expiry unavailable.")
    recognized = detected or any("matchmaking mode" in " ".join(table[0]).lower()
                                for table in parser.tables if table)
    if not recognized:
        return _result("unknown", "Steam’s matchmaking page format was not recognized.")
    return _result("clear", "No active matchmaking cooldown")
