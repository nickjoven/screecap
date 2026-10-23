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

## Transparency: the ledger is the product surface

`ledger.py` is an append-only file (`%LOCALAPPDATA%\screecap\ledger.log`) with a fixed six-column grammar: `ts | seq | kind | target | detail | src`. Rules that must hold everywhere:

- **Announce before acting.** `ledger.begin(kind, target, detail)` is called *before* the subprocess/HTTP call/write, and answered with `.ok()`/`.err()` (or used as a context manager). `ledger.run(cmd, **kw)` wraps `subprocess.run`; use it instead of `subprocess.run` in every module. The recorder's ffmpeg `Popen` is announced in `start()` and answered in `shutdown()`.
- **Verbatim, never paraphrased.** The detail column is the real argv (`list2cmdline`), the real request body, the real row written. Do not add a "friendly" description layer; that is where lies would hide.
- **Secrets are omitted, and the omission is stated.** The League lockfile read logs the port and says "token never written here". Never pass the auth token, cookies or the screecap API token into a ledger call. `server.py` writes `<token>` in the lockfile line for this reason.
- **Kinds are closed**: `exec http ws listen read write api note ok err`. `ok`/`err` lines have target `#<seq>` of the line they answer. Adding a kind means updating `KINDS`, `cli.py` colours and `web/index.html` filters together.
- **Network targets are checked against the manifest.** `begin()` emits an `err` "OUTSIDE MANIFEST" for any `http/ws/listen` host that is not local. `MANIFEST` (reads/writes/connects/never) is shown by the CLI header and the browser view; keep it true when adding a capability.
- **Noise discipline.** Reads of our own DB and inbound websocket events are not actions and are not logged; writes, launches and calls are. `discover_lockfile` only logs when it actually read something, because the EventStream retries it every 2 s while League is closed.

Readers: `cli.py watch` (SSE from the API, falling back to tailing the file), `web/index.html` (EventSource + `/api/source` to show the code behind a line), and the file itself. `seq` continues from the last line in the file across restarts so numbers stay unique; rotation at 50 MB renames, never truncates.

### Local API (server.py) and its trust boundary

The GUI process hosts a stdlib `ThreadingHTTPServer` on `127.0.0.1:<ephemeral>` and writes `%LOCALAPPDATA%\screecap\lockfile` as `screecap:<pid>:<port>:<token>:http` (Riot's lockfile format on purpose; `league/lockfile.py` already parses that shape). Checks, in order: Host must be `127.0.0.1:<port>`/`localhost:<port>` (DNS rebinding); Origin, if present, must be our own origin; then token via `Authorization: Bearer` (CLI) or the `screecap` cookie (browser). `GET /?t=<token>` is the only unauthenticated path: it sets the HttpOnly SameSite=Strict cookie and redirects to `/` so the token leaves the address bar. Actions are `POST /api/action/<name>` with no body; the GUI marshals them onto the Qt thread through `ApiBridge` signals (server threads must never touch widgets). `/api/source` serves only files in `server.source_whitelist()`. Every action request is itself a ledger `api` line.

Settings: `GET /api/settings` returns the widget state plus what is available (encoders, monitor specs, audio devices, policies); `POST /api/settings` takes a JSON object of changes, validated against that list (400 on a bad value, 409 while recording), applied to the widgets so the existing save slots persist them, and announced in the ledger with the exact JSON. Both hooks run on the GUI thread through `GuiCall` (a `BlockingQueuedConnection` round-trip); never read or set a widget from a server thread directly. Tests that exercise these endpoints must send the request from a helper thread while the main (GUI) thread pumps events, or they deadlock.

`cli.py` is the launcher and reader: `running()` = lockfile + live `/api/status`; `ensure_running()` spawns `pythonw gui.py --minimized` and waits for the lockfile, and detects the "older instance without API swallowed the single-instance handoff" case by the child exiting early. `launch_terminal_watch()` (used by the browser/tray "Open terminal") prefers `wt.exe new-tab`.

### Tests

`tests/` holds offscreen suites that run under the Windows venv with `QT_QPA_PLATFORM=offscreen` and a temp `LOCALAPPDATA` (set inside each script; env vars do not cross from WSL into the .exe). From WSL: `.venv/Scripts/python.exe "$(wslpath -w tests/test_ledger.py)"`. They construct the real `MainWindow`, so they also catch wiring breaks.

## Windows-only

`dxcam` is a Windows-only package (DXGI Desktop Duplication via COM). It will not even install on Linux/macOS. The source can live anywhere — including a WSL path — but the Python interpreter that runs it must be Windows Python. WSL paths are reachable from Windows via `\\wsl$\<distro>\...`.

## Architecture

Engine + front-ends + integration layer:

```
ledger.py               append-only action ledger + capability manifest  (UI-agnostic)
preflight.py            checks + encoder selection + device enumeration   (UI-agnostic)
recorder.py             Recorder engine: dxcam capture -> ffmpeg pipe     (UI-agnostic)
server.py               localhost API + browser view host (stdlib)        (UI-agnostic)
web/index.html          browser view: status, manifest, live ledger, source drawer
cli.py                  `screecap` launcher/reader: open, watch, status, ledger, gui, record
league/                 LCU (REST + WebSocket) and Live Client Data       (UI-agnostic)
highlights.py           Tagger (markers beside the Recorder) + cut       (UI-agnostic)
sidecars.py             <stem>.done.json (Recorder.shutdown) + <stem>.match.json (EndOfGame) for strata  (UI-agnostic)
tools/avsync.py         A/V offset + drift measurement on a finished MP4 (WSL, numpy; read-only)
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
- **DXGI device-removed (0x887A0005 / 0x887A0007) means dxcam's cached D3D11 device is dead, not that the GPU is broken.** dxcam builds its device once at import in a module-level singleton and never rebuilds it; a driver update or TDR while the tray app runs makes every later `create()`/`grab()` fail until the process restarts (2026-10-13: NVIDIA driver reinstall, every F9 failed for two days). `capture.reset_dxcam()` releases the cameras, drops the `Singleton` instance, builds a new `DXFactory`; `DisplaySource.start()` and the thumbnail grab call it and retry once. Do not classify that HRESULT as transient and spin on the old device.
- **A side-thread watchdog (`STALL_TIMEOUT`, default 30 s) aborts a wedged capture loop.** Frames are fetched by `_pump_frames()` on its own thread and published through a condition variable; `run()` waits on that with a 0.5 s timeout, never on the source directly. On a stall the watchdog stops the **camera, never ffmpeg**, then calls `request_stop()`, which `run()` honours even if the source call never returns (2026-10-10: dxcam spun in its output-recovery loop for 12 h, `run()` never exited, `shutdown()` never ran, ffmpeg was orphaned and the MP4 had no moov atom). The caller's `shutdown()` then finalizes the MP4 normally. It does **not** catch the asleep-screen case where `video_mode` keeps emitting duplicated frames (`frame_count` still advances) — that needs an auto-record duration cap in the front-end.
- **`-framerate N` (never `-r N`) + `-use_wallclock_as_timestamps 1` on the video pipe, `-fps_mode vfr` on the output.** As an *input* option `-r` makes ffmpeg discard input timestamps and regenerate constant-rate ones, which silently cancels the wall-clock flag. That was the A/V drift seen 2026-10-10: Python delivers ~55–57 fps, ffmpeg counted each frame as 1/60 s, the video track ran ~6 % fast (ffprobe: exactly 60.000 fps, duration 6 % short of the log's elapsed) and audio fell a minute behind over a 20-minute game. With `-framerate` the pipe keeps its wall-clock stamps: a slow capture gives a sparse VFR track with correct timing, and video stays on the same real timeline as the live dshow audio so the muxer never holds audio back (which is what overflowed the dshow buffer). Post-fix for files recorded with `-r`: `ffmpeg -itsscale:v <elapsed/(frames/60)> -i in.mp4 -c copy out.mp4`, ratio from the "capture loop exited" log line.
- **`recorder.health()` is the post-record quality verdict, distinct from `recorder.error`.** `error` = hard failure. `health()` catches the class an `rc=0` ffmpeg hides: a valid file with a gappy/silent audio track. It keys off `audio_drops`, tallied from the dshow "frame dropped" stderr lines (and their "Last message repeated N times" collapses) in `_pump_ffmpeg_stderr` — an xrun counter. Both front-ends surface it: the CLI exits `3` (vs `2` for hard error) on degraded; the GUI shows an advisory "audio degraded" warning, not the red "Error" state. Don't route degradation through `error` — it is advisory, not fatal.
- **`request_stop()` is signal-safe and non-blocking.** `shutdown()` is the blocking finalizer. They are separate so the CLI's SIGINT handler can call only `request_stop()`.
- **Audio inputs precede the video pipe on the ffmpeg command line.** ffmpeg rebases every input so its own first packet lands at t=0, so the real-time gap between the first video stamp and the first audio sample becomes a constant A/V offset. Pipe-first put the ~1 s dshow start-up in that gap (audio early); dshow-first means the graph is already running when the pipe is probed, and the gap is milliseconds. Hence `-map N:v` where N = number of audio devices, `-map 0:a` for one device, and `-filter_complex amix=inputs=N:duration=shortest` over `[0:a]..[N-1:a]` with `-map [aout]` for several.

### Highlights: sidecar between the during and after halves

`highlights.Tagger` runs beside the `Recorder` (CLI and GUI both construct it
right after the `Recorder`; the GUI starts it in `_on_capture_started` so the
clock is valid) and appends `Marker`s to `<output>.highlights.json`, rewritten
atomically on every add. `highlights.cut_highlights()` reads that file after
`shutdown()` and drives ffmpeg. Rules that keep it honest:

- **Markers are in video seconds, never game seconds.** The Tagger's `clock`
  is `recorder.elapsed`; a Live Client event is mapped the moment it is first
  seen as `now_video - (gameTime_now - EventTime)`. Polling is ~1 s, so this is
  accurate to the poll and immune to earlier in-game pauses. Events whose
  mapped time is negative (match already running when recording began) are
  dropped.
- **Identity is a set of spellings.** Event fields (`KillerName` etc.) are
  "GameName#TAG" on current patches and the bare summoner name on older ones;
  `_name_variants()` collects every form from `activeplayer` / `playerlist`.
  Team membership (for objective events) comes from `playerlist.team`.
- **`-g fps*KEYFRAME_INTERVAL_S` in the recorder exists for the copy mode.**
  Default GOPs are ~4 s; 2 s bounds how early a stream-copied clip starts.
  Re-encode mode (the default) reuses `preflight.encoder_args` and is
  frame-accurate; `preflight` is imported lazily inside `cut_highlights` so
  `highlights.py` itself imports without dxcam (its unit tests run anywhere).
- **The GUI cuts in `HighlightWorker` on a `QThread`, never inline.** Same
  thread rules as `CaptureWorker`: no `wait()` from the finished slot;
  references are nulled in `_on_hl_thread_finished`.
- **One pynput listener serves both hotkeys** (`_install_hotkey_listener`).
  `_set_mark_hotkey` refuses a key equal to the record hotkey. Settings keys:
  `hotkey/mark`, `highlights/auto_cut`, `highlights/fast`.

### Loadout memory and spell lock

`loadouts.py` keeps one SQLite row per `(puuid, champion_id)` in `%LOCALAPPDATA%\screecap\loadouts.db`. Schema is versioned additively (`SCHEMA_VERSION`, migrated in `_migrate` via `PRAGMA table_info` + `ALTER TABLE ADD COLUMN ... DEFAULT`): v1 = spells/runes/last_seen/count, v2 adds `locked`. Never rename or drop a column — old rows must keep parsing.

- **Capture** happens on the champ-select `Delete` event (`LoadoutTracker._on_session_end`): last spells + a snapshot of `/lol-perks/v1/currentpage`.
- **Locked rows pin the spell pair.** `record_loadout` uses `CASE WHEN loadout.locked` so a locked pair survives session end (count/last_seen/runes still update). `set_locked(..., spell1, spell2)` with a non-zero pair *replaces* the pair ("lock these"); zeros pin the stored pair unchanged; unlocking never touches spells.
- **Auto-apply** (`_maybe_auto_apply`) runs on the EventStream thread on every session frame: when the picked champion has a locked row and the selected pair differs, it `PATCH`es `/my-selection` **once per champion per champ select** (`_auto_apply_seen`, cleared on `Delete`/disconnect). After that the user's manual changes win. The GUI calls `forget_auto_apply` when the lock toggles mid-select so a fresh lock applies immediately.
- `apply_spells` (spells only) is split from `apply_loadout` (spells + runes) so the auto path never rewrites the rune page.

**Defaults: League integration on, recording opt-in.** `lcu/enabled` (default True) owns the EventStream/tracker/panel (`_enable_lcu`/`_disable_lcu`). `lcu/auto_record` (default False) only gates whether `_on_lcu_phase_changed` starts a recording on `InProgress`; ticking it forces the League connection on, and turning the connection off unticks it. Don't re-couple them: the loadout features must work with recording off.

**Postgame reminder** (`lcu/postgame_reminder`, default True): `_on_session_end` stores a `PlayedGame` (the pair actually played, even when the row is locked to something else); the first postgame phase (`is_postgame_phase`: WaitingForStats/PreEndOfGame/EndOfGame) consumes it via `take_last_played()` (one-shot, so one dialog per game) and shows `PostgameDialog` — non-modal, stays on top. "Keep" locks the pair that will be used next time; "Change…" → "Save + lock" locks a chosen pair. A dodge never reaches a postgame phase, so its `PlayedGame` is simply overwritten by the next champ select.

In the GUI, `LoadoutPanel` renders a live "Now: D = …, F = …" line from `cell.spell1Id/spell2Id` on every frame (green = matches saved, orange = differs) and "(hovering)"/"(locked in)" from `pick_locked_in` — the local player's *completed* pick action, since `championId` is set on select, before the lock-in click. The identical-frame short-circuit must compare champ + spells + pick state, and must not fire on the first frame of a session (`_in_session`), or the panel stays hidden until a champion is picked.

### GUI threading model

`gui.py` uses the `QObject` + `moveToThread` pattern (not subclassed `QThread`). The capture loop runs in the worker thread; the main thread polls `recorder.frame_count` / `recorder.elapsed` on a 250 ms `QTimer`. No locks needed — the only crossing data is integers/floats that Python writes atomically, and they're advisory (UI display only).

The global hotkey uses `pynput`. pynput's callback runs in its own thread, so it emits a signal on `HotkeyBridge` (a QObject) — Qt automatically marshals cross-thread signals via QueuedConnection. If `pynput` is not installed, the rest of the app still works; only the hotkey is disabled (status bar tells the user).

The hotkey is **rebindable**: clicking the hotkey button opens `HotkeyCaptureDialog`, which spins up a one-shot pynput listener for the next keypress. Two invariants:

1. **The main listener is paused before the dialog opens** and re-started after — otherwise the user's chosen keypress would simultaneously trigger a record toggle through the old hotkey.
2. **The capture dialog skips modifier-only presses** (`shift`, `ctrl`, `alt`, `cmd`) so users can't accidentally bind a modifier as the toggle. The next non-modifier press wins.

Storing the target as a pynput-native `Key` or `KeyCode` (not a Qt `QKeySequence`) avoids a fragile Qt→pynput translation layer.

### Frame sources (capture.py)

`Recorder` owns a *source*, not a dxcam camera: `capture.DisplaySource` (dxcam Desktop Duplication, whole output, `video_mode=True` required) or `capture.WindowSource` (Windows Graphics Capture via the `windows-capture` package, one window by HWND, cursor off, border off). Both expose `start() -> (w, h)`, `first_frame()`, `get_latest_frame()` and `stop()`; `run()` and the watchdog are source-agnostic. WindowSource receives frames on a callback thread only when the window redraws, so `get_latest_frame()` paces to `fps` and repeats the last frame (dxcam's video_mode semantics). The output size is fixed at start to the even floor of the first frame (yuv420p needs even dimensions; a window can be any size) and later frames are cropped/padded to it. A closed window yields `None`, which ends the recording cleanly rather than as an error. The GUI uses a WindowSource when the monitor spec is `auto` and `screens.find_game_window()` finds the League client; `record.py --monitor auto` does the same.

### Monitor identity (screens.py)

**Never store or compare a bare DXGI output index.** On 2026-10-03 the same machine, same two monitors, enumerated primary-first in one process and top-to-bottom in the next; "monitor 1" became the other screen and every recording that evening captured the desktop. `screens.enumerate_outputs()` gives each output its device name (`\\.\DISPLAY2`), desktop rectangle and primary flag; a *monitor spec* is `auto` (the display holding the League game window, class `RiotWindowClass`, else primary), `primary`, or a device name. Specs resolve to an index only at capture time via `screens.resolve()`, which also returns a one-line reason that goes into the log and the ledger (`screen:choice`), and the recorder's `read screen:` line names the display by identity. The GUI stores `capture/monitor_spec`; a legacy `capture/monitor` int migrates to `auto` (not to a guessed display) with a ledger note. The region picker maps the chosen output to a QScreen by geometry, not by position in `screens()`. `record.py --monitor` takes a spec; a bare index still works but is called out as unstable.

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
