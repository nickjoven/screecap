"""Frame sources for the recorder: a whole display (DXGI) or one window (WGC).

Both yield BGR uint8 frames of a fixed size at the recorder's pace:

    src = DisplaySource(monitor_idx, fps, region) | WindowSource(hwnd, fps)
    w, h = src.start()          # first frame grabbed; size fixed from here on
    frame = src.get_latest_frame()   # (h, w, 3) BGR, or None once the source is gone
    src.stop()

DisplaySource wraps dxcam (Desktop Duplication): everything on that output,
no OS cursor (duplication frames never contain the pointer).

WindowSource wraps Windows Graphics Capture through the `windows-capture`
package: the window's own composited surface, so other windows on top of it
never appear, the desktop never appears, and the cursor is off. This is what
"record the game, not the screen" means. Frames arrive on a callback thread
only when the content changes; get_latest_frame() paces to `fps` and repeats
the last frame when nothing new arrived, matching dxcam's video_mode.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

# DXGI "the device you are holding is dead" codes. A GPU driver update or a
# TDR (driver reset) invalidates every Direct3D device in every running
# process; dxcam creates its device once at import time inside a module-level
# singleton and never recreates it, so from then on every create()/grab()
# fails with one of these forever. 2026-10-13: the NVIDIA driver was
# reinstalled while the tray app was running; every F9 after that failed with
# 0x887A0005 until the process was restarted.
DXGI_ERROR_DEVICE_REMOVED = 0x887A0005
DXGI_ERROR_DEVICE_RESET = 0x887A0007


def hresult_of(exc: BaseException) -> int:
    """Unsigned HRESULT carried by a COMError (or 0 if there is none)."""
    h = getattr(exc, "hresult", None)
    if h is None and getattr(exc, "args", None):
        h = exc.args[0]
    try:
        return int(h) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def is_device_removed(exc: BaseException) -> bool:
    return hresult_of(exc) in (DXGI_ERROR_DEVICE_REMOVED, DXGI_ERROR_DEVICE_RESET)


def reset_dxcam() -> None:
    """Throw away dxcam's cached D3D11 device(s) and cameras; build fresh ones.

    dxcam keeps one DXFactory per process (a Singleton metaclass instance
    stored as the module global `__factory`); the factory owns the Device
    objects whose D3D11CreateDevice ran at import. Nothing in dxcam rebuilds
    them, so after a driver reset the only cure was restarting the process.
    screens.enumerate_outputs() re-imports `__factory` on every call, so it
    sees the new one.
    """
    import dxcam
    old = getattr(dxcam, "__factory", None)
    if old is not None:
        try:
            old.clean_up()
        except Exception:
            log.exception("releasing dxcam cameras on the dead device raised (ignored)")
    dxcam.Singleton._instances.pop(dxcam.DXFactory, None)
    dxcam.DXFactory._camera_instances.clear()
    setattr(dxcam, "__factory", dxcam.DXFactory())
    log.warning("dxcam factory rebuilt: new D3D11 device after a DXGI device-removed error")


class DisplaySource:
    kind = "display"

    def __init__(self, monitor: int, fps: int, region: Optional[Tuple[int, int, int, int]] = None):
        self.monitor = monitor
        self.fps = fps
        self.region = tuple(region) if region else None
        self._camera = None

    def describe(self) -> str:
        import screens
        return f"screen:{screens.describe_index(self.monitor)}"

    def detail(self) -> str:
        return (f"dxcam output {self.monitor}, DXGI Desktop Duplication, {self.fps} fps, "
                f"region={self.region or 'full'}")

    def start(self) -> Tuple[int, int]:
        try:
            return self._open()
        except Exception as e:
            if not is_device_removed(e):
                raise
            log.warning(
                "DXGI device removed (0x%08X) opening output %d: the GPU driver was "
                "reset or updated since dxcam initialised; rebuilding dxcam and "
                "retrying once", hresult_of(e), self.monitor,
            )
            self.stop()
            self._camera = None
            reset_dxcam()
            return self._open()

    def _open(self) -> Tuple[int, int]:
        import dxcam
        cam = dxcam.create(output_idx=self.monitor, output_color="BGR")
        if cam is None:
            raise RuntimeError(f"No DXGI output at monitor index {self.monitor}")
        self._camera = cam
        # video_mode=True is required: without it dxcam skips unchanged frames
        # and the MP4's timing breaks.
        cam.start(target_fps=self.fps, video_mode=True, region=self.region)
        first = cam.get_latest_frame()
        self._first = first
        h, w = first.shape[:2]
        return w, h

    def first_frame(self) -> np.ndarray:
        return self._first

    def get_latest_frame(self) -> Optional[np.ndarray]:
        return self._camera.get_latest_frame() if self._camera is not None else None

    def stop(self) -> None:
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                log.exception("dxcam stop raised")


class WindowSource:
    kind = "window"

    def __init__(self, hwnd: int, fps: int, title: str = "", first_frame_timeout: float = 8.0):
        self.hwnd = int(hwnd)
        self.fps = fps
        self.title = title
        self.first_frame_timeout = first_frame_timeout
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._new = threading.Event()
        self._closed = threading.Event()
        self._control = None
        self._size: Optional[Tuple[int, int]] = None
        self._next_tick = 0.0
        self._first = None
        self.resized = 0   # frames that arrived in a different size and were fitted

    def describe(self) -> str:
        return f"window:{self.title or self.hwnd}"

    def detail(self) -> str:
        return (f"hwnd={self.hwnd}, Windows Graphics Capture of this window only "
                f"(other windows and the desktop are never in frame), cursor off, {self.fps} fps")

    def start(self) -> Tuple[int, int]:
        try:
            import windows_capture as wc
        except ImportError as e:
            raise RuntimeError("windows-capture is not installed (pip install windows-capture)") from e
        cap = wc.WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=self.hwnd)

        @cap.event
        def on_frame_arrived(frame, _control):
            fb = frame.frame_buffer  # (h, w, 4) BGRA, owned by the callback
            bgr = np.ascontiguousarray(fb[:, :, :3])
            with self._lock:
                self._latest = bgr
            self._new.set()

        @cap.event
        def on_closed():
            self._closed.set()
            self._new.set()

        self._control = cap.start_free_threaded()
        if not self._new.wait(self.first_frame_timeout) or self._closed.is_set():
            self.stop()
            raise RuntimeError(f"no frame from window {self.hwnd} within {self.first_frame_timeout:.0f}s")
        with self._lock:
            first = self._latest
        h, w = first.shape[:2]
        # yuv420p encoders need even dimensions; a window can be any size.
        # Fix the output size to the even floor and fit every frame to it.
        self._size = (w - w % 2, h - h % 2)
        self._first = self._fit(first)
        self.resized = 0
        self._next_tick = time.perf_counter()
        return self._size

    def first_frame(self) -> np.ndarray:
        return self._first

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        """Crop/pad a frame of another size to the fixed output size (window was resized)."""
        w, h = self._size
        fh, fw = frame.shape[:2]
        if (fw, fh) == (w, h):
            return frame
        self.resized += 1
        out = np.zeros((h, w, 3), dtype=np.uint8)
        out[:min(h, fh), :min(w, fw)] = frame[:min(h, fh), :min(w, fw)]
        return out

    def get_latest_frame(self) -> Optional[np.ndarray]:
        if self._closed.is_set():
            return None
        # Pace to fps: sleep until the next tick, then hand out whatever is
        # newest (a repeat if the window did not redraw).
        self._next_tick += 1.0 / self.fps
        delay = self._next_tick - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            self._next_tick = time.perf_counter()
        self._new.clear()
        with self._lock:
            frame = self._latest
        if frame is None or self._closed.is_set():
            return None
        return self._fit(frame)

    def stop(self) -> None:
        self._closed.set()
        self._new.set()
        if self._control is not None:
            try:
                self._control.stop()
            except Exception:
                log.exception("WGC stop raised")
            self._control = None
