"""LCU WebSocket event stream with reconnect-on-disconnect.

LCU uses an old WAMP-style protocol. Frames are JSON arrays:
    [5, "OnJsonApiEvent_<route>"]            - SUBSCRIBE
    [6, "OnJsonApiEvent_<route>"]            - UNSUBSCRIBE
    [8, "<event_name>", {uri,eventType,data}] - EVENT delivery
Subscribing to bare "OnJsonApiEvent" delivers every event the client emits.
"""
import json
import logging
import ssl
import threading
from typing import Callable, Optional

try:
    import websocket  # provided by 'websocket-client'
except ImportError:
    websocket = None

import ledger

from .lockfile import Lockfile

log = logging.getLogger(__name__)


class EventStream:
    """Background thread: subscribe to LCU events, dispatch to callbacks.

    Reconnects on disconnect (so it survives client restarts) until stop().
    `lockfile_provider` is called each connection attempt — typically pass
    `discover_lockfile` so credentials refresh after the launcher restarts.
    """

    def __init__(
        self,
        lockfile_provider: Callable[[], Optional[Lockfile]],
        on_phase: Optional[Callable[[str], None]] = None,
        on_event: Optional[Callable[[str, dict], None]] = None,
        on_connect: Optional[Callable[[Lockfile], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        retry_delay: float = 2.0,
    ):
        if websocket is None:
            raise RuntimeError(
                "websocket-client is not installed (pip install websocket-client)"
            )
        self._lockfile_provider = lockfile_provider
        self._on_phase = on_phase
        self._on_event = on_event
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._retry_delay = retry_delay
        self._ws: Optional["websocket.WebSocketApp"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lcu-event-stream", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self):
        was_connected = False
        while not self._stop.is_set():
            lockfile = self._lockfile_provider()
            if lockfile is None:
                if was_connected:
                    was_connected = False
                    if self._on_disconnect:
                        self._on_disconnect()
                if self._stop.wait(self._retry_delay):
                    return
                continue
            try:
                self._run_once(lockfile)
                was_connected = True
            except Exception:
                log.exception("LCU stream error")
            if self._on_disconnect and not self._stop.is_set():
                self._on_disconnect()
            if self._stop.wait(self._retry_delay):
                return

    def _run_once(self, lockfile: Lockfile):
        url = f"wss://127.0.0.1:{lockfile.port}/"
        headers = {"Authorization": lockfile.auth_header}

        def on_open(ws):
            log.info("LCU websocket connected: port=%d", lockfile.port)
            # Subscribe to everything; we filter by URI in on_message.
            ws.send(json.dumps([5, "OnJsonApiEvent"]))
            if self._on_connect:
                try:
                    self._on_connect(lockfile)
                except Exception:
                    log.exception("on_connect callback raised")

        def on_message(_ws, message):
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                log.debug("non-JSON LCU message: %r", message[:200])
                return
            # Subscription acks are short arrays; events are 3-element.
            if not isinstance(payload, list) or len(payload) < 3:
                return
            data = payload[2]
            if not isinstance(data, dict):
                return
            uri = data.get("uri") or ""
            inner = data.get("data")
            if uri == "/lol-gameflow/v1/gameflow-phase" \
                    and self._on_phase and isinstance(inner, str):
                try:
                    self._on_phase(inner)
                except Exception:
                    log.exception("on_phase callback raised")
            if self._on_event:
                try:
                    self._on_event(uri, data)
                except Exception:
                    log.exception("on_event callback raised")

        def on_error(_ws, error):
            log.warning("LCU websocket error: %s", error)

        def on_close(_ws, status, msg):
            log.info("LCU websocket closed: status=%s msg=%s", status, msg)

        self._ws = websocket.WebSocketApp(
            url,
            header=headers,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        # run_forever blocks until the connection drops or close() is called.
        with ledger.begin("ws", url, "subscribe OnJsonApiEvent (every client event, "
                          "filtered locally; token sent in the header, not shown)") as entry:
            self._ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
            entry.ok("closed")
        self._ws = None
