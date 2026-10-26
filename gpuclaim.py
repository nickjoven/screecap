"""Claim the box's GPU for the user while a recording runs.

Batch GPU jobs on this box (lemma training under homeserv's `gpu-run`) watch
the named mutex `Global\\homeserv-gpu-recording`: while screecap holds it they
checkpoint and yield, and they refuse to start. See homeserv docs/GPU.md.

The claim never blocks or fails a recording. It waits at most 2 s (never 0:
gpu-run probes the mutex by taking it for an instant), and a claim that times
out or errors is logged and the recording goes on without it. The mutex is
owned by the thread that took it, so claim() and release() must run on the
same thread: Recorder.start() and Recorder.shutdown() both run on the capture
thread. If screecap dies holding it, Windows releases it (abandoned), so it
never goes stale.
"""
import logging
import sys

import ledger

log = logging.getLogger(__name__)

NAME = "Global\\homeserv-gpu-recording"
WAIT_MS = 2000
_WAIT_OBJECT_0, _WAIT_ABANDONED, _WAIT_TIMEOUT = 0x0, 0x80, 0x102


class RecordingClaim:
    """claim() once when capture is live, release() once on teardown. Never raises."""

    def __init__(self, name: str = NAME):
        self.name = name
        self._h = None
        self.held = False

    def claim(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateMutexW.restype = wintypes.HANDLE
            k.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            k.WaitForSingleObject.restype = wintypes.DWORD
            k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            with ledger.begin("note", self.name,
                              f"CreateMutexW + WaitForSingleObject({WAIT_MS} ms): "
                              "batch GPU jobs on this box yield while it is held") as e:
                h = k.CreateMutexW(None, False, self.name)
                if not h:
                    e.err(f"CreateMutexW failed: winerror {ctypes.get_last_error()}")
                    return False
                rc = k.WaitForSingleObject(h, WAIT_MS)
                if rc in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
                    self._h, self.held = h, True
                    e.ok("held" + (" (previous holder died; recovered)" if rc == _WAIT_ABANDONED else ""))
                    return True
                k.CloseHandle(h)
                e.err("timed out: another recorder holds it; recording without a claim"
                      if rc == _WAIT_TIMEOUT else f"WaitForSingleObject returned {rc:#x}")
                return False
        except Exception:
            log.exception("GPU recording claim failed; recording without it")
            return False

    def release(self) -> None:
        if not self.held:
            return
        try:
            import ctypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            with ledger.begin("note", self.name, "ReleaseMutex: batch GPU jobs may run again") as e:
                if not k.ReleaseMutex(self._h):
                    e.err(f"ReleaseMutex failed: winerror {ctypes.get_last_error()} "
                          "(wrong thread?); closing the handle, Windows will mark it abandoned")
                k.CloseHandle(self._h)
        except Exception:
            log.exception("GPU recording release failed")
        finally:
            self._h, self.held = None, False
