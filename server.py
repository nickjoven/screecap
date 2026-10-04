"""Local HTTP API + browser view for a running screecap instance.

One process (the GUI, which already runs single-instance in the tray) hosts
this server. It binds 127.0.0.1 on an ephemeral port and writes its own
lockfile in the data folder, deliberately in the League client's format:

    screecap:<pid>:<port>:<token>:http

The CLI reads that file to find the running instance; the browser gets the
token once, in the URL the CLI/tray opens, and exchanges it for a cookie.

Trust boundary, in order of what each check stops:
  * bind 127.0.0.1 only — nothing off this machine can reach it;
  * Host header must be 127.0.0.1:<port> or localhost:<port> — stops DNS
    rebinding (a hostile site resolving its name to 127.0.0.1);
  * Origin, when present, must be our own origin — stops cross-site requests
    from other local pages; the cookie is HttpOnly + SameSite=Strict too;
  * every request needs the token (cookie for the browser, Bearer for the
    CLI) — stops anything else on this machine that merely knows the port.
Every action the API triggers is announced in the ledger as kind `api`.

Stdlib only: no new dependency for the transparency surface.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import ledger
import logging_setup

log = logging.getLogger(__name__)

COOKIE = "screecap"
WEB_DIR = os.path.join(ledger.PROJECT_DIR, "web")
GITHUB_REPO = "https://github.com/nickjoven/screecap"
VERSION = "0.4"


def lockfile_path() -> str:
    return os.path.join(logging_setup.log_dir(), "lockfile")


def read_lockfile() -> Optional[Dict]:
    """{pid, port, token} of a running instance, or None if absent/stale."""
    try:
        with open(lockfile_path(), "r", encoding="utf-8") as f:
            parts = f.read().strip().split(":")
    except OSError:
        return None
    if len(parts) != 5 or parts[0] != "screecap":
        return None
    try:
        info = {"pid": int(parts[1]), "port": int(parts[2]), "token": parts[3]}
    except ValueError:
        return None
    try:
        import psutil
        if not psutil.pid_exists(info["pid"]):
            return None
    except ImportError:
        pass
    return info


def _git_commit() -> str:
    try:
        r = ledger.run(["git", "-C", ledger.PROJECT_DIR, "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "main"
    except Exception:
        return "main"


def source_whitelist() -> Dict[str, str]:
    """relative path -> absolute path for every file the browser may view."""
    out = {}
    for name in os.listdir(ledger.PROJECT_DIR):
        if name.endswith(".py"):
            out[name] = os.path.join(ledger.PROJECT_DIR, name)
    for sub in ("league", "web"):
        d = os.path.join(ledger.PROJECT_DIR, sub)
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith((".py", ".html")):
                    out[f"{sub}/{name}"] = os.path.join(d, name)
    return out


@dataclass
class Hooks:
    """What the hosting process lends the API. All callables may run on a server thread."""
    status: Callable[[], Dict]
    actions: Dict[str, Callable[[], Optional[str]]] = field(default_factory=dict)
    # settings() -> current settings + what is available; apply_settings(changes) -> new
    # settings. apply raises ValueError (bad value -> 400) or RuntimeError (not now -> 409).
    settings: Optional[Callable[[], Dict]] = None
    apply_settings: Optional[Callable[[Dict], Dict]] = None


class _Ctx:
    def __init__(self, hooks: Hooks, token: str):
        self.hooks = hooks
        self.token = token
        self.port = 0
        self.started = time.time()
        self.stopping = threading.Event()
        self.commit = "main"
        self.sources = source_whitelist()

    @property
    def origins(self):
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    @property
    def hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "screecap/" + VERSION
    protocol_version = "HTTP/1.1"

    @property
    def ctx(self) -> _Ctx:
        return self.server.ctx  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        log.debug("api: " + fmt, *args)

    # -- helpers -----------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: Optional[Dict[str, str]] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def _origin_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host not in self.ctx.hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in self.ctx.origins:
            return False
        return True

    def _auth_ok(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), self.ctx.token):
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and secrets.compare_digest(v, self.ctx.token):
                return True
        return False

    def _gate(self) -> bool:
        if not self._origin_ok():
            self._json(403, {"error": "bad Host or Origin"})
            return False
        if not self._auth_ok():
            self._json(401, {"error": "not authorised: open screecap from the tray or "
                                      "run `screecap open` to get a fresh link"})
            return False
        return True

    # -- GET ---------------------------------------------------------------

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            return self._get_index(qs)
        if url.path == "/api/status":
            return self._gate() and self._json(200, self._status())
        if url.path == "/api/manifest":
            return self._gate() and self._json(200, {
                "manifest": ledger.MANIFEST, "ledger_path": ledger.path(),
                "repo": GITHUB_REPO, "commit": self.ctx.commit,
                "data_dir": logging_setup.log_dir(),
            })
        if url.path == "/api/ledger":
            if not self._gate():
                return None
            since = int((qs.get("since") or ["0"])[0])
            limit = int((qs.get("limit") or ["500"])[0])
            return self._json(200, {"entries": ledger.recent(since, limit)})
        if url.path == "/api/ledger/stream":
            if not self._gate():
                return None
            # EventSource reconnects send Last-Event-ID; it wins over ?since.
            since = self.headers.get("Last-Event-ID") or (qs.get("since") or ["0"])[0]
            try:
                since_i = int(since)
            except ValueError:
                since_i = 0
            return self._stream(since_i)
        if url.path == "/api/source":
            return self._gate() and self._source((qs.get("src") or [""])[0])
        if url.path == "/api/settings":
            if not self._gate():
                return None
            if self.ctx.hooks.settings is None:
                return self._json(404, {"error": "settings not available"})
            try:
                return self._json(200, self.ctx.hooks.settings())
            except Exception as e:
                log.exception("settings hook raised")
                return self._json(500, {"error": str(e)})
        self._json(404, {"error": "no such path"})

    def _get_index(self, qs):
        if not self._origin_ok():
            return self._json(403, {"error": "bad Host or Origin"})
        token = (qs.get("t") or [None])[0]
        if token is not None:
            if not secrets.compare_digest(token, self.ctx.token):
                return self._send(403, b"bad token", "text/plain; charset=utf-8")
            # Exchange the one-time URL token for a cookie and strip it from
            # the address bar so it is not left in history/screenshots.
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie",
                             f"{COOKIE}={self.ctx.token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if not self._auth_ok():
            return self._send(401, (
                b"screecap: not authorised.\n\nThis page needs the one-time link. "
                b"Open it from the tray icon (Open web view) or run `screecap open`."
            ), "text/plain; charset=utf-8")
        try:
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                body = f.read()
        except OSError:
            return self._send(500, b"web/index.html missing", "text/plain; charset=utf-8")
        return self._send(200, body, "text/html; charset=utf-8", {
            "Content-Security-Policy":
                "default-src 'none'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:",
        })

    def _status(self) -> Dict:
        try:
            st = dict(self.ctx.hooks.status() or {})
        except Exception as e:
            log.exception("status hook raised")
            st = {"error": str(e)}
        st.update({
            "pid": os.getpid(), "port": self.ctx.port, "version": VERSION,
            "started": self.ctx.started, "now": time.time(),
            "ledger_seq": (ledger.recent(0, 1) or [{"seq": 0}])[-1]["seq"],
        })
        return st

    def _stream(self, since: int):
        """Server-sent events: replay everything after `since`, then live."""
        q: "queue.Queue[Dict]" = queue.Queue(maxsize=2000)

        def on_rec(rec):
            try:
                q.put_nowait(rec)
            except queue.Full:
                pass

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = since
        # Subscribe BEFORE replaying so nothing emitted in between is lost;
        # the seq check below drops anything the replay already covered.
        ledger.subscribe(on_rec)
        try:
            for rec in ledger.recent(since, 2000):
                self._sse(rec)
                last = rec["seq"]
            try:
                while not self.ctx.stopping.is_set():
                    try:
                        rec = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if rec["seq"] <= last:
                        continue
                    last = rec["seq"]
                    self._sse(rec)
            finally:
                ledger.unsubscribe(on_rec)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        return None

    def _sse(self, rec: Dict):
        self.wfile.write(f"id: {rec['seq']}\ndata: {json.dumps(rec, ensure_ascii=False)}\n\n"
                         .encode("utf-8"))
        self.wfile.flush()

    def _source(self, src: str):
        """The code behind a ledger line: ±12 lines around file.py:N, whitelisted files only."""
        file, _, line_s = src.partition(":")
        abspath = self.ctx.sources.get(file)
        if abspath is None:
            return self._json(404, {"error": "not a screecap source file"})
        try:
            line = max(1, int(line_s or "1"))
        except ValueError:
            line = 1
        try:
            with open(abspath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as e:
            return self._json(500, {"error": str(e)})
        start = max(1, line - 12)
        end = min(len(lines), line + 12)
        return self._json(200, {
            "file": file, "line": line, "start": start,
            "lines": lines[start - 1:end],
            "github": f"{GITHUB_REPO}/blob/{self.ctx.commit}/{file}#L{line}",
        })

    # -- POST --------------------------------------------------------------

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if not self._gate():
            return None
        if url.path == "/api/settings":
            return self._post_settings(body)
        if not url.path.startswith("/api/action/"):
            return self._json(404, {"error": "no such path"})
        name = url.path[len("/api/action/"):]
        fn = self.ctx.hooks.actions.get(name)
        if fn is None:
            return self._json(404, {"error": f"unknown action {name!r}",
                                    "actions": sorted(self.ctx.hooks.actions)})
        via = "browser" if self.headers.get("Cookie") else "cli"
        entry = ledger.begin("api", f"POST /api/action/{name}", f"from {via}")
        try:
            result = fn()
        except Exception as e:
            entry.err(f"{type(e).__name__}: {e}")
            log.exception("action %s raised", name)
            return self._json(500, {"error": str(e)})
        entry.ok(result or "")
        return self._json(202, {"ok": True, "action": name, "result": result})


    def _post_settings(self, body: bytes):
        hooks = self.ctx.hooks
        if hooks.apply_settings is None:
            return self._json(404, {"error": "settings not available"})
        try:
            changes = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return self._json(400, {"error": "body must be JSON"})
        if not isinstance(changes, dict) or not changes:
            return self._json(400, {"error": "body must be a non-empty JSON object"})
        via = "browser" if self.headers.get("Cookie") else "cli"
        entry = ledger.begin("api", "POST /api/settings",
                             f"from {via}: " + json.dumps(changes, ensure_ascii=False, sort_keys=True))
        try:
            result = hooks.apply_settings(changes)
        except ValueError as e:
            entry.err(f"rejected: {e}")
            return self._json(400, {"error": str(e)})
        except RuntimeError as e:
            entry.err(f"refused: {e}")
            return self._json(409, {"error": str(e)})
        except Exception as e:
            entry.err(f"{type(e).__name__}: {e}")
            log.exception("apply_settings raised")
            return self._json(500, {"error": str(e)})
        entry.ok("applied " + ", ".join(sorted(changes)))
        return self._json(200, result)


class _ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class Server:
    """Start/stop wrapper. One per process."""

    def __init__(self, hooks: Hooks, port: int = 0):
        self._hooks = hooks
        self._want_port = port
        self._httpd: Optional[_ThreadingServer] = None
        self._thread: Optional[threading.Thread] = None
        self.ctx = _Ctx(hooks, secrets.token_urlsafe(32))

    @property
    def port(self) -> int:
        return self.ctx.port

    @property
    def token(self) -> str:
        return self.ctx.token

    @property
    def url(self) -> str:
        """The one-time link: hands the token to the browser, which swaps it for a cookie."""
        return f"http://127.0.0.1:{self.ctx.port}/?t={self.ctx.token}"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.ctx.port}"

    def start(self) -> "Server":
        if self._httpd is not None:
            return self
        self.ctx.commit = _git_commit()
        httpd = _ThreadingServer(("127.0.0.1", self._want_port), _Handler)
        httpd.ctx = self.ctx  # type: ignore[attr-defined]
        self.ctx.port = httpd.server_address[1]
        entry = ledger.begin("listen", self.base_url + "/",
                             "local API + browser view; token written to lockfile, "
                             "cookie or Bearer required, Host/Origin checked")
        try:
            os.makedirs(logging_setup.log_dir(), exist_ok=True)
            with ledger.begin("write", lockfile_path(), f"screecap:{os.getpid()}:{self.ctx.port}:<token>:http"):
                with open(lockfile_path(), "w", encoding="utf-8") as f:
                    f.write(f"screecap:{os.getpid()}:{self.ctx.port}:{self.ctx.token}:http")
        except OSError as e:
            entry.err(f"lockfile: {e}")
            httpd.server_close()
            raise
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="screecap-api",
                                        daemon=True)
        self._thread.start()
        entry.ok(f"pid={os.getpid()}")
        log.info("API listening on %s", self.base_url)
        return self

    def stop(self, timeout: float = 3.0):
        if self._httpd is None:
            return
        self.ctx.stopping.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        try:
            with ledger.begin("write", lockfile_path(), "remove (API stopped)"):
                os.unlink(lockfile_path())
        except OSError:
            pass
        ledger.note(self.base_url + "/", "API stopped")
