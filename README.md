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
| [`extractor.py`](extractor.py) | The core logic: read bytes, find & validate the JWT. ~120 lines, no dependencies. |
| [`app.py`](app.py) | The drag-and-drop GUI (tkinter). |
| [`extract.py`](extract.py) | A command-line version. |
| [`tests/test_extractor.py`](tests/test_extractor.py) | Tests, using a **synthetic** token (no real data). |
| [`build.ps1`](build.ps1) | Builds the standalone `.exe` with PyInstaller. |
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

## Command line

Prefer a terminal? `extract.py` prints the string for any file:

```bash
python extract.py ChadGreen.exe
# -> ChadGreen----eyJ0eXAiOiJKV1Qi...
```

## Building a standalone .exe (to share with friends)

This produces a single `dist\JWTractor.exe` that needs **nothing installed**
on the other machine. From this folder, in PowerShell:

```powershell
.\build.ps1
```

If PowerShell blocks the script, either run the underlying command directly:

```powershell
pip install pyinstaller tkinterdnd2
python -m PyInstaller --noconfirm --onefile --windowed --name JWTractor --collect-all tkinterdnd2 app.py
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
