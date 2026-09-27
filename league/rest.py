"""Synchronous REST client for the LCU."""
import json
import urllib.request
from typing import Any

from ._ssl import INSECURE_LOCALHOST_CTX
from .lockfile import Lockfile

# Sentinel that means "no body" — distinct from None, which is a valid JSON
# body the LCU sometimes accepts (e.g. PUT with `null` to clear a value).
_NO_BODY = object()


class Client:
    """Synchronous LCU REST client. TLS verification disabled for the self-signed cert."""

    def __init__(self, lockfile: Lockfile, timeout: float = 5.0):
        self.lockfile = lockfile
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = _NO_BODY) -> Any:
        url = f"{self.lockfile.base_url}{path}"
        data = json.dumps(body).encode() if body is not _NO_BODY else None
        headers = {
            "Authorization": self.lockfile.auth_header,
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout, context=INSECURE_LOCALHOST_CTX) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    # Convenience accessors for the endpoints we'll use to drive recording.
    def gameflow_phase(self) -> str:
        # Returns the phase as a JSON string, e.g. "InProgress".
        return self.get("/lol-gameflow/v1/gameflow-phase")

    def current_summoner(self) -> dict:
        return self.get("/lol-summoner/v1/current-summoner")

    def champ_select_session(self) -> dict:
        return self.get("/lol-champ-select/v1/session")

    def end_of_game_stats(self) -> dict:
        return self.get("/lol-end-of-game/v1/eog-stats-block")
