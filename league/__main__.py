"""CLI for validating the league package against a running client.

Usage:
    python -m league                    # discover client, print phase + summoner
    python -m league --watch            # stream gameflow-phase changes
    python -m league --watch --liveclient   # also poll in-game events at 1 Hz
"""
import argparse
import sys
import time

import logging_setup

from .events import EventStream
from .liveclient import liveclient_events
from .lockfile import discover_lockfile
from .rest import Client


def _print_summary(client: Client):
    try:
        print(f"Phase:    {client.gameflow_phase()}")
    except Exception as e:
        print(f"Phase:    (error: {e})")
    try:
        s = client.current_summoner()
        name = s.get("displayName") or s.get("gameName") or "?"
        print(f"Summoner: {name}  (id={s.get('summonerId')}, level={s.get('summonerLevel')})")
    except Exception as e:
        print(f"Summoner: (error: {e})")


def main():
    ap = argparse.ArgumentParser(description="Probe the League Client API.")
    ap.add_argument("--watch", action="store_true",
                    help="Stream gameflow-phase changes until Ctrl+C.")
    ap.add_argument("--all-events", action="store_true",
                    help="With --watch, print every event URI (very chatty).")
    ap.add_argument("--liveclient", action="store_true",
                    help="Poll the in-game Live Client Data API once per second.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging_setup.configure(also_stderr=args.verbose)

    lf = discover_lockfile()
    if lf is None:
        print("No running League client found. Start the launcher and try again.")
        return 1
    print(f"Lockfile: pid={lf.pid} port={lf.port}")
    _print_summary(Client(lf))

    if not args.watch and not args.liveclient:
        return 0

    stream = None
    if args.watch:
        def on_phase(phase):
            print(f"  phase -> {phase}")

        def on_event(uri, _data):
            if args.all_events:
                print(f"  event: {uri}")

        def on_connect(lf2):
            print(f"  (connected: pid={lf2.pid} port={lf2.port})")

        def on_disconnect():
            print("  (disconnected; retrying)")

        stream = EventStream(
            lockfile_provider=discover_lockfile,
            on_phase=on_phase,
            on_event=on_event if args.all_events else None,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
        )
        stream.start()
        print("Watching gameflow-phase (Ctrl+C to stop)...")

    try:
        while True:
            if args.liveclient:
                events = liveclient_events()
                if events is None:
                    print("  liveclient: not in match")
                else:
                    latest = events[-1] if events else None
                    print(f"  liveclient: {len(events)} events; latest={latest}")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print()
    finally:
        if stream is not None:
            stream.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
