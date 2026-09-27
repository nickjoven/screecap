"""Live Client Data API (port 2999, no auth, only available while in-match)."""
import json
import urllib.error
import urllib.request
from typing import Any, Optional

from ._ssl import INSECURE_LOCALHOST_CTX

LIVECLIENT_BASE = "https://127.0.0.1:2999"


def _liveclient_get(path: str, timeout: float = 2.0) -> Optional[Any]:
    url = f"{LIVECLIENT_BASE}{path}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=INSECURE_LOCALHOST_CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        # Not in a match yet, or game just ended.
        return None


def liveclient_events() -> Optional[list]:
    """List of in-match events since match start. None when no match is active."""
    payload = _liveclient_get("/liveclientdata/eventdata")
    return payload.get("Events") if isinstance(payload, dict) else None


def liveclient_active_player() -> Optional[dict]:
    return _liveclient_get("/liveclientdata/activeplayer")


def liveclient_game_data() -> Optional[dict]:
    return _liveclient_get("/liveclientdata/gamestats")
