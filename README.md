# JWTractor

A tiny, transparent tool that reads the JWT embedded in an `.exe` and prints
it prefixed with the file's name — no decompiling, it just pulls out a token
that's already stored as plain text in the file:

```
ChadGreen----eyJ0eXAiOiJKV1QiLCJhbGciOiJFZERTQSJ9.eyJpc3Mi...
```

The result is **auto-copied to your clipboard**, so you can paste it straight
away.

On Windows, click **Log in to Steam** to use the extracted account directly,
or select a saved account first. The login workflow is built into the Python
app and standalone `.exe`; Node.js and SteamNFATool are not required.

## How it works

Use the compact **Settings** button in the upper-left corner to manage login
options. **Default settings** apply to accounts that inherit them; **Selected
account** lets you override individual options for that account. Changes are
saved automatically. **Use defaults for this account** removes its overrides.
Click **← Accounts** to return to extraction, saved accounts, and login
actions. **Ctrl+Tab** switches views; the corner button also supports Enter,
Space, and Left/Right.

JWTractor reopens your last selected account with its locally cached cooldown
data. Reopening the app does not copy a token or start Steam login. The selected
account and cooldown appear above the visible token; **Show token details**
expands the decoded claims when you need them. The import area becomes compact
once an account is selected, and empty token panels stay out of the way.

The **Next login** summary above the login button shows the selected account's
effective options, including overrides. Settings stay in the upper-left corner.
The window can be resized; cards, text, and buttons adapt to its width, while
long content scrolls and the action buttons remain visible. Your chosen window
size is retained when switching tabs during the session.

File imports run in the background. Browse for several executables, drop them
together, or pass their paths to `app.py` to import a queue of up to 2,000 files.
A per-file report shows added accounts, refreshed tokens, and failures; one
failed file does not stop the queue. Completed results clear when you select
an account or take another action. **Cancel import** retains completed saves
and discards pending results. A failed import preserves the selected account,
its token, and cached data. Account search keeps your query when you rename or
delete an account.

The account token is visible in the result card. **Copy** copies the full
`username----token` string. A clipboard failure leaves the account saved and
offers a retry instead of reporting a successful copy.

| Shortcut | Action |
| --- | --- |
| **Ctrl+O** | Browse for an executable. |
| **Ctrl+K** | Open saved accounts and focus search. |
| **Ctrl+V** | Open the paste dialog with clipboard text; normal paste inside text fields. |
| **Ctrl+C** | Copy the account result; normal copy inside text fields. |
| **Ctrl+Tab** | Switch between Accounts and Settings. |
| **Escape** | Close the account picker, cancel a running import/login, or return from Settings. |
| **Ctrl+Q** | Close the app, waiting for login cleanup when necessary. |

Under **Settings → CS2 launch options**, enter your custom options and turn on
**Use custom launch options**. The text is saved when you leave the field or
press Enter, and is applied to the selected account before Steam starts on
each JWTractor login. Quotes and other arguments are preserved as entered.
It replaces CS2's existing launch options; an enabled empty field clears them.
The toggle is off by default and leaves Steam's current options alone when off.
Unsaved edits are marked in Settings. **Revert changes** restores the last
saved text; a validation or save error brings the field into view so you can
correct it before leaving Settings or starting a login.
These changes share the login backups and rollback and preserve other games
and accounts. CS2 itself is not launched automatically. You can check the result
in Steam's **CS2 → Properties → General → Launch Options**, as described in
[Steam's launch-options guide](https://help.steampowered.com/en/faqs/view/7D01-D2DD-D75E-2955).

**Disable Steam Cloud Sync** under **Settings → Login options** is on by default to help
with crashes when Cloud sync is enabled. JWTractor remembers this option and
sets the selected account's global `cloudenabled` preference to `0` in
`userdata/<account-id>/7/remote/sharedconfig.vdf` before launching Steam.
An existing legacy `config/sharedconfig.vdf` copy is updated too. These files
use the login transaction's backups and rollback; other accounts, per-game
preferences, and save files are preserved. This applies on each JWTractor login.
Turning the toggle off leaves Steam's current setting alone; re-enable syncing
in Steam **Settings → Cloud** when wanted. While disabled, game saves do not
sync between devices. This config change is tested with synthetic accounts;
confirm Cloud is off in your client to verify the crash workaround.
Steam documents the account-wide setting in its
[Steam Cloud documentation](https://partner.steamgames.com/doc/features/cloud).

Turn on **Start Steam w/ Remote Play & Friends offline** under **Settings → Login options** to
start the selected account signed out of Friends & Chat with Remote Play
disabled. Automatic Friends & Chat sign-in is disabled for that account, with
the desired and cached friends status set to Offline. Steam stays connected
so games, downloads, and the store still work.
The default is off. Enable it in **Default settings** for accounts that inherit
the option, or in **Selected account** for just that account. JWTractor remembers
both defaults and overrides between launches.

These settings are merged into the selected account's `localconfig.vdf` before
Steam starts, with the same backups and rollback as the login configuration.
Other accounts' settings are preserved. Turning the toggle off leaves Steam's
current settings alone; use Steam to change them back. A valid token is still
required, and live verification of these preferences is pending a working token.

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
| [`steam_login.py`](steam_login.py) | Windows Steam login, DPAPI encryption, account preservation, backups and rollback. Uses the Python standard library. |
| [`steam.py`](steam.py) | Steam installation detection and separate backup/restore helpers. |
| [`extract.py`](extract.py) | A command-line version (batch, `--decode`, `--all`, `--token-only`, …). |
| [`tests/test_extractor.py`](tests/test_extractor.py) | Core tests, using a **synthetic** token (no real data). |
| [`tests/test_cli.py`](tests/test_cli.py) | Command-line tests, also using the synthetic token. |
| [`tests/test_store.py`](tests/test_store.py) | Saved-account store tests (temp files, synthetic tokens). |
| [`tests/test_steam_login.py`](tests/test_steam_login.py) | Login workflow and account-preservation tests using temporary files and synthetic tokens. |
| [`tests/test_app_login.py`](tests/test_app_login.py) | Extraction, selection, login and cancellation UI tests with a mocked Steam backend. |
| [`cooldown.py`](cooldown.py) | Reads matchmaking data through the signed-in Steam client and validates the account. |
| [`presence.py`](presence.py) | Checks public Steam Friends status without credentials and keeps unavailable results distinct from Offline. |
| [`diagnostics.py`](diagnostics.py) | Records a limited set of local error events and exports no tokens or account details. |
| [`build.ps1`](build.ps1) | Builds the standalone `.exe` with PyInstaller. |
| [`make_icon.py`](make_icon.py) | Build-time helper that draws the app/exe icon (`icon.ico`). |
| [`requirements.txt`](requirements.txt) | Drag-and-drop and Steam page-interface dependencies. |

Extraction only **reads** the file you give it. It never modifies, uploads,
or executes that input. The optional Steam login action updates local Steam
configuration and launches the installed Steam client. The optional cooldown
check reads Steam Community through the already signed-in Steam client. Public
online-status checks make anonymous HTTPS requests to Steam Community using
the selected SteamID, without sending tokens or cookies.

## Quick start (from source)

Requires **Python 3.9+**.

```bash
git clone https://github.com/CelestialCoderWaves/JWTractor.git
cd JWTractor
pip install -r requirements.txt
python app.py
```

Then drag an `.exe` onto the window, click **Browse**, or click **Paste token**
to add a token that has already been extracted.

> `tkinterdnd2` is optional. Without it the window still works via the
> **Browse** button and by dropping a file onto the app's icon; with it you
> also get drag-and-drop directly into the window.

## Saved accounts

Account details show **This PC** sign-in status separately from **Public Friends
status**. The toolbar identifies the account currently signed into the local
Steam client, and saved-account rows mark it. Local sign-in does not establish
that Steam is connected to the internet.

Public status distinguishes **Online**, **In game**, and **Appears offline**,
with the last check's age. It refreshes automatically for the selected account
about once a minute; **Refresh online status** requests an immediate check.
Results are kept in memory, and older results are marked stale. Rate limits,
private profiles, and unreadable responses produce an unavailable result rather
than claiming the account is Offline. Steam's Invisible status can appear
Offline. This uses Steam's documented legacy profile XML interface, which is
deprecated and may be limited by privacy settings; see
[Steam Community data documentation](https://partner.steamgames.com/documentation/community_data).

Account **Details → CS2 matchmaking cooldown** reads the personal matchmaking
page using the account already logged into the Windows Steam client. No separate
browser sign-in is needed. A successful check shows an active cooldown and its
expiry, or **No active matchmaking cooldown**. Signed-out, wrong-account, and
unreadable pages cannot produce a clean result.

JWTractor automatically refreshes after it confirms a Steam login and when it
detects a saved account starting or switching in Steam while JWTractor is open.
**Refresh data** also checks manually. If Steam has no Community page open,
the check opens the matchmaking page in Steam’s own window. This may change
the page displayed in Steam; it does not change the account or launch CS2.

Steam must expose its local CEF page interface. JWTractor supplies
`-cef-enable-debugging` when launching Steam. For an already-running client,
log in through JWTractor once to restart it with that flag. The checker uses
only the local interface at `127.0.0.1:8080`, performs the read inside Steam’s
existing Community session, and does not access session-cookie databases.
Clients started elsewhere without the flag report that the interface is
unavailable, preserving the saved result.

Successful results and their **Last refreshed** UTC timestamp are saved locally
in the accounts file, keyed by SteamID. They remain available after restarting
JWTractor or adding a fresh token for the same account. Loading saved details
does not ask you to sign in again.
The displayed result is the last known status, not a live guarantee. An elapsed
cached expiry asks you to refresh; a failed or cancelled check preserves the
previous result. No password, web cookie, or page contents are saved by the
cooldown cache.

Every token you extract is **remembered**, so you can re-select an account you've
used before without hunting down the original `.exe`. Click **Saved accounts**
to open the list; each entry shows its name and issuer.

Search by alias, login name, issuer, or SteamID. Multiple search words narrow the
results together. **Down** moves from search into the results; **Up/Down** moves
between accounts and scrolls the focused row into view. **Enter** selects the
focused account (or the first match when searching). The list marks the selected
account and expired tokens, and long account names wrap within their row.
Renaming an account changes its display name without changing the login target.

Importing a fresh Steam token for an existing SteamID refreshes the saved account
instead of adding a duplicate. Its login name, alias, selection, account settings,
and cached cooldown remain intact. A token with an older expiry cannot replace
a newer saved token. Older duplicate records for the same account are consolidated
when a refresh succeeds.

- **Load** — click an account to put its `name----token` back in the box and copy it.
- **Rename** — give an account a friendly **alias** (e.g. "main" or "burner"), so a
  cryptic username like a spam phone number is easy to recognise. The alias
  becomes the display name; the real username still shows underneath.
- **Delete** — forget a saved token (this only removes it from JWTractor).

Accounts are stored as plain JSON at `%APPDATA%\JWTractor\accounts.json` on
Windows (`~/.config/JWTractor/accounts.json` elsewhere). **Treat that file as
sensitive** — it holds real tokens, which are credentials. Set the
`JWTRACTOR_STORE` environment variable to keep it somewhere else.

Before each saved-data update, JWTractor keeps the previous valid snapshot in
`accounts.json.bak`. Damaged or unsupported files are left intact and block writes
until recovered. Under **Settings → Saved data & diagnostics → Recover backup**,
choose a valid backup to restore accounts, defaults, overrides, and cached data.
The existing file is preserved as `accounts-preserved-*.json` before replacement.
These backups also contain credentials; removing saved data completely requires
removing its backups as well.

**Copy diagnostics** copies version information and recent local event names
and exception types. The rotating `diagnostics.log` beside the accounts file
excludes tokens, account names, SteamIDs, paths, exception messages, and page
contents. Nothing is uploaded automatically.

## Add an already extracted token

Click **Paste token** and paste either `username----token` or the JWT alone.
The Steam login name fills automatically from the text before `----`.
For a token alone, enter its actual Steam login name. Input is visible.
Click **Add account** (or press Ctrl+Enter) to save and select the account,
show its decoded claims, and copy the combined string. You can then click
**Log in to Steam**. Adding a token itself does not close or launch Steam.

Validation errors appear inside the dialog, allowing you to correct the input.
Re-adding the same token updates its saved entry and preserves its alias.
Buttons support Tab, Enter, and Space; Escape closes the paste dialog.

## Log in to Steam (Windows)

1. Extract an account, add it using **Paste token**, or select it from **Saved accounts**.
2. Click **Log in to Steam**. Steam closes normally, the account is saved,
   and the client restarts. Check Steam to confirm sign-in.

The filename must contain the actual Steam login name (`alice.exe` → `alice`),
not the profile's display name. Saved aliases are for display only; login uses
the original username, normalized to lowercase for Steam's credential cache.
Invalid, expired, web-only, and access tokens are rejected before Steam is
closed. Desktop login requires a Steam client refresh token. JWT claims are
checked locally; their signatures are not verified.

After launch, JWTractor watches new Steam connection-log entries for up to 30
seconds. It distinguishes confirmed sign-in to the selected account, a rejected
login, and Steam signing in to another account. If no result is available, it
asks you to check Steam. Rejection messages contain a known response description,
never raw log content or credentials. Cancellation during this check leaves the
already launched Steam client running.

A future expiry does not prove a token still works: Steam can revoke a session.
If Steam reports **Access denied**, confirm the session with the account owner
or obtain a fresh client refresh token. JWTractor cannot override that rejection.

The other accounts' saved credentials and remember-login settings are preserved.
The selected account's login fields, startup account selection, and enabled
login options change. Current Steam clients select an account with `AutoLogin`; older clients
use `MostRecent` and `AllowAutoLogin`. JWTractor follows the format already in
your file and uses `AutoLogin` for a new file. It also disables Steam's
**Ask which account to use each time Steam starts** preference at
`InstallConfigStore/WebStorage/Auth/AlwaysShowUserChooser` so the selected account
can sign in automatically. You can
turn that preference back on in Steam. Steam still decides whether each saved
session is valid. Preservation is
tested with synthetic accounts; real account sign-in requires client verification.

Login runs in the background. **Cancel login**, Escape, or closing the window
requests cancellation; the window waits for cleanup before exiting. Cancellation
during replacement attempts to restore the originals. Cancellation after saving
reports that state and prevents Steam from being launched.

Tokens are encrypted with Windows DPAPI before configuration changes; there is
no plaintext fallback. Each existing configuration file receives a unique `.bak`
copy beside it. Failed replacements trigger rollback without overwriting detected
external edits. Backups can contain credentials and are retained for recovery.
JWTractor and SteamNFATool share the same installation lock. A crash or failed
rollback can leave `config.vdf.steam-nfa.lock` in place: close Steam, confirm no
login is running, inspect the files and backups, and recover as necessary before
manually removing the lock. File updates, registry selection and client launch
are separate operations; forced termination can interrupt them.

The interface keeps its action buttons visible and scrolls the content when
token details exceed the available screen height.

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

This produces a single `dist\JWTractor.exe` that needs no Python installed
on the other machine. Cooldown checks use the installed Steam client.
From this folder, in PowerShell:

```powershell
.\build.ps1
```

If PowerShell blocks the script, either run the underlying command directly:

```powershell
pip install pyinstaller tkinterdnd2 pillow websocket-client
python make_icon.py
python make_toggle_graphics.py
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
