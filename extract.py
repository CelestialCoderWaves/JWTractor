"""Command-line front-end for JWTractor.

    python extract.py ChadGreen.exe
    -> ChadGreen----eyAidHlwIjogIkpXVCIs...

Exits 0 on success, 1 if no token was found, 2 on a file/usage error.
"""

from __future__ import annotations

import sys

from extractor import ExtractionError, extract_from_file


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        prog = "extract.py"
        print(f"usage: python {prog} <file.exe>", file=sys.stderr)
        return 2

    path = argv[0]
    try:
        result = extract_from_file(path)
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: couldn't read '{path}': {exc}", file=sys.stderr)
        return 2

    print(result["combined"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
