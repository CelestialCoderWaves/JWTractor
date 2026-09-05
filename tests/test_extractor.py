"""Tests for the core extractor.

The token used here is a *synthetic* JWT generated purely for testing — it
contains no real account data. It has the same shape as the real thing
(``header.payload.signature``, base64url) so it exercises the full extraction
and validation path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractor import (  # noqa: E402
    ExtractionError,
    decode_token,
    extract_from_file,
    find_tokens,
    summarize_claims,
    username_from_path,
)

# Synthetic JWT: header {"typ":"JWT","alg":"EdDSA"}, dummy payload, dummy sig.
SAMPLE_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJFZERTQSJ9."
    "eyJpc3MiOiJleGFtcGxlIiwic3ViIjoiMDAwMDAwMDAwMDAwMDAwMCIsImF1ZCI6WyJjbGll"
    "bnQiXSwiZXhwIjo5OTk5OTk5OTk5LCJub3RlIjoic3ludGhldGljLXRlc3QtdG9rZW4tbm8t"
    "cmVhbC1kYXRhIn0."
    "AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKKLLLLMMMMNNNN_-abcdEFGH"
)


def _fake_exe(tmp_path, name, token=SAMPLE_TOKEN):
    """Write junk bytes with the token embedded, mimicking a real .exe layout."""
    path = tmp_path / name
    blob = b"MZ" + os.urandom(4096) + b"\x00" + token.encode() + b"\x00" + os.urandom(2048)
    path.write_bytes(blob)
    return str(path)


def test_find_tokens_in_raw_bytes():
    data = b"garbage\x00" + SAMPLE_TOKEN.encode() + b"\x00garbage"
    assert find_tokens(data) == [SAMPLE_TOKEN]


def test_extract_combined_format(tmp_path):
    path = _fake_exe(tmp_path, "ChadGreen.exe")
    result = extract_from_file(path)
    assert result["token"] == SAMPLE_TOKEN
    assert result["username"] == "ChadGreen"
    assert result["combined"] == f"ChadGreen----{SAMPLE_TOKEN}"


def test_username_from_path():
    assert username_from_path("C:/x/ChadGreen.exe") == "ChadGreen"
    assert username_from_path("weird.name.exe") == "weird.name"


def test_no_token_raises(tmp_path):
    path = tmp_path / "empty.exe"
    path.write_bytes(b"MZ" + os.urandom(1000))
    try:
        extract_from_file(str(path))
    except ExtractionError:
        pass
    else:
        raise AssertionError("expected ExtractionError")


def test_ignores_non_jwt_lookalikes():
    # Starts with "ey" and has dots, but header is not valid JSON -> rejected.
    data = b"eyGARBAGE.notreal.stuffhere and eyXX.YY.ZZ"
    assert find_tokens(data) == []


def test_dedupes_repeated_token():
    data = SAMPLE_TOKEN.encode() + b"\x00" + SAMPLE_TOKEN.encode()
    assert find_tokens(data) == [SAMPLE_TOKEN]


def test_decode_token_returns_header_and_payload():
    decoded = decode_token(SAMPLE_TOKEN)
    assert decoded["header"] == {"typ": "JWT", "alg": "EdDSA"}
    assert decoded["payload"]["iss"] == "example"
    assert decoded["payload"]["aud"] == ["client"]


def test_decode_token_rejects_wrong_shape():
    try:
        decode_token("not.a-jwt")  # only two segments
    except ExtractionError:
        pass
    else:
        raise AssertionError("expected ExtractionError")


def test_decode_token_tolerates_non_json_payload():
    # Header is valid JSON ("{}"), payload segment is not JSON.
    token = "eyJhbGciOiJub25lIn0.Zm9vYmFy.sig"
    decoded = decode_token(token)
    assert decoded["header"] == {"alg": "none"}
    assert decoded["payload"] is None


def test_summarize_claims_labels_and_expiry():
    rows = dict(summarize_claims(decode_token(SAMPLE_TOKEN)["payload"]))
    assert rows["Issuer"] == "example"
    assert rows["Subject"] == "0000000000000000"
    assert rows["Audience"] == "client"  # list joined into a string
    assert "(in " in rows["Expires"]  # exp is far in the future


def test_summarize_claims_marks_expired():
    rows = dict(summarize_claims({"exp": 0}))  # 1970 -> long expired
    assert "(expired " in rows["Expires"]


def test_summarize_claims_ignores_non_dict():
    assert summarize_claims(None) == []
    assert summarize_claims("nope") == []


def test_extract_from_file_includes_decoded(tmp_path):
    path = _fake_exe(tmp_path, "ChadGreen.exe")
    result = extract_from_file(path)
    assert result["header"]["alg"] == "EdDSA"
    assert result["payload"]["iss"] == "example"
