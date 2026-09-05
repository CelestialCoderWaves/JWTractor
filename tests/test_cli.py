"""Tests for the command-line front-end (``extract.py``).

These call ``extract.main()`` directly with an argv list and capture stdout so
the CLI's behaviour and exit codes are covered without spawning a subprocess.
The tokens here are synthetic — no real account data.
"""

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extract import main  # noqa: E402

SAMPLE_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJFZERTQSJ9."
    "eyJpc3MiOiJleGFtcGxlIiwic3ViIjoiMDAwMDAwMDAwMDAwMDAwMCIsImF1ZCI6WyJjbGll"
    "bnQiXSwiZXhwIjo5OTk5OTk5OTk5LCJub3RlIjoic3ludGhldGljLXRlc3QtdG9rZW4tbm8t"
    "cmVhbC1kYXRhIn0."
    "AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKKLLLLMMMMNNNN_-abcdEFGH"
)


def _mk_token(header, payload, sig="AAAABBBBCCCCDDDD"):
    def enc(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{enc(header)}.{enc(payload)}.{sig}"


def _fake_exe(tmp_path, name, *tokens):
    blob = b"MZ" + os.urandom(512)
    for token in tokens:
        blob += b"\x00" + token.encode() + b"\x00" + os.urandom(256)
    path = tmp_path / name
    path.write_bytes(blob)
    return str(path)


def test_default_prints_combined(tmp_path, capsys):
    path = _fake_exe(tmp_path, "ChadGreen.exe", SAMPLE_TOKEN)
    assert main([path]) == 0
    assert capsys.readouterr().out.strip() == f"ChadGreen----{SAMPLE_TOKEN}"


def test_token_only(tmp_path, capsys):
    path = _fake_exe(tmp_path, "ChadGreen.exe", SAMPLE_TOKEN)
    assert main(["--token-only", path]) == 0
    assert capsys.readouterr().out.strip() == SAMPLE_TOKEN


def test_custom_separator(tmp_path, capsys):
    path = _fake_exe(tmp_path, "ChadGreen.exe", SAMPLE_TOKEN)
    assert main(["--separator", "::", path]) == 0
    assert capsys.readouterr().out.strip() == f"ChadGreen::{SAMPLE_TOKEN}"


def test_all_prints_every_token(tmp_path, capsys):
    second = _mk_token({"alg": "none"}, {"iss": "second"})
    path = _fake_exe(tmp_path, "Multi.exe", SAMPLE_TOKEN, second)
    assert main(["--all", path]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [f"Multi----{SAMPLE_TOKEN}", f"Multi----{second}"]


def test_decode_shows_header_and_claims(tmp_path, capsys):
    path = _fake_exe(tmp_path, "ChadGreen.exe", SAMPLE_TOKEN)
    assert main(["--decode", path]) == 0
    out = capsys.readouterr().out
    assert '"alg": "EdDSA"' in out
    assert "Issuer: example" in out


def test_missing_file_returns_2(tmp_path, capsys):
    assert main([str(tmp_path / "nope.exe")]) == 2
    assert "couldn't read" in capsys.readouterr().err


def test_no_token_returns_1(tmp_path, capsys):
    path = tmp_path / "empty.exe"
    path.write_bytes(b"MZ" + os.urandom(300))
    assert main([str(path)]) == 1
    assert "No embedded token" in capsys.readouterr().err


def test_multiple_files_return_worst_code(tmp_path, capsys):
    good = _fake_exe(tmp_path, "Good.exe", SAMPLE_TOKEN)
    missing = str(tmp_path / "gone.exe")
    # One good (0) + one unreadable (2) -> worst code wins.
    assert main([good, missing]) == 2
    assert f"Good----{SAMPLE_TOKEN}" in capsys.readouterr().out


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "JWTractor" in capsys.readouterr().out


def test_no_files_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
