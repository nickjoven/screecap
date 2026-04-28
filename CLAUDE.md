# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run targets

```powershell
pip install -r requirements.txt
python gui.py                          # PyQt6 GUI
python record.py                       # CLI; Ctrl+C to stop
python record.py --check               # preflight only — verify setup
python record.py --list-audio          # enumerate dshow audio devices
python record.py --duration 10 --encoder libx264 -o clip.mp4
python record.py --audio "Microphone (USB)" --audio "Stereo Mix"  # mix two sources
```

There is no test suite or build step.

## Windows-only

`dxcam` is a Windows-only package (DXGI Desktop Duplication via COM). It will not even install on Linux/macOS. The source can live anywhere — including a WSL path — but the Python interpreter that runs it must be Windows Python. WSL paths are reachable from Windows via `\\wsl$\<distro>\...`.

## Architecture

Four files, two layers — engine and front-ends:

```
preflight.py     checks + encoder selection + device enumeration   (UI-agnostic)
recorder.py      Recorder engine: dxcam capture -> ffmpeg pipe     (UI-agnostic)
record.py        CLI front-end (argparse, signal.SIGINT)
gui.py           PyQt6 front-end (QThread worker, QTimer polling, rebindable hotkey)
```

Both front-ends compose the same engine. Do not duplicate engine logic into a UI.

### Why ffmpeg owns the encoder (not OpenCV)

The recorder pipes raw BGR frames to an `ffmpeg` subprocess instead of using `cv2.VideoWriter`. The two reasons that matter:

1. **Codec quality and hardware encoders.** `mp4v` (the OpenCV default) softens text — bad for screen content. ffmpeg gives us libx264 with `-tune zerolatency` and hardware encoders (`h264_nvenc`, `h264_qsv`) with one flag.
2. **Audio muxing.** When audio is enabled, ffmpeg captures it via `-f dshow` *in the same process* and muxes A/V together with consistent timestamps. Doing this externally would mean separate audio capture + a post-record mux step + sync drift.

If you find yourself reaching for OpenCV's `VideoWriter`, you are about to undo this decision.

### Preflight is the API for "what can go wrong"

Every check in `preflight.py` returns a `Check(ok, name, detail, remediation)` and **must** populate `remediation` on failure with a concrete next action (a command to run, a setting to change). "ffmpeg not found" alone is half a feature. The CLI's `print_checks` and the GUI's `_show_remediation` both surface this same field.

Encoder discovery is two-stage:
1. `check_encoder_listed(name)` — fast: parses `ffmpeg -encoders`. Used at GUI startup to populate the dropdown.
2. `probe_encoder(name)` — slow (~0.5–2 s): encodes one black frame. The only reliable way to catch "ffmpeg lists `h264_nvenc` but you're on an AMD laptop." Used at record time on the chosen encoder, and walked across the priority list when `auto` is requested.

`ENCODER_PRIORITY = ["h264_nvenc", "h264_qsv", "libx264", "mpeg4"]` — `auto` walks this list, probing each, and picks the first that works.

### Recorder lifecycle and key invariants

```
r = Recorder(...)
r.start()         # spin up dxcam + ffmpeg, write the first frame
r.run()           # blocks: capture-to-pipe loop until request_stop() or duration
r.shutdown()      # close stdin, wait up to 15 s for ffmpeg to write the moov atom
```

Hard rules:

- **`video_mode=True` on `camera.start()` is required.** Without it, dxcam skips frames when the screen hasn't changed, and the resulting MP4 has broken timing.
- **`shutdown()` must close stdin and wait — not kill.** Killing ffmpeg before it finalizes the moov atom produces an unplayable MP4. The 15 s ceiling exists so a hung ffmpeg can't hold the caller hostage; if it trips, `recorder.error` is set.
- **Audio requires `-shortest`.** dshow audio is a live source with no EOF. Without `-shortest`, ffmpeg hangs forever after the video pipe closes, waiting for audio that never ends.
- **`-thread_queue_size 1024`** on both inputs avoids "real-time buffer too full" warnings from dshow at higher fps.
- **`request_stop()` is signal-safe and non-blocking.** `shutdown()` is the blocking finalizer. They are separate so the CLI's SIGINT handler can call only `request_stop()`.
- **Multi-device audio uses `-filter_complex amix=inputs=N:duration=shortest`** with explicit `-map 0:v -map [aout]`. The `[N:a]` indices are 1-based against the audio inputs because input `0` is the raw video pipe. Adding the filter only when N>1 keeps the single-device path simple.

### GUI threading model

`gui.py` uses the `QObject` + `moveToThread` pattern (not subclassed `QThread`). The capture loop runs in the worker thread; the main thread polls `recorder.frame_count` / `recorder.elapsed` on a 250 ms `QTimer`. No locks needed — the only crossing data is integers/floats that Python writes atomically, and they're advisory (UI display only).

The global hotkey uses `pynput`. pynput's callback runs in its own thread, so it emits a signal on `HotkeyBridge` (a QObject) — Qt automatically marshals cross-thread signals via QueuedConnection. If `pynput` is not installed, the rest of the app still works; only the hotkey is disabled (status bar tells the user).

The hotkey is **rebindable**: clicking the hotkey button opens `HotkeyCaptureDialog`, which spins up a one-shot pynput listener for the next keypress. Two invariants:

1. **The main listener is paused before the dialog opens** and re-started after — otherwise the user's chosen keypress would simultaneously trigger a record toggle through the old hotkey.
2. **The capture dialog skips modifier-only presses** (`shift`, `ctrl`, `alt`, `cmd`) so users can't accidentally bind a modifier as the toggle. The next non-modifier press wins.

Storing the target as a pynput-native `Key` or `KeyCode` (not a Qt `QKeySequence`) avoids a fragile Qt→pynput translation layer.

### Region picker and DPI

`RegionPicker` is a transparent fullscreen overlay positioned via `screen.geometry()` from `QtGui.QGuiApplication.screens()`. **Coordinates from `event.position()` are Qt logical pixels; `dxcam` expects physical pixels** for its `region` argument. Multiply by `screen.devicePixelRatio()` before emitting. If you forget, captured regions on high-DPI monitors will be off by the scale factor.

Qt's screen ordering is *assumed* to match `dxcam`'s `output_idx` ordering; this is true for the common case but not guaranteed. The picker logs a warning and bails if `monitor_idx` is out of range for `QGuiApplication.screens()`.

## Common gotchas

- **DRM-protected content captures as black frames** (Netflix, some streaming apps). Enforced by Windows; no library bypasses this.
- **Multiple GPUs** (laptop iGPU + dGPU): `dxcam.create()` may pick the wrong one. Use `device_idx=` if you add a flag for it.
- **ffmpeg "essentials" build lacks libx264.** The remediation strings already say this — keep it that way; it's the single most common setup failure.
- **Stereo Mix is disabled by default on most Windows installs.** For system audio capture, the user must enable it in Sound settings (Recording tab → Show Disabled Devices) or use a virtual audio cable.
