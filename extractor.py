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
import json
import os
import re

__all__ = [
    "ExtractionError",
    "find_tokens",
    "username_from_path",
    "extract_from_file",
    "DEFAULT_SEPARATOR",
]

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


def username_from_path(path: str | os.PathLike) -> str:
    """The file's name without its extension (``ChadGreen.exe`` -> ``ChadGreen``)."""
    return os.path.splitext(os.path.basename(os.fspath(path)))[0]


def extract_from_file(
    path: str | os.PathLike, separator: str = DEFAULT_SEPARATOR
) -> dict:
    """Read ``path``, find the embedded token, and build the output string.

    Returns a dict with keys:
      - ``username``:   file name without extension
      - ``token``:      the first valid JWT found
      - ``combined``:   ``f"{username}{separator}{token}"`` (the thing you want)
      - ``all_tokens``: every distinct JWT found (usually just one)

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
    return {
        "username": username,
        "token": token,
        "combined": f"{username}{separator}{token}",
        "all_tokens": tokens,
    }
