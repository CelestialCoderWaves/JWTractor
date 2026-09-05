# JWTractor

A tiny, transparent tool that reads the JWT embedded in an `.exe` and prints
it prefixed with the file's name — no decompiling, it just pulls out a token
that's already stored as plain text in the file:

```
ChadGreen----eyJ0eXAiOiJKV1QiLCJhbGciOiJFZERTQSJ9.eyJpc3Mi...
```

The result is **auto-copied to your clipboard**, so you can paste it straight
away.

## How it works

The embedded string is a **JWT** — three base64url chunks separated by dots
(`header.payload.signature`). In these executables it's stored as plain ASCII
text, so there's no real "decompilation" or reverse-engineering needed: the app
reads the file's raw bytes, pattern-matches the token, and then *validates* it
by decoding the header to JSON. That's why it's fast, has no heavy
dependencies, and doesn't need IDA or any disassembler.

The username is simply the file name without its extension
(`ChadGreen.exe` → `ChadGreen`), joined to the token with `----`.

## Is it safe? (please verify)

Everything here is a small amount of plain, readable Python — no binaries, no
obfuscation, nothing hidden. You are encouraged to read every file before
running it:

| File | What it does |
| --- | --- |
| [`extractor.py`](extractor.py) | The core logic: read bytes, find, validate & decode the JWT. No dependencies. |
| [`app.py`](app.py) | The drag-and-drop GUI (tkinter), with a decoded-claims preview and saved accounts. |
| [`store.py`](store.py) | Saves extracted tokens + your aliases to a local JSON file. No dependencies. |
| [`extract.py`](extract.py) | A command-line version (batch, `--decode`, `--all`, `--token-only`, …). |
| [`tests/test_extractor.py`](tests/test_extractor.py) | Core tests, using a **synthetic** token (no real data). |
| [`tests/test_cli.py`](tests/test_cli.py) | Command-line tests, also using the synthetic token. |
| [`tests/test_store.py`](tests/test_store.py) | Saved-account store tests (temp files, synthetic tokens). |
| [`build.ps1`](build.ps1) | Builds the standalone `.exe` with PyInstaller. |
| [`make_icon.py`](make_icon.py) | Build-time helper that draws the app/exe icon (`icon.ico`). |
| [`requirements.txt`](requirements.txt) | The one optional dependency (`tkinterdnd2`). |

The tool only ever **reads** the file you give it. It never modifies, uploads,
or executes the input, and it makes **no network connections** of any kind.

## Quick start (from source)

Requires **Python 3.9+**.

```bash
git clone https://github.com/CelestialCoderWaves/JWTractor.git
cd JWTractor
pip install -r requirements.txt
python app.py
```

Then drag an `.exe` onto the window, or click **Browse**.

> `tkinterdnd2` is optional. Without it the window still works via the
> **Browse** button and by dropping a file onto the app's icon; with it you
> also get drag-and-drop directly into the window.

## Saved accounts

Every token you extract is **remembered**, so you can re-select an account you've
used before without hunting down the original `.exe`. Click **Saved accounts**
to open the list; each entry shows its name and issuer.

- **Load** — click an account to put its `name----token` back in the box and copy it.
- **Rename** — give an account a friendly **alias** (e.g. "main" or "burner"), so a
  cryptic username like a spam phone number is easy to recognise. The alias
  becomes the display name; the real username still shows underneath.
- **Delete** — forget a saved token (this only removes it from JWTractor).

Accounts are stored as plain JSON at `%APPDATA%\JWTractor\accounts.json` on
Windows (`~/.config/JWTractor/accounts.json` elsewhere). **Treat that file as
sensitive** — it holds real tokens, which are credentials. Delete it to wipe all
saved accounts. Set the `JWTRACTOR_STORE` environment variable to keep it
somewhere else.

## Command line

Prefer a terminal? `extract.py` prints the string for any file:

```bash
python extract.py ChadGreen.exe
# -> ChadGreen----eyJ0eXAiOiJKV1Qi...
```

It also handles several files at once and can inspect the token:

```bash
python extract.py a.exe b.exe        # one "name----token" line per file
python extract.py --token-only a.exe # just the token, no "name----" prefix
python extract.py --all game.exe     # every distinct token found in the file
python extract.py --decode game.exe  # + pretty-printed header/payload + claims
python extract.py --separator :: a.exe   # use a custom separator
```

`--decode` shows what the token claims (issuer, subject, expiry, …) so you can
sanity-check it. The signature is **not** verified — this only base64url-decodes
the JSON, exactly like the site [jwt.io](https://jwt.io) does.

Exit codes: `0` success, `1` if a file had no token, `2` on a usage/read error
(with several files, the worst code wins). Run `python extract.py --help` for
the full list.

## Building a standalone .exe (to share with friends)

This produces a single `dist\JWTractor.exe` that needs **nothing installed**
on the other machine. From this folder, in PowerShell:

```powershell
.\build.ps1
```

If PowerShell blocks the script, either run the underlying command directly:

```powershell
pip install pyinstaller tkinterdnd2 pillow
python make_icon.py
python -m PyInstaller --noconfirm --onefile --windowed --name JWTractor --icon icon.ico --add-data "icon.ico;." --collect-all tkinterdnd2 app.py
```

...or allow the script for this one process: `powershell -ExecutionPolicy Bypass -File build.ps1`.

The built `.exe` is intentionally **not** committed to this repo (see
[`.gitignore`](.gitignore)) — publish it as a GitHub **Release** instead, so the
repository stays 100% readable source that anyone can audit.

### Using the built .exe (for friends — no install)

- **Double-click it**, then drag an `.exe` onto the window (or click *Browse*), or
- **Drag an `.exe` directly onto `JWTractor.exe`'s icon** in Explorer.

The string appears and is copied to your clipboard automatically.

> Note: an unsigned `.exe` may show a Windows SmartScreen "unknown publisher"
> warning. That's normal for hobby builds — click *More info → Run anyway*, or
> just run from source if you prefer.

## Tests

```bash
pip install pytest
pytest
```

## License

[MIT](LICENSE) — free to use, modify, and share.
