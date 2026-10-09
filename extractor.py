"""Core extraction logic for JWTractor.

The interesting string embedded in these EXEs is a JWT — three base64url
segments separated by dots (``header.payload.signature``). It is stored in the
binary as a plain, null-terminated ASCII string (not encrypted or obfuscated),
so we don't need IDA or any disassembler at runtime: we just read the file's
raw bytes and pattern-match the token.

This module is deliberately dependency-free and importable on its own so it can
be unit-tested and reused from a CLI, the GUI, or anything else.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re

__all__ = [
    "ExtractionError",
    "find_tokens",
    "username_from_path",
    "extract_from_file",
    "parse_token_input",
    "decode_token",
    "summarize_claims",
    "invalidation_notes",
    "DEFAULT_SEPARATOR",
    "__version__",
]

__version__ = "0.3.4"

DEFAULT_SEPARATOR = "----"

# A JWT is <base64url>.<base64url>.<base64url>. The header segment is the
# base64url encoding of a JSON object, which always begins with "{", so the
# encoded form always starts with "ey". base64url alphabet is A-Z a-z 0-9 - _
# (no padding). We require a reasonable minimum length per segment to avoid
# matching short unrelated tokens.
_JWT_RE = re.compile(
    rb"ey[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"
)


class ExtractionError(Exception):
    """Raised when no embedded token can be found in a file."""


def _b64url_decode(segment: bytes) -> bytes:
    """Decode a base64url segment, restoring any stripped padding."""
    padding = (-len(segment)) % 4
    return base64.urlsafe_b64decode(segment + b"=" * padding)


def _looks_like_jwt(token: bytes) -> bool:
    """Confirm a candidate is a real JWT by decoding its header to JSON.

    This filters out the rare chance of a random ``ey...`.` byte sequence that
    happens to match the shape but isn't actually a token.
    """
    try:
        header_segment = token.split(b".", 1)[0]
        header = json.loads(_b64url_decode(header_segment))
    except Exception:
        return False
    if not isinstance(header, dict):
        return False
    # Every JWT header carries "alg"; "typ" is near-universal too.
    return "alg" in header or "typ" in header


def find_tokens(data: bytes) -> list[str]:
    """Return every distinct JWT found in ``data``, in order of appearance."""
    tokens: list[str] = []
    seen: set[bytes] = set()
    for match in _JWT_RE.finditer(data):
        candidate = match.group()
        if candidate in seen:
            continue
        if _looks_like_jwt(candidate):
            seen.add(candidate)
            tokens.append(candidate.decode("ascii"))
    return tokens


def decode_token(token: str) -> dict:
    """Decode a JWT's header and payload into Python objects.

    The signature is **not** verified — this only base64url-decodes the two JSON
    segments so the caller can inspect what the token claims. Either value is
    ``None`` if that segment isn't valid JSON (rare, but possible for the
    non-standard payloads some tools emit).

    Raises ``ExtractionError`` if ``token`` isn't shaped like a JWT at all.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ExtractionError("Not a well-formed JWT (expected three segments).")

    def _segment(raw: str):
        try:
            return json.loads(_b64url_decode(raw.encode("ascii")))
        except Exception:
            return None

    return {"header": _segment(parts[0]), "payload": _segment(parts[1])}


# Common registered JWT claims (RFC 7519) plus Steam-specific fields,
# in a sensible display order.
_CLAIM_LABELS = (
    ("iss", "Issuer"),
    ("sub", "Subject"),
    ("aud", "Audience"),
    ("iat", "Issued"),
    ("nbf", "Not before"),
    ("exp", "Expires"),
    ("jti", "Token ID"),
    ("oat", "Original auth"),
    ("per", "Permissions"),
    # ("ip_subject", "IP (subject)"),
    # ("ip_confirmer", "IP (confirmer)"),
)
_TIME_CLAIMS = {"iat", "nbf", "exp", "oat"}


def _format_timestamp(value) -> str:
    """Render a NumericDate claim as a readable UTC string (``value`` on failure)."""
    try:
        moment = datetime.datetime.fromtimestamp(
            int(value), tz=datetime.timezone.utc
        )
    except (ValueError, TypeError, OverflowError, OSError):
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _time_delta_text(seconds: float) -> str:
    """Human-friendly delta like '3d 12h' or '7mo 2d'."""
    s = abs(seconds)
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h {int((s % 3600) // 60)}m"
    days = int(s // 86400)
    if days < 60:
        return f"{days}d {int((s % 86400) // 3600)}h"
    months = days // 30
    remaining_days = days % 30
    return f"{months}mo {remaining_days}d"


def summarize_claims(payload) -> list[tuple[str, str]]:
    """Return ``(label, value)`` rows for the common claims in ``payload``.

    Timestamps are rendered as UTC; ``exp`` is annotated with how long until the
    token expires (or how long ago it expired). Returns an empty list if
    ``payload`` isn't a dict of claims. Intended for a compact, human-readable
    preview (GUI panel or CLI summary).
    """
    if not isinstance(payload, dict):
        return []

    now = datetime.datetime.now(tz=datetime.timezone.utc).timestamp()
    rows: list[tuple[str, str]] = []
    for key, label in _CLAIM_LABELS:
        if key not in payload:
            continue
        value = payload[key]
        if key in _TIME_CLAIMS:
            text = _format_timestamp(value)
            if key == "exp":
                try:
                    exp_ts = int(value)
                    delta = exp_ts - now
                    if delta < 0:
                        text += f"  (expired {_time_delta_text(delta)} ago)"
                    else:
                        text += f"  (in {_time_delta_text(delta)})"
                except (ValueError, TypeError):
                    pass
            value = text
        elif isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        rows.append((label, str(value)))

    # rows.extend(invalidation_notes(payload, now))
    return rows


def invalidation_notes(payload, now: float | None = None) -> list[tuple[str, str]]:
    """Extra rows warning about non-exp invalidation scenarios.

    A Steam refresh token can be revoked server-side even when ``exp`` hasn't
    passed — e.g. after a password change or "deauthorize all devices". These
    notes surface that context so the user doesn't assume "not expired" means
    "still works".
    """
    if not isinstance(payload, dict):
        return []
    if now is None:
        now = datetime.datetime.now(tz=datetime.timezone.utc).timestamp()

    notes: list[tuple[str, str]] = []
    iss = payload.get("iss", "")
    exp = payload.get("exp")
    oat = payload.get("oat")

    if str(iss).lower() == "steam":
        try:
            exp_ts = int(exp)
        except (TypeError, ValueError):
            exp_ts = None

        if exp_ts is not None and exp_ts > now:
            notes.append(("Status", "NOT EXPIRED — but may be revoked (see below)"))
        elif exp_ts is not None:
            notes.append(("Status", "EXPIRED"))

        reasons = []
        reasons.append("Password changed on the account")
        reasons.append("\"Deauthorize all devices\" used in Steam settings")
        reasons.append("Steam Guard method changed")
        notes.append(("Revoked if", " / ".join(reasons)))

        if oat is not None:
            notes.append(
                ("Note", "Tokens issued before a password change are "
                 "rejected regardless of exp")
            )

    return notes


def username_from_path(path: str | os.PathLike) -> str:
    """The file's name without its extension (``ChadGreen.exe`` -> ``ChadGreen``)."""
    return os.path.splitext(os.path.basename(os.fspath(path)))[0]


def parse_token_input(text: str, username: str = "") -> dict:
    """Accept one pasted JWT, or username----JWT, without reading a file.

    Like file extraction, this checks the JWT header but does not verify its
    signature or require an unexpired Steam session merely to inspect/save it.
    """
    if not isinstance(text, str) or not text.strip():
        raise ExtractionError("Paste a token first.")
    if len(text) > 64 * 1024:
        raise ExtractionError("Paste one account at a time (maximum 64 KiB).")
    if not isinstance(username, str):
        raise ExtractionError("Enter the Steam login name.")
    def clean(value):
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1].strip()
        return value
    def candidate(value):
        token = re.sub(r"\s+", "", clean(value))
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            return None
        return token if _looks_like_jwt(token.encode("ascii")) else None
    text, username = clean(text), clean(username)
    token, pasted_name = None, ""
    # Overlapping delimiters allow login names which themselves contain hyphens.
    separator = text.find(DEFAULT_SEPARATOR)
    while 0 <= separator <= 64:
        possible = candidate(text[separator + len(DEFAULT_SEPARATOR):])
        if possible:
            pasted_name, token = text[:separator].strip(), possible
            if username and pasted_name and username != pasted_name:
                raise ExtractionError("The login name does not match the username in the pasted text. Leave the name field blank or use the same name.")
            break
        separator = text.find(DEFAULT_SEPARATOR, separator + 1)
    token = token or candidate(text)
    if not token:
        raise ExtractionError("Paste one complete JWT token or username----token.")
    username = username or pasted_name
    if not username:
        raise ExtractionError("Enter the Steam login name for this token.")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", username):
        raise ExtractionError("Use a Steam login name with letters, numbers, _, -, . or @ (up to 64 characters).")
    decoded = decode_token(token)
    return {"username": username, "token": token,
            "combined": f"{username}{DEFAULT_SEPARATOR}{token}", "all_tokens": [token],
            "header": decoded["header"], "payload": decoded["payload"]}


def extract_from_file(
    path: str | os.PathLike, separator: str = DEFAULT_SEPARATOR
) -> dict:
    """Read ``path``, find the embedded token, and build the output string.

    Returns a dict with keys:
      - ``username``:   file name without extension
      - ``token``:      the first valid JWT found
      - ``combined``:   ``f"{username}{separator}{token}"`` (the thing you want)
      - ``all_tokens``: every distinct JWT found (usually just one)
      - ``header``:     the first token's decoded header (dict, or ``None``)
      - ``payload``:    the first token's decoded payload (dict, or ``None``)

    Raises ``ExtractionError`` if no token is present, and the usual OS errors
    (``FileNotFoundError`` etc.) if the file can't be read.
    """
    with open(path, "rb") as handle:
        data = handle.read()

    tokens = find_tokens(data)
    if not tokens:
        raise ExtractionError(
            "No embedded token (JWT) was found in this file."
        )

    username = username_from_path(path)
    token = tokens[0]
    decoded = decode_token(token)
    return {
        "username": username,
        "token": token,
        "combined": f"{username}{separator}{token}",
        "all_tokens": tokens,
        "header": decoded["header"],
        "payload": decoded["payload"],
    }
