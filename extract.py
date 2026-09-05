"""Command-line front-end for JWTractor.

    python extract.py ChadGreen.exe
    -> ChadGreen----eyJ0eXAiOiJKV1Qi...

Handles several files at once and can also show every token found or decode the
JWT for inspection:

    python extract.py a.exe b.exe          # one "name----token" line each
    python extract.py --all game.exe       # every distinct token in the file
    python extract.py --decode game.exe    # + pretty-printed header/payload
    python extract.py --token-only a.exe   # just the token, no "name----" prefix

Exit codes: 0 on success, 1 if a file contained no token, 2 on a usage or
file-read error. With several files the highest (worst) code is returned.
"""

from __future__ import annotations

import argparse
import json
import sys

from extractor import (
    DEFAULT_SEPARATOR,
    ExtractionError,
    __version__,
    decode_token,
    extract_from_file,
    summarize_claims,
)
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract.py",
        description="Extract the JWT embedded in a file and print "
        "'<name>----<token>'.",
    )
    parser.add_argument(
        "files", nargs="*", metavar="FILE", help="one or more files to read"
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="print every distinct token found, not just the first",
    )
    parser.add_argument(
        "-d",
        "--decode",
        action="store_true",
        help="also print the decoded JWT header and payload as JSON",
    )
    parser.add_argument(
        "-t",
        "--token-only",
        action="store_true",
        help="print only the token(s), without the '<name>----' prefix",
    )
    parser.add_argument(
        "-s",
        "--separator",
        default=DEFAULT_SEPARATOR,
        metavar="SEP",
        help=f"separator between name and token (default: {DEFAULT_SEPARATOR!r})",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"JWTractor {__version__}",
    )

    # Steam config backup/restore CLI options are intentionally disabled
    # pending redesign.

    return parser


def _process_one(path: str, args: argparse.Namespace) -> int:
    """Print results for one file. Returns its exit code (0/1/2)."""
    try:
        result = extract_from_file(path)
    except ExtractionError as exc:
        print(f"error: {path}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: couldn't read '{path}': {exc}", file=sys.stderr)
        return 2

    name = result["username"]
    tokens = result["all_tokens"] if args.all else [result["token"]]

    for token in tokens:
        line = token if args.token_only else f"{name}{args.separator}{token}"
        print(line)

    if args.decode:
        for token in tokens:
            decoded = decode_token(token)
            print(f"# {name}: decoded JWT (signature NOT verified)")
            print(json.dumps(decoded, indent=2, ensure_ascii=False))
            for label, value in summarize_claims(decoded["payload"]):
                print(f"#   {label}: {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.files:
        _build_parser().error("the following arguments are required: FILE")

    worst = 0
    for path in args.files:
        worst = max(worst, _process_one(path, args))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
