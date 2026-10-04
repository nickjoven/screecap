"""screecap command line: the terminal is a reader of the ledger, and a launcher.

    screecap                  open the browser view (starting screecap if needed), then watch
    screecap open             open the browser view only
    screecap watch            terminal view: the live ledger, every action as it happens
    screecap status           what is running, what it is doing, what it touches
    screecap ledger [-n N]    last N ledger lines
    screecap gui              show the window (start it if needed)
    screecap quit             stop the tray app (refused while recording)
    screecap restart          quit, start again, reopen the browser view
    screecap record [...]     the recorder CLI (record.py); any unknown first
                              argument is passed through, so `screecap --duration 10`
                              still records.

Talks to the running instance over its localhost API (see server.py); the
lockfile in %LOCALAPPDATA%\\screecap says where. With no instance running,
`watch` and `ledger` read the ledger file directly.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from typing import Dict, Optional

import ledger
import logging_setup
import server

PROJECT_DIR = ledger.PROJECT_DIR
SUBCOMMANDS = ("open", "watch", "status", "ledger", "gui", "quit", "restart", "record")

# -- colour --------------------------------------------------------------------

_COLORS = {
    "exec": "\x1b[36m", "http": "\x1b[34m", "ws": "\x1b[34m", "listen": "\x1b[35m",
    "read": "\x1b[90m", "write": "\x1b[33m", "api": "\x1b[32m", "note": "\x1b[90m",
    "ok": "\x1b[2;32m", "err": "\x1b[1;31m",
}
_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_USE_COLOR = True


def _enable_vt() -> None:
    """Turn on ANSI escapes in the Windows console (no-op elsewhere)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def _c(kind: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"{_COLORS.get(kind, '')}{text}{_RESET}"


def _local_time(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    except ValueError:
        return ts[11:23]


def format_entry(rec: Dict) -> str:
    kind = rec["kind"]
    target = rec["target"]
    if kind in ("ok", "err"):
        target = "↳ " + target
    line = (f"{_DIM if _USE_COLOR else ''}{_local_time(rec['ts'])}{_RESET if _USE_COLOR else ''} "
            f"{rec['seq']:>6}  {_c(kind, f'{kind:<6}')} {target}")
    if rec["detail"]:
        line += f"  {rec['detail']}"
    if rec["src"] and rec["src"] != "?":
        line += f"  {_DIM if _USE_COLOR else ''}({rec['src']}){_RESET if _USE_COLOR else ''}"
    return line


# -- talking to the running instance -------------------------------------------

def _request(info: Dict, path: str, method: str = "GET", timeout: float = 3.0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}", method=method,
        headers={"Authorization": f"Bearer {info['token']}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def running() -> Optional[Dict]:
    """Lockfile info + live status of the running instance, or None."""
    info = server.read_lockfile()
    if info is None:
        return None
    try:
        info["status"] = _request(info, "/api/status", timeout=1.5)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return info


def _console_python() -> str:
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


def _windowed_python() -> str:
    exe = sys.executable
    if os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def _spawn_detached(cmd, cwd=None) -> subprocess.Popen:
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, cwd=cwd or PROJECT_DIR, creationflags=flags,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True)


def ensure_running(timeout: float = 25.0, minimized: bool = True) -> Dict:
    info = running()
    if info is not None:
        return info
    cmd = [_windowed_python(), os.path.join(PROJECT_DIR, "gui.py")]
    if minimized:
        cmd.append("--minimized")
    entry = ledger.begin("exec", os.path.basename(cmd[0]), subprocess.list2cmdline(cmd))
    try:
        proc = _spawn_detached(cmd)
    except OSError as e:
        entry.err(str(e))
        sys.exit(f"could not start screecap: {e}")
    entry.ok(f"pid={proc.pid}, waiting for its API")
    print("starting screecap...", end="", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        print(".", end="", flush=True)
        info = running()
        if info is not None:
            print()
            return info
        if proc.poll() is not None:
            # The new process handed off to an already-running instance and
            # exited. That instance has no lockfile, so it predates the API.
            print()
            sys.exit("an older screecap is already running without the local API. "
                     "Quit it from the tray icon and run this again.")
    print()
    sys.exit("screecap started but its API did not come up; see "
             f"{logging_setup.log_path()}")


def launch_terminal_watch() -> str:
    """Open a terminal running `screecap watch`. Used by the GUI/browser 'Open terminal' button."""
    py = _console_python()
    watch = [py, os.path.join(PROJECT_DIR, "cli.py"), "watch"]
    wt = shutil.which("wt.exe") if os.name == "nt" else None
    if wt:
        cmd = [wt, "-w", "0", "new-tab", "--title", "screecap watch", "-d", PROJECT_DIR, *watch]
    else:
        cmd = watch
    with ledger.begin("exec", os.path.basename(cmd[0]), subprocess.list2cmdline(cmd)) as e:
        if wt:
            subprocess.Popen(cmd, cwd=PROJECT_DIR)
            e.ok("Windows Terminal tab")
            return "opened a Windows Terminal tab"
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        subprocess.Popen(cmd, cwd=PROJECT_DIR, creationflags=flags)
        e.ok("new console")
        return "opened a console window"


# -- commands -------------------------------------------------------------------

def _print_manifest():
    print(_c("note", "screecap touches exactly this, and announces each action below before it runs:"))
    for line in ledger.manifest_lines():
        print(_c("note", "  " + line))
    print(_c("note", f"  ledger file: {ledger.path()}"))
    print()


def cmd_open(args) -> int:
    info = ensure_running()
    base = f"http://127.0.0.1:{info['port']}"
    ledger.begin("exec", "browser", f"{base}/?t=<token>  (one-time link; the token becomes a cookie)").ok()
    webbrowser.open(f"{base}/?t={info['token']}")
    print(f"opened {base}/ in your browser")
    if getattr(args, "then_watch", False):
        return cmd_watch(args)
    return 0


def _stream(info: Dict, last_seq: int) -> int:
    """Print live entries over SSE until the connection drops. Returns the last seq seen."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/api/ledger/stream?since={last_seq}",
        headers={"Authorization": f"Bearer {info['token']}", "Accept": "text/event-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                try:
                    rec = json.loads(line[6:])
                except ValueError:
                    continue
                if rec["seq"] <= last_seq:
                    continue
                last_seq = rec["seq"]
                print(format_entry(rec), flush=True)
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return last_seq


def _tail_file(last_seq: int) -> int:
    """Follow the ledger file until a running instance appears. Returns the last seq seen."""
    p = ledger.path()
    pos = 0
    try:
        pos = os.path.getsize(p)
    except OSError:
        pass
    checks = 0
    while True:
        time.sleep(0.5)
        checks += 1
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        if size < pos:
            pos = 0  # rotated
        if size > pos:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                for line in f:
                    rec = ledger.parse_line(line)
                    if rec and rec["seq"] > last_seq:
                        last_seq = rec["seq"]
                        print(format_entry(rec), flush=True)
                pos = f.tell()
        if checks % 4 == 0 and running() is not None:
            return last_seq


def cmd_watch(args) -> int:
    _print_manifest()
    last_seq = 0
    for rec in ledger.tail(getattr(args, "lines", 30) or 30):
        print(format_entry(rec))
        last_seq = rec["seq"]
    try:
        while True:
            info = running()
            if info is not None:
                print(_c("note", f"-- live from screecap pid {info['pid']} on 127.0.0.1:{info['port']} --"))
                last_seq = _stream(info, last_seq)
                print(_c("note", "-- connection closed; following the ledger file --"))
            else:
                print(_c("note", "-- screecap is not running; following the ledger file --"))
                last_seq = _tail_file(last_seq)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_status(args) -> int:
    info = running()
    if info is None:
        print("screecap is not running.")
        print(f"ledger: {ledger.path()}")
        for rec in ledger.tail(5):
            print("  " + format_entry(rec))
        print()
        _print_manifest()
        return 1
    st = info["status"]
    rec_state = "RECORDING" if st.get("recording") else "idle"
    print(f"screecap pid {info['pid']}  API http://127.0.0.1:{info['port']}/  "
          f"up {int(st.get('now', 0) - st.get('started', 0))}s  v{st.get('version')}")
    print(f"  recorder:  {rec_state}" + (
        f"  {st.get('output')}  {st.get('frames')} frames  {st.get('elapsed', 0):.0f}s  "
        f"{st.get('avg_fps', 0):.1f} fps" if st.get("recording") else ""))
    print(f"  league:    {'on' if st.get('league') else 'off'}"
          f"  client {'connected' if st.get('lcu_connected') else 'not connected'}"
          f"  phase {st.get('phase') or '-'}"
          f"  auto-record {'on' if st.get('auto_record') else 'off'}")
    print(f"  data:      {st.get('data_dir')}")
    print(f"  ledger:    {ledger.path()}  (seq {st.get('ledger_seq')})")
    print()
    _print_manifest()
    return 0


def cmd_ledger(args) -> int:
    for rec in ledger.tail(args.lines):
        print(format_entry(rec))
    return 0


def cmd_gui(args) -> int:
    info = running()
    if info is not None:
        _request(info, "/api/action/show", method="POST")
        print("raised the screecap window")
        return 0
    ensure_running(minimized=False)
    return 0


def cmd_quit(args) -> int:
    info = running()
    if info is None:
        print("screecap is not running")
        return 0
    try:
        res = _request(info, "/api/action/quit", method="POST")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("the running screecap predates `quit` support: Quit it from the tray icon, "
                  "then run `screecap`")
            return 1
        raise
    result = (res or {}).get("result", "")
    print(result or "quit requested")
    if result.startswith("refused"):
        return 1
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and running() is not None:
        time.sleep(0.3)
    return 0 if running() is None else 1


def cmd_restart(args) -> int:
    rc = cmd_quit(args)
    if rc != 0 and running() is not None:
        return rc
    args.then_watch = False
    return cmd_open(args)


def cmd_record(rest) -> int:
    import record
    sys.argv = [os.path.join(PROJECT_DIR, "record.py"), *rest]
    record.main()
    return 0


def main(argv=None) -> int:
    global _USE_COLOR
    argv = list(sys.argv[1:] if argv is None else argv)
    logging_setup.configure()
    _enable_vt()
    if "--no-color" in argv:
        argv.remove("--no-color")
        _USE_COLOR = False
    if not sys.stdout.isatty():
        _USE_COLOR = False
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # Pass-through: an unknown first argument means "record with these flags".
    if argv and argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        return cmd_record(argv)
    if argv and argv[0] == "record":
        return cmd_record(argv[1:])

    p = argparse.ArgumentParser(prog="screecap", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("open", help="open the browser view (start screecap if needed)")
    w = sub.add_parser("watch", help="live ledger in this terminal")
    w.add_argument("-n", "--lines", type=int, default=30, help="history lines to show first")
    sub.add_parser("status", help="what is running and what it touches")
    lg = sub.add_parser("ledger", help="print the last N ledger lines")
    lg.add_argument("-n", "--lines", type=int, default=50)
    sub.add_parser("gui", help="show the window (start it if needed)")
    sub.add_parser("quit", help="stop the tray app (refused while recording)")
    sub.add_parser("restart", help="quit, start again, reopen the browser view")
    sub.add_parser("record", help="record (see record.py --help)")
    args = p.parse_args(argv)

    if args.cmd is None:
        args.then_watch = True
        return cmd_open(args)
    return {"open": cmd_open, "watch": cmd_watch, "status": cmd_status,
            "ledger": cmd_ledger, "gui": cmd_gui, "quit": cmd_quit,
            "restart": cmd_restart}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
