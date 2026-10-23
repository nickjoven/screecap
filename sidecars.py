"""Bundle sidecars written next to a recording, for downstream tools (strata).

    <stem>.done.json    written by Recorder.shutdown() once ffmpeg has exited:
                        the recorder's own account of the file (rc, frames,
                        elapsed). Its presence means "screecap is finished
                        writing the MP4".
    <stem>.match.json   written by the GUI at the League EndOfGame phase:
                        the LCU end-of-game stats block for the game that was
                        being recorded, verbatim under "eog".

Both are atomic (tmp + rename) and announced in the ledger. Neither is ever
required: a consumer must cope with either missing.
"""
import json
import os
import tempfile
import time
from typing import Any, Dict

import ledger

DONE_SUFFIX = ".done.json"
MATCH_SUFFIX = ".match.json"


def path_for(video: str, suffix: str) -> str:
    return os.path.splitext(video)[0] + suffix


def _write(path: str, payload: Dict[str, Any], what: str) -> str:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".sidecar-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
        with ledger.begin("write", path, what):
            os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_done(video: str, **facts: Any) -> str:
    payload = {"schema": "screecap.done/1", "video": os.path.basename(video),
               "finished_at": time.time(), **facts}
    return _write(path_for(video, DONE_SUFFIX), payload,
                  "recording finished: " + ", ".join(f"{k}={v}" for k, v in facts.items()))


def write_match(video: str, eog: Any) -> str:
    payload = {"schema": "screecap.match/1", "video": os.path.basename(video),
               "fetched_at": time.time(), "eog": eog}
    game = eog.get("gameId") if isinstance(eog, dict) else None
    return _write(path_for(video, MATCH_SUFFIX), payload, f"end-of-game stats, gameId={game}")
