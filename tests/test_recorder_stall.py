"""Recorder against a frame source that blocks forever, and the ffmpeg
command shape. No camera, no ffmpeg: the source and Popen are faked.

Regression for 2026-10-10: dxcam sat in its output-recovery loop for 12 h,
run() never returned from get_latest_frame(), shutdown() never ran and the
MP4 had no moov atom. run() must come back on the watchdog or on
request_stop() even when the source call never returns.
"""
import os, sys, tempfile, time, threading, io
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="screecap_t_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

import logging
logging.getLogger().addHandler(logging.NullHandler())   # the stall path logs tracebacks on purpose

import numpy as np
import recorder
from recorder import Recorder


class BlockingSource:
    """Delivers `good` frames, then blocks forever (dxcam in output recovery)."""
    kind = "display"
    good = 3
    def __init__(self, *a, **k):
        self.calls = 0
        self.stop_calls = 0
        self._frame = np.zeros((4, 4, 3), np.uint8)
    def describe(self): return "fake"
    def detail(self): return "fake"
    def start(self): return 4, 4
    def first_frame(self): return self._frame
    def get_latest_frame(self):
        self.calls += 1
        if self.calls <= self.good:
            return self._frame
        threading.Event().wait()   # never returns, like the real thing did
    def stop(self):
        self.stop_calls += 1
        raise RuntimeError("Capture thread did not stop within timeout")


class FakeProc:
    """Enough of Popen for Recorder: a stdin to write, a stderr to pump."""
    last = None
    def __init__(self, cmd, **kw):
        FakeProc.last = self
        self.cmd = cmd
        self.stdin = io.BytesIO()
        r, w = os.pipe()
        self.stderr = os.fdopen(r, "rb")
        self._w = w
        self.returncode = None
        self.killed = False
    def wait(self, timeout=None):
        if self._w is not None:
            os.close(self._w); self._w = None
        self.returncode = 0
        return 0
    def kill(self):
        self.killed = True


recorder.capture.DisplaySource = BlockingSource
recorder.subprocess.Popen = FakeProc
tmp = os.environ["LOCALAPPDATA"]

# --- 1. watchdog ends a loop whose source never returns ---
r = Recorder(output=os.path.join(tmp, "a.mp4"), fps=60, encoder="libx264", stall_timeout=1.0)
r.start()
t0 = time.perf_counter(); r.run(); dt = time.perf_counter() - t0
check(dt < 5.0, f"run() returned {dt:.1f}s after the source blocked (stall_timeout=1 s)")
check(bool(r.error) and "stalled" in r.error, f"error names the stall: {r.error!r}")
check(r.frame_count >= 2, f"frames written before the stall: {r.frame_count}")
check(r._camera.stop_calls == 1, "watchdog tried to stop the source first")
r.shutdown()
check(FakeProc.last.stdin.closed, "shutdown closed ffmpeg stdin (ffmpeg finalizes the moov itself)")
check(FakeProc.last.returncode == 0 and not FakeProc.last.killed, "ffmpeg was waited on, never killed")

# --- 2. a user stop gets through the same way ---
BlockingSource.good = 0
r = Recorder(output=os.path.join(tmp, "b.mp4"), fps=60, encoder="libx264", stall_timeout=60.0)
r.start()
threading.Timer(0.3, r.request_stop).start()
t0 = time.perf_counter(); r.run(); dt = time.perf_counter() - t0
check(dt < 2.0, f"request_stop() returned run() in {dt:.1f}s while the source was blocked")
check(r.error is None, "no error on a user stop")
r.shutdown()

# --- 3. ffmpeg command shape: -framerate not -r, audio inputs before the pipe ---
BlockingSource.good = 0
r = Recorder(output=os.path.join(tmp, "c.mp4"), fps=60, encoder="libx264",
             audio_devices=["Mic A", "Mix B"], stall_timeout=60.0)
r.start(); r.request_stop(); r.run(); r.shutdown()
cmd = FakeProc.last.cmd
check("-framerate" in cmd and "-r" not in cmd,
      "-framerate sets the pipe time base; -r would discard wall-clock stamps")
check(cmd.index("rawvideo") < cmd.index("-use_wallclock_as_timestamps") < cmd.index("-"),
      "wall-clock stamps requested on the pipe input")
check(cmd.index("dshow") < cmd.index("rawvideo"), "dshow inputs precede the video pipe")
check(cmd[cmd.index("-map") + 1] == "2:v", "video mapped from the pipe (input N = number of audio devices)")
check(any("[0:a][1:a]amix=inputs=2" in a for a in cmd), "amix reads audio inputs 0..N-1")
check("-shortest" in cmd and "vfr" in cmd, "-shortest and -fps_mode vfr kept")

print("ALL OK" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
