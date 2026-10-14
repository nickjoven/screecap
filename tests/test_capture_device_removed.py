"""DisplaySource against a dxcam whose D3D11 device has been removed.

Regression for 2026-10-13: the NVIDIA driver was reinstalled while the tray
app ran; dxcam kept its dead device and every F9 failed with 0x887A0005 for
two days. No real dxcam here: a fake module stands in, raising a genuine
comtypes COMError on the first attempt.
"""
import os, sys, tempfile, types, logging
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="screecap_t_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.getLogger().addHandler(logging.NullHandler())
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

import numpy as np
from _ctypes import COMError
import capture

DEVICE_REMOVED = -2005270523   # 0x887A0005 as comtypes reports it (signed)

class FakeCam:
    is_released = False
    def __init__(self): self.started = False; self.stopped = False
    def start(self, **kw): self.started = True
    def get_latest_frame(self): return np.zeros((4, 6, 3), np.uint8)
    def stop(self): self.stopped = True
    def release(self): pass

class FakeSingleton(type):
    """Same contract as dxcam.Singleton: one instance per class, cached here.
    reset_dxcam() must evict the entry or DXFactory() hands back the dead one."""
    _instances = {}
    def __call__(cls, *a, **k):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*a, **k)
        return cls._instances[cls]

class FakeFactory(metaclass=FakeSingleton):
    built = 0
    def __init__(self): FakeFactory.built += 1; self.cleaned = 0
    def clean_up(self): self.cleaned += 1

def make_fake_dxcam(failures):
    """A dxcam whose create() raises device-removed `failures` times, then works."""
    m = types.ModuleType("dxcam")
    state = {"left": failures, "cams": []}
    def create(output_idx=0, output_color="RGB"):
        if state["left"] > 0:
            state["left"] -= 1
            raise COMError(DEVICE_REMOVED, "The GPU device instance has been suspended.", (None, None, None, 0, None))
        cam = FakeCam(); state["cams"].append(cam); return cam
    m.create = create
    m.Singleton = FakeSingleton
    m.DXFactory = FakeFactory
    FakeFactory._camera_instances = {}
    FakeSingleton._instances.clear()
    setattr(m, "__factory", FakeFactory())
    m._state = state
    return m

# --- hresult helpers ---
e = COMError(DEVICE_REMOVED, "x", (None,) * 5)
check(capture.hresult_of(e) == 0x887A0005, "hresult_of reads the signed COMError hresult as 0x887A0005")
check(capture.is_device_removed(e), "0x887A0005 is device-removed")
check(capture.is_device_removed(COMError(-2005270521, "x", (None,) * 5)), "0x887A0007 (device reset) is device-removed too")
check(not capture.is_device_removed(RuntimeError("no")), "ordinary exceptions are not")

# --- 1. dead device once: rebuild + retry succeeds ---
fake = make_fake_dxcam(failures=1); sys.modules["dxcam"] = fake
old_factory = getattr(fake, "__factory")
src = capture.DisplaySource(0, 60)
w, h = src.start()
check((w, h) == (6, 4), f"start() succeeded after one device-removed error: {w}x{h}")
check(old_factory.cleaned == 1, "old factory's cameras were released")
check(getattr(fake, "__factory") is not old_factory, "a new DXFactory replaced the module global")
check(FakeSingleton._instances.get(FakeFactory) is getattr(fake, "__factory"), "Singleton registry points at the new factory")
check(src.get_latest_frame() is not None, "frames flow from the new camera")

# --- 2. dead device twice: the second failure propagates ---
fake = make_fake_dxcam(failures=2); sys.modules["dxcam"] = fake
try:
    capture.DisplaySource(0, 60).start(); check(False, "second failure should raise")
except COMError as e2:
    check(capture.is_device_removed(e2), "still device-removed after one rebuild -> raised to the caller")
check(fake._state["left"] == 0, "exactly one retry")

# --- 3. unrelated errors do not trigger a rebuild ---
fake = make_fake_dxcam(failures=0); sys.modules["dxcam"] = fake
def boom(**kw): raise RuntimeError("No DXGI output")
fake.create = boom
before = FakeFactory.built
try:
    capture.DisplaySource(0, 60).start(); check(False, "should raise")
except RuntimeError:
    check(FakeFactory.built == before, "no factory rebuild for a non-DXGI error")

print("ALL OK" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
