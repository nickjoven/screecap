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

Engine + front-ends + integration layer:

```
preflight.py            checks + encoder selection + device enumeration   (UI-agnostic)
recorder.py             Recorder engine: dxcam capture -> ffmpeg pipe     (UI-agnostic)
league/                 LCU (REST + WebSocket) and Live Client Data       (UI-agnostic)
loadouts.py             Per-champion spell/rune memory, spell lock, apply   (UI-agnostic)
record.py               CLI front-end (argparse, signal.SIGINT)
gui.py                  PyQt6 front-end (worker thread, hotkey, tray icon)
windows_integration.py  AppUserModelID, run-on-login, shortcut, jump list (Win-only)
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
- **`-thread_queue_size 1024`** on both inputs sizes the inter-thread *packet* queue at higher fps. It is **not** the dshow real-time buffer — the "real-time buffer too full ... [rtbufsize parameter]! frame dropped!" warning is a different knob.
- **`-rtbufsize 256M` (`AUDIO_RTBUFSIZE`) on every dshow audio input.** ffmpeg's default dshow real-time buffer is ~3 MB; under load — or whenever the video clock stalls and `-shortest`-paced audio backs up — it overflows continuously and ffmpeg drops *every* audio frame, yielding a silent track. This is the usual root cause of "no audio." Do not conflate it with `-thread_queue_size`.
- **A side-thread watchdog (`STALL_TIMEOUT`, default 30 s) aborts a wedged capture loop.** It catches `get_latest_frame()` blocking forever — which neither the `frame is None` check (commit `0806b28`) nor `_stop` can reach because `run()` never returns from the call. On a stall it stops the **camera, never ffmpeg**: dxcam then returns `None`, the existing None branch breaks the loop, and `shutdown()` finalizes the MP4 normally (the no-kill invariant holds). It does **not** catch the asleep-screen case where `video_mode` keeps emitting duplicated frames (`frame_count` still advances) — that needs an auto-record duration cap in the front-end.
- **`-use_wallclock_as_timestamps 1` on the video pipe + `-fps_mode vfr` on the output.** The raw pipe would otherwise be treated as constant-rate `-r fps`: a slow/stalled capture makes video's media clock lag wall time, ffmpeg throttles the pipe read, and the live dshow audio buffer overflows → dropped audio. Wall-clock PTS puts video on the same real timeline as the audio, so the continuous audio is effectively the clock master and video conforms (sparse/stretched) instead of starving it. This is "audio as clock master" achieved *without* taking dshow capture away from ffmpeg — the in-process-mux decision above stays intact. The two flags are a pair: wall-clock input makes the stream genuinely variable-rate, so the muxer must be told `vfr` rather than re-padding back to CFR (which would reintroduce the divergence). **Tradeoff: output is VFR**, not CFR. `-r fps` stays only as the nominal rate.
- **`recorder.health()` is the post-record quality verdict, distinct from `recorder.error`.** `error` = hard failure. `health()` catches the class an `rc=0` ffmpeg hides: a valid file with a gappy/silent audio track. It keys off `audio_drops`, tallied from the dshow "frame dropped" stderr lines (and their "Last message repeated N times" collapses) in `_pump_ffmpeg_stderr` — an xrun counter. Both front-ends surface it: the CLI exits `3` (vs `2` for hard error) on degraded; the GUI shows an advisory "audio degraded" warning, not the red "Error" state. Don't route degradation through `error` — it is advisory, not fatal.
- **`request_stop()` is signal-safe and non-blocking.** `shutdown()` is the blocking finalizer. They are separate so the CLI's SIGINT handler can call only `request_stop()`.
- **Multi-device audio uses `-filter_complex amix=inputs=N:duration=shortest`** with explicit `-map 0:v -map [aout]`. The `[N:a]` indices are 1-based against the audio inputs because input `0` is the raw video pipe. Adding the filter only when N>1 keeps the single-device path simple.

### Loadout memory and spell lock

`loadouts.py` keeps one SQLite row per `(puuid, champion_id)` in `%LOCALAPPDATA%\screecap\loadouts.db`. Schema is versioned additively (`SCHEMA_VERSION`, migrated in `_migrate` via `PRAGMA table_info` + `ALTER TABLE ADD COLUMN ... DEFAULT`): v1 = spells/runes/last_seen/count, v2 adds `locked`. Never rename or drop a column — old rows must keep parsing.

- **Capture** happens on the champ-select `Delete` event (`LoadoutTracker._on_session_end`): last spells + a snapshot of `/lol-perks/v1/currentpage`.
- **Locked rows pin the spell pair.** `record_loadout` uses `CASE WHEN loadout.locked` so a locked pair survives session end (count/last_seen/runes still update). `set_locked(..., spell1, spell2)` with a non-zero pair *replaces* the pair ("lock these"); zeros pin the stored pair unchanged; unlocking never touches spells.
- **Auto-apply** (`_maybe_auto_apply`) runs on the EventStream thread on every session frame: when the picked champion has a locked row and the selected pair differs, it `PATCH`es `/my-selection` **once per champion per champ select** (`_auto_apply_seen`, cleared on `Delete`/disconnect). After that the user's manual changes win. The GUI calls `forget_auto_apply` when the lock toggles mid-select so a fresh lock applies immediately.
- `apply_spells` (spells only) is split from `apply_loadout` (spells + runes) so the auto path never rewrites the rune page.

In the GUI, `LoadoutPanel` renders a live "Now: D = …, F = …" line from `cell.spell1Id/spell2Id` on every frame (green = matches saved, orange = differs) and "(hovering)"/"(locked in)" from `pick_locked_in` — the local player's *completed* pick action, since `championId` is set on select, before the lock-in click. The identical-frame short-circuit must compare champ + spells + pick state, and must not fire on the first frame of a session (`_in_session`), or the panel stays hidden until a champion is picked.

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

### Tray icon, single-instance, jump list

The GUI is single-instance via `QLocalServer` on the key `screecap-gui`. A second invocation tries to connect to that socket; on success it sends one of:

- `show` — raise the running window (sent when no other action was requested);
- `jump:<action>` — dispatch a jump-list verb (`toggle-record`, `open-last`, `toggle-auto-record`, `open-data-dir`).

If the connect fails (no server yet), the new process *becomes* the server and dispatches the action against itself. This is what lets jump-list tasks "do the thing" whether or not the GUI was already running, without ever spawning a duplicate.

Three invariants make the tray work:

1. **`QApplication.setQuitOnLastWindowClosed(False)`** is set in `main()` before constructing the app. Without it, hiding the last window would quit the process and the tray would disappear.
2. **`closeEvent` hides instead of closing** unless `_really_quit` is set. Only the tray's "Quit" action sets it. As a fallback, if no system tray is available, close behaves normally — so the app can still be exited on systems without a tray.
3. **`AppUserModelID` is set at module import time** in `gui.py` via `windows_integration.set_app_user_model_id()`, *before* any window or shell COM is touched. Without it, taskbar pinning and jump-list grouping attach to `pythonw.exe` instead of the app, and the running process appears as a separate icon from a pinned shortcut.

`--minimized` skips the initial `show()` (used by the run-on-login entry). `--jump=<action>` is for jump-list tasks: handed off to a running instance via IPC, or run by this instance after the GUI is up. Shortcuts created via `windows_integration.install_shortcut` deliberately do *not* pass `--minimized` so clicking them from the Start Menu opens the window; only the `HKCU\Run` entry passes `--minimized`.

The jump-list `PKEY_Title` (visible label) is set via `IPropertyStore` on each `IShellLink`; `SetDescription` only controls the hover tooltip. If the title isn't set, the entry shows the .exe filename instead.

## Common gotchas

- **DRM-protected content captures as black frames** (Netflix, some streaming apps). Enforced by Windows; no library bypasses this.
- **Multiple GPUs** (laptop iGPU + dGPU): `dxcam.create()` may pick the wrong one. Use `device_idx=` if you add a flag for it.
- **ffmpeg "essentials" build lacks libx264.** The remediation strings already say this — keep it that way; it's the single most common setup failure.
- **Stereo Mix is disabled by default on most Windows installs.** For system audio capture, the user must enable it in Sound settings (Recording tab → Show Disabled Devices) or use a virtual audio cable.
- **`ffmpeg -list_devices` output format is version-dependent.** Legacy ffmpeg (≤ ~4.x) groups devices under "DirectShow audio/video devices" header lines; modern ffmpeg (~7.x+) drops the headers and tags each device inline as `(audio)`/`(video)`. `list_audio_devices()` handles both — header-only parsing silently returns *zero* devices on modern ffmpeg even though they exist. Keep both paths if you touch that parser.
- **`pywin32` jump-list COM is non-fatal.** Every helper in `windows_integration.py` is wrapped in try/except and returns False (or None) on failure — the GUI keeps working without integration. Don't promote any of these failures into a hard error.
