# screecap

Simple Windows desktop recorder. DXGI Desktop Duplication for capture, ffmpeg for encoding. Ships with a CLI and a PyQt6 GUI.

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

**GUI** (recommended for normal use):

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

## Troubleshooting

The three issues that cover ~90% of setup problems:

1. **"libx264 not listed by ffmpeg"** — you have the "essentials" ffmpeg build. Replace it with the "full" build (see Requirements).
2. **No audio devices found, or "Stereo Mix" missing** — Windows hides Stereo Mix by default. Right-click the speaker icon → **Sounds** → **Recording** tab → right-click empty area → **Show Disabled Devices** → enable Stereo Mix. Or install a virtual audio cable.
3. **Recording shows black frames** — DRM-protected content (Netflix and similar) is blocked at the OS level; no library bypasses this. If your whole recording is black, double-check you picked the right monitor index for multi-monitor setups.

For everything else, run `python record.py --check`. The output is the troubleshooting guide.

## Project layout

```
gui.py          PyQt6 GUI
record.py       CLI
recorder.py     capture/encode engine (UI-agnostic)
preflight.py    setup checks + encoder/device discovery
CLAUDE.md       guidance for AI assistants working on this codebase
```

## WSL note

You can keep the source in a WSL filesystem (`/home/<you>/...`) and edit from Linux tooling, but the Python interpreter that *runs* the recorder must be Windows Python — `dxcam` requires Windows COM/DXGI bindings that don't exist on Linux. Reach the WSL path from Windows via `\\wsl$\<distro>\home\...`.
