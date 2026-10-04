# screecap

Simple Windows desktop recorder. DXGI Desktop Duplication for capture, ffmpeg for encoding. Ships with a CLI, a PyQt6 GUI, a browser view and a terminal view, all reading one append-only ledger of everything the program does.

## Transparency

screecap announces every action **before** it runs, verbatim, in a ledger file with a fixed six-column grammar:

```
ts | seq | kind | target | detail | src
2026-10-03T21:19:32.249Z | 3 | listen | http://127.0.0.1:50178/ | local API + browser view; ... | server.py:403
2026-10-03T21:19:32.251Z | 9 | exec | ffmpeg | ffmpeg -y -hide_banner -f rawvideo ... out.mp4 | recorder.py:153
2026-10-03T21:21:04.010Z | 10 | ok | #9 | rc=0, 5520 frames, 92.0s, 60.0 fps avg, 311.2 MB | recorder.py:153
```

`kind` is one of `exec` (a program it launches, full command line), `http` / `ws` (a call to the League client on localhost), `listen` (its own local port), `read` (credentials or the screen), `write` (a file, database row or registry key), `api` (something you clicked in the browser or typed in the terminal), `note`, and `ok` / `err` answering an earlier line by its `seq`. `src` is the file and line that did it, and the browser view turns it into the code. Nothing is paraphrased; the only thing deliberately left out is secrets (the League auth token is read, and the ledger says so, but never written).

Before the first line, the program states what it touches: the screen and audio devices while recording, the output folder you chose, its own data folder, the League client on 127.0.0.1, and nothing on the internet. Any network target outside localhost is flagged in the ledger as a violation.

Three readers of the same ledger:

- **Terminal**: `screecap watch` streams it live. This is the primary view; the terminal is the screensaver.
- **Browser**: `screecap open` (or *Open web view* in the tray) opens a page on `127.0.0.1` with status, the manifest, the live ledger, Record/Stop, and a click on any `src` to see the code that ran.
- **File**: `%LOCALAPPDATA%\screecap\ledger.log`, plain text, `tail`-able with anything.

The browser gets a one-time link containing a token, which becomes an HttpOnly cookie; the CLI uses the same token as a Bearer header, read from `%LOCALAPPDATA%\screecap\lockfile` (deliberately the League client's own format). The API binds 127.0.0.1 only and checks `Host` and `Origin`, so no other site or machine can drive it.

## Requirements

- **Windows 10/11.** `dxcam` is Windows-only and won't install on Linux/macOS.
- **Python 3.10+** (Windows Python — not WSL).
- **ffmpeg, "full" build.** The "essentials" build does not include libx264. The preflight will tell you if yours is too thin.
  - `winget install --id Gyan.FFmpeg`, or
  - download from <https://www.gyan.dev/ffmpeg/builds/> and add the `bin/` folder to PATH.

## Install

```powershell
git clone git@github.com:nickjoven/screecap.git
cd screecap
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Verify setup before recording

```powershell
python record.py --check
```

Runs the preflight: ffmpeg present, monitor index reachable, output path writable, encoder works. **Every failure prints a remediation** — read it; it tells you exactly what to install or change.

## Run

**One command** (starts the tray app if needed, opens the browser view, then watches in this terminal):

```powershell
python cli.py
```

```powershell
python cli.py watch            # terminal view only
python cli.py status           # what is running and what it touches
python cli.py ledger -n 100    # last lines of the ledger file
python cli.py open             # browser view only
python cli.py gui              # show the settings window
python cli.py --duration 10    # unknown flags go to the recorder CLI (record.py)
```

**GUI** (settings window, tray icon):

```powershell
python gui.py
```

Pick a monitor, optionally tick audio devices, optionally drag a region, hit **Record**. Press **F9** (rebindable) to toggle without focus.

**CLI** (for scripting and quick clips):

```powershell
python record.py                                          # 60fps, Ctrl+C to stop
python record.py --duration 10 -o clip.mp4                # 10-second clip
python record.py --fps 30 --encoder libx264               # force CPU encoder
python record.py --region 0 0 1280 720                    # specific region
python record.py --list-audio                             # list dshow audio devices
python record.py --audio "Microphone (Realtek)"           # mic only
python record.py --audio "Microphone" --audio "Stereo Mix"  # mix two sources
```

## Features

- **DXGI Desktop Duplication** capture — low CPU, 60+ fps at 1080p.
- **Hardware encoders**: NVENC, Quick Sync, libx264, mpeg4 fallback. `--encoder auto` (the default) probes them in priority order and picks the first that actually works.
- **Audio**: capture from any dshow device; tick multiple devices in the GUI (or repeat `--audio` on the CLI) to mix mic + system audio into one track via ffmpeg's `amix` filter.
- **Region selection**: numeric inputs, or click-and-drag a transparent overlay on the chosen monitor.
- **Global hotkey**: rebindable, default F9, works while another window has focus.
- **Highlights**: while recording, League Live Client events that involve you (kills, deaths, assists, multikills, first blood, aces, your team's dragon/baron/herald/inhibitor takes, your turret kills) and presses of the **mark hotkey** (default **F10**) are written as video timestamps to `<recording>.highlights.json`. When the recording ends, each moment becomes a clip (10 s before, 5 s after, overlapping moments merged) plus a combined `highlights.mp4`, in `<recording>_highlights\` next to the file. Re-encoded with the recording encoder for frame-accurate cuts; **Fast cut** stream-copies instead (instant, snaps to the 2 s keyframe grid). Works for any recording via the mark hotkey; League events need the League connection on.

```powershell
python highlights.py recording.mp4            # cut from the sidecar written during recording
python highlights.py recording.mp4 --list     # show markers and windows only
python highlights.py recording.mp4 --copy     # keyframe-snapped, no re-encode
python highlights.py recording.mp4 --mark 83 "nice play"   # add a marker by hand
python record.py --cut-highlights             # CLI: cut automatically after recording
```

## League of Legends integration

On by default, no recording involved. While the League client is running, screecap connects to its local API and:

- **Remembers your loadout per champion** (summoner spells + a snapshot of the rune page) at the end of every champ select.
- **Shows it in champ select**, with the spells you have selected right now coloured against the saved pair.
- **Lock spells**: pin the pair for a champion. It is applied automatically the next time you pick that champion, and what you play last no longer overwrites it.
- **Postgame reminder**: after each match, a small window asks whether to keep or change the spells for the champion you just played.

**Recording League matches is opt-in.** Tick *Auto-record → League matches* to start a recording when a match begins and stop it when it ends. Untick *League → Loadout memory* to disconnect from the client entirely.

## Troubleshooting

The three issues that cover ~90% of setup problems:

1. **"libx264 not listed by ffmpeg"** — you have the "essentials" ffmpeg build. Replace it with the "full" build (see Requirements).
2. **No audio devices found, or "Stereo Mix" missing** — Windows hides Stereo Mix by default. Right-click the speaker icon → **Sounds** → **Recording** tab → right-click empty area → **Show Disabled Devices** → enable Stereo Mix. Or install a virtual audio cable.
3. **Recording shows black frames** — DRM-protected content (Netflix and similar) is blocked at the OS level; no library bypasses this. If your whole recording is black, double-check you picked the right monitor index for multi-monitor setups.

For everything else, run `python record.py --check`. The output is the troubleshooting guide.

## Project layout

```
cli.py          `screecap` command: open / watch / status / ledger / gui / record
gui.py          PyQt6 settings window + tray; hosts the local API
server.py       localhost API + browser view (stdlib http.server, token + cookie)
web/index.html  the browser view
ledger.py       append-only action ledger (the transparency surface) + manifest
record.py       recorder CLI
recorder.py     capture/encode engine (UI-agnostic)
preflight.py    setup checks + encoder/device discovery
league/         League client API (LCU) integration
loadouts.py     per-champion spell/rune memory + spell lock
highlights.py   highlight markers and clip cutting
tests/          offscreen test suites (run with the Windows venv)
CLAUDE.md       guidance for AI assistants working on this codebase
```

## WSL note

You can keep the source in a WSL filesystem (`/home/<you>/...`) and edit from Linux tooling, but the Python interpreter that *runs* the recorder must be Windows Python — `dxcam` requires Windows COM/DXGI bindings that don't exist on Linux. Reach the WSL path from Windows via `\\wsl$\<distro>\home\...`.
