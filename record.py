"""CLI front-end for the screen recorder."""
import argparse
import signal
import sys

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
    return p.parse_args()


def main():
    args = parse_args()

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

    print(f"\nRecording {recorder.width}x{recorder.height} @ {args.fps}fps "
          f"-> {args.output} (Ctrl+C to stop)")
    recorder.run()
    recorder.shutdown()

    print(f"\nWrote {recorder.frame_count} frames in {recorder.elapsed:.1f}s "
          f"({recorder.avg_fps:.1f} fps avg) -> {args.output}")
    if recorder.error:
        print(f"Note: {recorder.error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
