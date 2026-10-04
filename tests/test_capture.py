import os, sys, tempfile, time, ctypes, subprocess
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="screecap_t_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

import capture, ledger, screens
from recorder import Recorder

def find_any_visible_window():
    """A visible top-level window with a title and a sane size (any app)."""
    import ctypes.wintypes as w
    u32 = ctypes.windll.user32
    found = []
    ENUM = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def cb(hwnd, lp):
        if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd): return True
        n = u32.GetWindowTextLengthW(hwnd)
        if not n: return True
        r = w.RECT(); u32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left >= 400 and r.bottom - r.top >= 300:
            buf = ctypes.create_unicode_buffer(n + 1); u32.GetWindowTextW(hwnd, buf, n + 1)
            found.append((hwnd, buf.value, (r.left, r.top, r.right, r.bottom)))
        return True
    u32.EnumWindows(ENUM(cb), 0)
    return found[0] if found else None

if os.name != "nt":
    print("     (Windows only; skipped)"); sys.exit(0)

win = find_any_visible_window()
check(win is not None, f"found a visible window to capture: {win[1] if win else None}")
if win:
    src = capture.WindowSource(win[0], fps=30, title=win[1])
    w, h = src.start()
    check(w > 0 and h > 0, f"WindowSource.start -> {w}x{h}")
    t0 = time.perf_counter(); n = 0; shapes = set()
    while n < 15:
        f = src.get_latest_frame()
        if f is None: break
        shapes.add(f.shape); n += 1
    dt = time.perf_counter() - t0
    src.stop()
    check(n == 15 and shapes == {(h, w, 3)}, f"15 BGR frames of fixed size {shapes}")
    check(0.35 <= dt <= 1.2, f"paced to ~30 fps: 15 frames in {dt:.2f}s")
    check(src.get_latest_frame() is None, "after stop, get_latest_frame is None")
    # fit: a frame of another size is cropped/padded to the first size
    import numpy as np
    src2 = capture.WindowSource(win[0], fps=30); src2._size = (100, 50)
    out = src2._fit(np.ones((80, 120, 3), dtype=np.uint8))
    check(out.shape == (50, 100, 3) and src2.resized == 1, "resized frames are fitted to the fixed size")

    # end-to-end: 1.5 s recording of that window through the Recorder + ffmpeg
    outp = os.path.join(os.environ["LOCALAPPDATA"], "win.mp4")
    rec = Recorder(output=outp, fps=30, encoder="libx264", duration=1.5, window_hwnd=win[0], window_title=win[1])
    try:
        rec.start(); rec.run(); rec.shutdown()
        ok_file = os.path.exists(outp) and os.path.getsize(outp) > 10_000
        check(ok_file and rec.error is None and rec.frame_count >= 30, f"recorded {rec.frame_count} frames -> {os.path.getsize(outp) if ok_file else 0} B, error={rec.error}")
        lines = ledger.tail(20)
        check(any(r["kind"] == "read" and r["target"].startswith("window:") and "Windows Graphics Capture" in r["detail"] for r in lines), "ledger announces the window read")
    except Exception as e:
        check(False, f"recorder end-to-end raised: {e!r}")

# DisplaySource still works (first frame + a few more)
outs = screens.enumerate_outputs()
if outs:
    d = capture.DisplaySource(outs[0].idx, fps=30)
    w, h = d.start(); f = d.get_latest_frame(); d.stop()
    check(f is not None and f.shape == (h, w, 3), f"DisplaySource {w}x{h} BGR frame")
print("\n%d failures" % len(fails)); sys.exit(1 if fails else 0)
