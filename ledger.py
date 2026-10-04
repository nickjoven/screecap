"""Append-only activity ledger: one line per action, written BEFORE the action runs.

This is the transparency surface. Every subprocess screecap launches, every
local HTTP/WebSocket call it makes, every file or registry key it writes and
every credential it reads is announced here first, verbatim, then answered
with a result line. The terminal view (`cli.py watch`) and the browser view
(`server.py` + web/index.html) are both just readers of this file.

Grammar — six pipe-separated columns, fixed order, one line per entry:

    ts | seq | kind | target | detail | src

    ts      UTC, ISO-8601 with milliseconds, "Z" suffix
    seq     integer, strictly increasing across the life of the file
    kind    exec | http | ws | listen | read | write | api | note | ok | err
    target  what is acted on: program name, URL, path, registry key, "#<seq>"
    detail  the real thing — the full argv, the request body, the row written
    src     file.py:line of the code that did it

`ok` / `err` lines answer an earlier `seq`: their target is "#<that seq>".
Pipes and newlines inside a column are replaced (¦ and ⏎) so the grammar
holds for any content. Nothing is ever paraphrased; secrets are simply not
passed in (see league/lockfile.py — the port is logged, the token is not).

The file is never edited or truncated. When it passes ROTATE_BYTES it is
renamed to ledger.log.1 (shifting older ones up to .3) and a fresh file
starts; `seq` continues from the last line so numbers stay unique.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Optional
from urllib.parse import urlsplit

import logging_setup

log = logging.getLogger(__name__)

KINDS = ("exec", "http", "ws", "listen", "read", "write", "api", "note", "ok", "err")
ROTATE_BYTES = 50 * 1024 * 1024
RING_SIZE = 5000
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# What screecap touches. Shown before anything runs (CLI header, browser view)
# and enforced for network targets: any http/ws/listen entry whose host is not
# local gets an immediate `err` line naming the violation.
MANIFEST = {
    "reads": [
        "the screen (DXGI Desktop Duplication) — only while recording",
        "the audio devices you tick (DirectShow) — only while recording",
        "the League client's lockfile or process command line, for its local port and token",
        "its own data folder: %LOCALAPPDATA%\\screecap (settings, loadouts.db, ledger.log)",
    ],
    "writes": [
        "the MP4 and highlight clips you asked for, in the output folder you chose",
        "%LOCALAPPDATA%\\screecap: screecap.log, ledger.log, loadouts.db, lockfile",
        "HKCU\\Software\\screecap (settings) and, only if you enable it, the HKCU Run key and a Start Menu shortcut",
        "the dedicated '[screecap] auto' rune page and your summoner spells in the League client, only when you apply or lock them",
    ],
    "connects": [
        "127.0.0.1 only: the League client's local API, and screecap's own local API for the browser and terminal views",
    ],
    "never": [
        "anything on the internet",
        "any file outside the folders above",
    ],
}


def path() -> str:
    return os.path.join(logging_setup.log_dir(), "ledger.log")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(s: object) -> str:
    text = str(s)
    text = text.replace("\r\n", "⏎").replace("\n", "⏎").replace("\r", "⏎")
    text = text.replace("|", "¦")
    return _CTRL.sub("", text)


def format_line(rec: Dict) -> str:
    return " | ".join((
        rec["ts"], str(rec["seq"]), rec["kind"], _clean(rec["target"]),
        _clean(rec["detail"]), _clean(rec["src"]),
    ))


def parse_line(line: str) -> Optional[Dict]:
    """Inverse of format_line. Returns None for comments/blank/garbled lines."""
    line = line.rstrip("\r\n")
    if not line or line.startswith("#"):
        return None
    parts = line.split(" | ", 5)
    if len(parts) != 6:
        return None
    ts, seq, kind, target, detail, src = parts
    try:
        seq_i = int(seq)
    except ValueError:
        return None
    ref = None
    if kind in ("ok", "err") and target.startswith("#"):
        try:
            ref = int(target[1:])
        except ValueError:
            ref = None
    return {"ts": ts, "seq": seq_i, "kind": kind, "target": target,
            "detail": detail, "src": src, "ref": ref}


def _caller_src() -> str:
    """file.py:line of the first frame outside this module."""
    f = sys._getframe(1)
    while f is not None:
        fn = f.f_code.co_filename
        if os.path.abspath(fn) != os.path.abspath(__file__):
            try:
                rel = os.path.relpath(fn, PROJECT_DIR)
            except ValueError:
                rel = os.path.basename(fn)
            if rel.startswith(".."):
                rel = os.path.basename(fn)
            return f"{rel.replace(os.sep, '/')}:{f.f_lineno}"
        f = f.f_back
    return "?"


def _host_of(target: str) -> Optional[str]:
    """Host part of a URL, or of 'METHOD URL'. None if there is no URL."""
    for tok in target.split():
        if "://" in tok:
            try:
                return (urlsplit(tok).hostname or "").lower() or None
            except ValueError:
                return None
    return None


class _Ledger:
    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._loaded = False
        self._ring: Deque[Dict] = collections.deque(maxlen=RING_SIZE)
        self._subs: List[Callable[[Dict], None]] = []

    # -- persistence -----------------------------------------------------

    def _load_seq_locked(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(path(), "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return
        for line in reversed(tail.splitlines()):
            rec = parse_line(line)
            if rec:
                self._seq = rec["seq"]
                return

    def _rotate_locked(self, p: str):
        try:
            if os.path.getsize(p) < ROTATE_BYTES:
                return
        except OSError:
            return
        for i in (2, 1):
            src, dst = f"{p}.{i}", f"{p}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(p, f"{p}.1")

    def _append_locked(self, rec: Dict):
        p = path()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            self._rotate_locked(p)
            with open(p, "a", encoding="utf-8") as f:
                f.write(format_line(rec) + "\n")
        except OSError as e:
            # The ledger must never take the app down; say so in the log.
            log.warning("ledger append failed: %s", e)

    # -- emit ------------------------------------------------------------

    def emit(self, kind: str, target: str, detail: str = "", src: Optional[str] = None,
             ref: Optional[int] = None) -> Dict:
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}")
        src = src or _caller_src()
        with self._lock:
            self._load_seq_locked()
            self._seq += 1
            rec = {"ts": _now(), "seq": self._seq, "kind": kind, "target": _clean(target),
                   "detail": _clean(detail), "src": _clean(src), "ref": ref}
            self._append_locked(rec)
            self._ring.append(rec)
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(rec)
            except Exception:
                log.exception("ledger subscriber raised")
        return rec

    # -- read ------------------------------------------------------------

    def recent(self, since: int = 0, limit: int = 500) -> List[Dict]:
        with self._lock:
            out = [r for r in self._ring if r["seq"] > since]
        return out[-limit:] if limit else out

    def subscribe(self, cb: Callable[[Dict], None]) -> None:
        with self._lock:
            if cb not in self._subs:
                self._subs.append(cb)

    def unsubscribe(self, cb: Callable[[Dict], None]) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)


_L = _Ledger()


class Entry:
    """A begun action. Call ok()/err() exactly once, or use as a context manager."""

    def __init__(self, rec: Dict):
        self.rec = rec
        self.seq = rec["seq"]
        self._closed = False
        self._src = rec["src"]

    def ok(self, detail: str = "") -> Dict:
        return self._close("ok", detail)

    def err(self, detail: str = "") -> Dict:
        return self._close("err", detail)

    def _close(self, kind: str, detail: str) -> Dict:
        if self._closed:
            return self.rec
        self._closed = True
        return _L.emit(kind, f"#{self.seq}", detail, src=self._src, ref=self.seq)

    def __enter__(self) -> "Entry":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.err(f"{exc_type.__name__}: {exc}")
        else:
            self.ok()
        return False


def begin(kind: str, target: str, detail: str = "") -> Entry:
    """Announce an action before doing it. Returns the Entry to answer with ok()/err()."""
    src = _caller_src()
    rec = _L.emit(kind, target, detail, src=src)
    entry = Entry(rec)
    if kind in ("http", "ws", "listen"):
        host = _host_of(target)
        if host is not None and host not in LOCAL_HOSTS:
            _L.emit("err", f"#{rec['seq']}", f"OUTSIDE MANIFEST: host {host} is not local",
                    src=src, ref=rec["seq"])
    return entry


act = begin


def note(target: str, detail: str = "") -> Dict:
    """A fact worth seeing that is not an action (e.g. a phase change)."""
    return _L.emit("note", target, detail, src=_caller_src())


def run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run with the full argv announced first and the exit code answered."""
    target = os.path.basename(str(cmd[0]))
    entry = Entry(_L.emit("exec", target, subprocess.list2cmdline(cmd), src=_caller_src()))
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired as e:
        entry.err(f"timeout after {e.timeout}s")
        raise
    except Exception as e:
        entry.err(f"{type(e).__name__}: {e}")
        raise
    entry.ok(f"rc={proc.returncode} in {time.monotonic() - t0:.2f}s")
    return proc


def recent(since: int = 0, limit: int = 500) -> List[Dict]:
    return _L.recent(since, limit)


def subscribe(cb: Callable[[Dict], None]) -> None:
    _L.subscribe(cb)


def unsubscribe(cb: Callable[[Dict], None]) -> None:
    _L.unsubscribe(cb)


def tail(n: int = 50) -> List[Dict]:
    """Last n parsed entries from the file (works from any process)."""
    try:
        with open(path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, max(4096, n * 400))
            f.seek(size - chunk)
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    recs = [r for r in (parse_line(l) for l in text.splitlines()) if r]
    return recs[-n:]


def manifest_lines() -> List[str]:
    """MANIFEST as plain text lines, for terminals."""
    out = []
    for key in ("reads", "writes", "connects", "never"):
        out.append(f"{key}:")
        out.extend(f"  - {item}" for item in MANIFEST[key])
    return out
