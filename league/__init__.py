"""League Client (LCU) integration: lockfile discovery, REST, WebSocket, Live Client Data.

The LCU exposes an authenticated localhost REST + WebSocket API while the
launcher is running. The Live Client Data API on port 2999 provides per-game
state (events, gold, items) only while a match is in progress.

Submodules:
    lockfile     - credentials discovery (paths + running-process)
    rest         - synchronous REST Client
    events       - WebSocket EventStream (background thread, reconnects)
    liveclient   - in-match Live Client Data API helpers

Standalone validation: `python -m league` (see __main__.py for flags).
"""
from .events import EventStream
from .liveclient import (
    LIVECLIENT_BASE,
    liveclient_active_player,
    liveclient_events,
    liveclient_game_data,
)
from .lockfile import (
    Lockfile,
    discover_lockfile,
    find_lockfile_via_paths,
    find_lockfile_via_process,
)
from .rest import Client

__all__ = [
    "Client",
    "EventStream",
    "LIVECLIENT_BASE",
    "Lockfile",
    "discover_lockfile",
    "find_lockfile_via_paths",
    "find_lockfile_via_process",
    "liveclient_active_player",
    "liveclient_events",
    "liveclient_game_data",
]
