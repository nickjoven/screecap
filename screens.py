"""Monitor identity: which physical display is which, and where the game is.

DXGI output *indices* are not stable. Their order changed between two runs on
the same machine with the same two monitors (primary-first in one process,
top-to-bottom in the next), so "monitor 1" silently became the other screen
and a whole evening of recordings captured the desktop. Identify outputs by
their device name and desktop coordinates instead, and resolve to an index
only at the moment of capture.

A monitor spec is a string:
    "auto"              the monitor the League game window is on; else primary
    "primary"           the primary display
    "\\\\.\\DISPLAY2"   a Windows device name (stable per physical connector)
    "1"                 a bare DXGI index (legacy; discouraged, logged as such)
"""
from __future__ import annotations

import ctypes
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

AUTO = "auto"
PRIMARY = "primary"

# The in-game client window (not the launcher). Class is the reliable key;
# the title is the fallback for builds that change the class.
GAME_WINDOW_CLASS = "RiotWindowClass"
GAME_WINDOW_TITLE_PREFIX = "League of Legends (TM) Client"


@dataclass(frozen=True)
class Output:
    idx: int            # dxcam output index *right now*
    device: str         # e.g. \\.\DISPLAY2
    left: int
    top: int
    right: int
    bottom: int
    primary: bool

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def short(self) -> str:
        return self.device.rsplit("\\", 1)[-1]

    def describe(self) -> str:
        return (f"{self.device} ({self.left},{self.top})-({self.right},{self.bottom})"
                f"{' primary' if self.primary else ''}")

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


# ----------------------------------------------------------------------------
# Enumeration
# ----------------------------------------------------------------------------

def _primary_device() -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        import ctypes.wintypes as w
        u32 = ctypes.windll.user32

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT),
                        ("dwFlags", w.DWORD), ("szDevice", w.WCHAR * 32)]

        hm = u32.MonitorFromPoint(w.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if u32.GetMonitorInfoW(hm, ctypes.byref(mi)):
            return mi.szDevice
    except Exception:
        log.debug("primary monitor lookup failed", exc_info=True)
    return None


def enumerate_outputs() -> List[Output]:
    """Every DXGI output on the first adapter, with its identity. [] if dxcam is unavailable."""
    try:
        import dxcam
        from dxcam import __factory as factory
    except Exception:
        return []
    primary = _primary_device()
    outs: List[Output] = []
    try:
        for di, _dev in enumerate(factory.devices):
            if di != 0:
                break  # dxcam's create(output_idx=) indexes the first device
            for oi, out in enumerate(factory.outputs[di]):
                d = out.desc.DesktopCoordinates
                device = str(out.desc.DeviceName)
                outs.append(Output(oi, device, d.left, d.top, d.right, d.bottom,
                                   primary=(device == primary) if primary else (d.left == 0 and d.top == 0)))
    except Exception:
        log.exception("DXGI output enumeration failed")
        return []
    return outs


def describe_index(idx: int) -> str:
    """Identity string for a dxcam index, for logs and the ledger."""
    for o in enumerate_outputs():
        if o.idx == idx:
            return o.describe()
    return f"index {idx} (identity unknown)"


# ----------------------------------------------------------------------------
# Game window
# ----------------------------------------------------------------------------

def find_game_window() -> Optional[Tuple[int, str, Tuple[int, int, int, int]]]:
    """(hwnd, title, (l, t, r, b)) of the visible League game window, or None."""
    if os.name != "nt":
        return None
    try:
        import ctypes.wintypes as w
        u32 = ctypes.windll.user32
        found: list = []
        ENUM = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        def cb(hwnd, _lp):
            if not u32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            u32.GetClassNameW(hwnd, cls, 64)
            n = u32.GetWindowTextLengthW(hwnd)
            title = ""
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value
            if cls.value == GAME_WINDOW_CLASS or title.startswith(GAME_WINDOW_TITLE_PREFIX):
                r = w.RECT()
                u32.GetWindowRect(hwnd, ctypes.byref(r))
                found.append((hwnd, title, (r.left, r.top, r.right, r.bottom)))
                return False
            return True

        u32.EnumWindows(ENUM(cb), 0)
        return found[0] if found else None
    except Exception:
        log.debug("game window lookup failed", exc_info=True)
        return None


# ----------------------------------------------------------------------------
# Resolution
# ----------------------------------------------------------------------------

def output_for_point(x: int, y: int, outputs: List[Output]) -> Optional[Output]:
    for o in outputs:
        if o.contains(x, y):
            return o
    return None


def output_for_rect(rect: Tuple[int, int, int, int], outputs: List[Output]) -> Optional[Output]:
    """The output holding the rect's centre (a borderless game window may overhang by a pixel)."""
    l, t, r, b = rect
    return output_for_point((l + r) // 2, (t + b) // 2, outputs)


def output_for_device(device: str, outputs: List[Output]) -> Optional[Output]:
    want = device.rsplit("\\", 1)[-1].upper()
    for o in outputs:
        if o.short.upper() == want:
            return o
    return None


def primary_output(outputs: List[Output]) -> Optional[Output]:
    for o in outputs:
        if o.primary:
            return o
    return outputs[0] if outputs else None


def resolve(spec: str, outputs: Optional[List[Output]] = None,
            game_window=None) -> Tuple[Optional[Output], str]:
    """Turn a monitor spec into a concrete Output plus a one-line reason.

    `game_window` lets callers pass a pre-fetched find_game_window() result
    (or a stub in tests); None means look it up for "auto".
    """
    outputs = enumerate_outputs() if outputs is None else outputs
    if not outputs:
        return None, "no DXGI outputs enumerated"
    spec = (spec or AUTO).strip()
    if spec == AUTO:
        gw = find_game_window() if game_window is None else game_window
        if gw:
            o = output_for_rect(gw[2], outputs)
            if o:
                return o, f"auto: League game window {gw[2]} is on {o.describe()}"
            p = primary_output(outputs)
            return p, f"auto: game window {gw[2]} is on no known output; using primary {p.describe()}"
        p = primary_output(outputs)
        return p, f"auto: no League game window; using primary {p.describe()}"
    if spec == PRIMARY:
        p = primary_output(outputs)
        return p, f"primary {p.describe()}"
    if spec.lstrip("-").isdigit():
        idx = int(spec)
        for o in outputs:
            if o.idx == idx:
                return o, f"legacy index {idx} -> {o.describe()} (indices are not stable; prefer a device name)"
        return None, f"no DXGI output at index {idx}"
    o = output_for_device(spec, outputs)
    if o:
        return o, o.describe()
    return None, f"no display named {spec}; have {', '.join(x.short for x in outputs)}"


def spec_for_output(o: Output) -> str:
    return o.device


def qt_screen_for_output(o: Output, screens) -> Optional[object]:
    """The QScreen whose top-left matches the output (Qt and DXGI orders differ)."""
    best = None
    best_d = None
    for s in screens:
        g = s.geometry()
        try:
            dpr = float(s.devicePixelRatio())
        except Exception:
            dpr = 1.0
        # Qt reports logical coordinates; physical = logical * dpr for the
        # top-left on the common 100%/uniform-scaling layouts.
        d = abs(g.x() * dpr - o.left) + abs(g.y() * dpr - o.top)
        if best_d is None or d < best_d:
            best, best_d = s, d
    return best
