"""CLI front-end for the screen recorder."""
import argparse
import logging
import signal
import sys

import logging_setup
from preflight import (
    ENCODER_CHOICES,
    ENCODER_PRIORITY,
    check_encoder_listed,
    check_ffmpeg,
    check_monitor,
    check_output,
    list_audio_devices,
    print_checks,
    probe_audio_device,
    probe_encoder,
    resolve_encoder,
)
from recorder import Recorder


def parse_args():
    p = argparse.ArgumentParser(description="Record the Windows desktop to MP4.")
    p.add_argument("-o", "--output", default="recording.mp4", help="Output MP4 path")
    p.add_argument("--fps", type=int, default=60, help="Target frames per second")
    p.add_argument("--duration", type=float, default=None,
                   help="Stop after N seconds (default: run until Ctrl+C)")
    p.add_argument("--monitor", type=int, default=0, help="Monitor index (0 = primary)")
    p.add_argument("--region", nargs=4, type=int,
                   metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
                   help="Capture only a rectangular region in screen coords")
    p.add_argument("--encoder", choices=ENCODER_CHOICES, default="auto",
                   help="Video encoder. 'auto' picks the best available.")
    p.add_argument("--audio", action="append", default=None, metavar="DEVICE",
                   help="Capture from a dshow audio device. Repeat to mix multiple "
                        "devices (e.g. mic + system audio).")
    p.add_argument("--list-audio", action="store_true",
                   help="Print available dshow audio devices, then exit.")
    p.add_argument("--check", action="store_true",
                   help="Run preflight only, then exit. Useful for setup verification.")
    p.add_argument("--stop-on-match-end", action="store_true",
                   help="Watch the League gameflow phase and stop recording when "
                        "the match ends (transition out of 'InProgress'). Requires "
                        "websocket-client and a running League client.")
    return p.parse_args()


def _start_match_end_watcher(recorder, log):
    """Stop `recorder` when the LCU phase transitions out of 'InProgress'.

    Returns the EventStream (caller must .stop() it) or None if the integration
    couldn't be started — the recording continues normally either way.
    """
    try:
        import league
    except ImportError as e:
        log.warning("league package unavailable (%s); --stop-on-match-end ignored", e)
        return None

    # Mutable state shared with the websocket thread. Only ever transitions
    # False -> True; no lock needed since we only read for an OR check.
    state = {"saw_in_progress": False}

    def on_phase(phase):
        log.info("LCU phase: %s (saw_in_progress=%s)", phase, state["saw_in_progress"])
        if phase == "InProgress":
            if not state["saw_in_progress"]:
                print("  (LCU: match in progress; will stop on next phase change)")
            state["saw_in_progress"] = True
            return
        if state["saw_in_progress"]:
            print(f"  (LCU: match ended -> {phase}; stopping)")
            recorder.request_stop()

    def on_connect(lockfile):
        log.info("LCU connected: pid=%d port=%d", lockfile.pid, lockfile.port)
        # EventStream only fires on transitions; querying the current phase here
        # means we recognize an in-progress match immediately instead of waiting
        # for it to end (at which point we'd miss the InProgress -> next edge).
        try:
            on_phase(league.Client(lockfile).gameflow_phase())
        except Exception:
            log.exception("initial phase query failed")

    try:
        stream = league.EventStream(
            lockfile_provider=league.discover_lockfile,
            on_phase=on_phase,
            on_connect=on_connect,
        )
        stream.start()
        print("Watching League gameflow phase; will auto-stop on match end.")
        return stream
    except RuntimeError as e:
        log.warning("could not start LCU stream (%s); --stop-on-match-end ignored", e)
        return None


def main():
    args = parse_args()
    log_path = logging_setup.configure(also_stderr=True)
    log = logging.getLogger("screecap.cli")
    log.info("CLI start: %s", vars(args))
    if log_path:
        print(f"(log: {log_path})")

    if args.list_audio:
        devices = list_audio_devices()
        if not devices:
            print("No dshow audio devices found (or ffmpeg unavailable).")
            sys.exit(1)
        print("Available dshow audio devices:")
        for d in devices:
            print(f"  {d}")
        return

    print("Preflight:")
    base = [check_ffmpeg(), check_monitor(args.monitor), check_output(args.output)]
    for dev in (args.audio or []):
        base.append(probe_audio_device(dev))
    print_checks(base)
    if not all(c.ok for c in base):
        sys.exit(1)

    print(f"\nResolving encoder ({args.encoder})...")
    encoder = resolve_encoder(args.encoder)
    if encoder is None:
        print("  no working encoder found.")
        diagnostic = []
        for enc in (ENCODER_PRIORITY if args.encoder == "auto" else [args.encoder]):
            diagnostic.append(check_encoder_listed(enc))
            if diagnostic[-1].ok:
                diagnostic.append(probe_encoder(enc))
        print_checks(diagnostic)
        sys.exit(1)
    print(f"  using: {encoder}")

    if args.check:
        return

    recorder = Recorder(
        output=args.output,
        fps=args.fps,
        monitor=args.monitor,
        region=args.region,
        encoder=encoder,
        duration=args.duration,
        audio_devices=args.audio,
    )

    try:
        recorder.start()
    except Exception as e:
        sys.exit(f"Could not start recorder: {e}")

    signal.signal(signal.SIGINT, lambda *_: recorder.request_stop())

    lcu_stream = None
    if args.stop_on_match_end:
        lcu_stream = _start_match_end_watcher(recorder, log)

    print(f"\nRecording {recorder.width}x{recorder.height} @ {args.fps}fps "
          f"-> {args.output} (Ctrl+C to stop)")
    try:
        recorder.run()
    finally:
        if lcu_stream is not None:
            lcu_stream.stop(timeout=2.0)
        recorder.shutdown()

    print(f"\nWrote {recorder.frame_count} frames in {recorder.elapsed:.1f}s "
          f"({recorder.avg_fps:.1f} fps avg) -> {args.output}")
    ok, detail = recorder.health()
    if not ok:
        print(f"Note: {detail}", file=sys.stderr)
        # 2 = hard failure (unchanged); 3 = completed but quality-degraded,
        # so unattended/auto-record callers can detect a bad file.
        sys.exit(2 if recorder.error else 3)


if __name__ == "__main__":
    main()
