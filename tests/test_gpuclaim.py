import os, sys, tempfile, threading, time, types
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="screecap_t_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

import ctypes
import gpuclaim, ledger

# A throwaway name: never touch the box's real Global\homeserv-gpu-recording here.
NAME = f"Global\\screecap-test-{os.getpid()}"
k = ctypes.WinDLL("kernel32")
k.CreateMutexW.restype = ctypes.c_void_p
k.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
k.ReleaseMutex.argtypes = k.CloseHandle.argtypes = (ctypes.c_void_p,)

def probe():
    """What gpu-run's holder does: take for an instant. True = someone holds it."""
    res = {}
    def t():
        h = k.CreateMutexW(None, False, NAME)
        rc = k.WaitForSingleObject(h, 0)
        res["held"] = rc == 0x102
        if rc in (0, 0x80): k.ReleaseMutex(h)
        k.CloseHandle(h)
    th = threading.Thread(target=t); th.start(); th.join()
    return res["held"]

# ---- claim / release on one thread --------------------------------------
c = gpuclaim.RecordingClaim(NAME)
check(not probe(), "free before the claim")
check(c.claim() and c.held, "claim takes the mutex")
check(probe(), "a probe from another thread sees it held")
rows = ledger.tail(2)
check(rows[0]["kind"] == "note" and rows[0]["target"] == NAME and rows[1]["kind"] == "ok" and rows[1]["detail"] == "held",
      f"claim announced then answered: {[(r['kind'], r['detail']) for r in rows]}")
c.release()
check(not c.held and not probe(), "release frees it")
check(ledger.tail(2)[0]["detail"].startswith("ReleaseMutex"), "release announced")
c.release()  # idempotent

# ---- a second recorder times out without raising -------------------------
a, b = gpuclaim.RecordingClaim(NAME), gpuclaim.RecordingClaim(NAME)
ready, done = threading.Event(), threading.Event()
def holder():
    a.claim(); ready.set(); done.wait(); a.release()
th = threading.Thread(target=holder); th.start(); ready.wait()
t0 = time.time()
got = b.claim()
check(not got and not b.held and 1.5 < time.time() - t0 < 4, "a held mutex: claim waits ~2 s, then gives up")
check(ledger.tail(1)[0]["kind"] == "err" and "timed out" in ledger.tail(1)[0]["detail"], "the timeout is a ledger err")
done.set(); th.join()

# ---- a dead holder's mutex comes back abandoned, and is taken -------------
dead = gpuclaim.RecordingClaim(NAME)
th = threading.Thread(target=dead.claim); th.start(); th.join()  # thread exits holding it
c = gpuclaim.RecordingClaim(NAME)
check(c.claim() and "recovered" in ledger.tail(1)[0]["detail"], "abandoned by a dead thread: recovered")
c.release()

# ---- Recorder wiring ------------------------------------------------------
from recorder import Recorder
r = Recorder.__new__(Recorder)
r._gpu_claim = types.SimpleNamespace(released=False)
r._gpu_claim.release = lambda: setattr(r._gpu_claim, "released", True)
r._camera = r._proc = r._watchdog_thread = r._frame_thread = r._stderr_thread = None
r._stop = threading.Event()
r.output = os.path.join(tempfile.mkdtemp(), "none.mp4")
r.shutdown()
check(r._gpu_claim.released, "Recorder.shutdown releases the claim")

print("FAILED" if fails else "ALL OK")
sys.exit(1 if fails else 0)
