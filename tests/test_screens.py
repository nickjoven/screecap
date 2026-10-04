import os, sys, tempfile
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="screecap_t_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

import screens
O = screens.Output
# Tonight's real layout, in the order the NEW process saw it (primary first).
outs = [O(0, r"\\.\DISPLAY2", 0, 0, 1920, 1080, True), O(1, r"\\.\DISPLAY1", 0, -1080, 1920, 0, False)]
game_on_primary = (1234, "League of Legends (TM) Client", (0, 0, 1920, 1080))
game_on_top = (1234, "League of Legends (TM) Client", (0, -1080, 1920, 0))

o, why = screens.resolve("auto", outs, game_window=game_on_primary)
check(o.device.endswith("DISPLAY2") and "game window" in why, "auto follows the game window (primary)")
o, why = screens.resolve("auto", outs, game_window=game_on_top)
check(o.device.endswith("DISPLAY1") and o.idx == 1, "auto follows the game window (top display)")
o, why = screens.resolve("auto", outs, game_window=False)
check(o.primary and "no League game window" in why, "auto without a game -> primary, says so")
o, why = screens.resolve("primary", outs)
check(o.device.endswith("DISPLAY2"), "primary spec")
o, why = screens.resolve(r"\\.\DISPLAY1", outs)
check(o.idx == 1, "device name spec (full)")
o, why = screens.resolve("display1", outs)
check(o is not None and o.idx == 1, "device name spec (short, case-insensitive)")
o, why = screens.resolve("1", outs)
check(o is not None and o.idx == 1 and "not stable" in why, "legacy index resolves but is called out")
o, why = screens.resolve("7", outs)
check(o is None, "bad index -> None")
o, why = screens.resolve("DISPLAY9", outs)
check(o is None and "have DISPLAY2, DISPLAY1" in why, "unknown device lists what exists")
o, why = screens.resolve("auto", [], game_window=False)
check(o is None, "no outputs -> None")
# the whole point: same device name resolves correctly when the order flips
flipped = [O(0, r"\\.\DISPLAY1", 0, -1080, 1920, 0, False), O(1, r"\\.\DISPLAY2", 0, 0, 1920, 1080, True)]
a, _ = screens.resolve(r"\\.\DISPLAY2", outs); b, _ = screens.resolve(r"\\.\DISPLAY2", flipped)
check(a.idx == 0 and b.idx == 1 and a.device == b.device, "device name survives an enumeration order flip (idx 0 -> 1)")
# window overhanging by a pixel still maps by centre
o = screens.output_for_rect((-1, -1, 1921, 1081), outs)
check(o is not None and o.device.endswith("DISPLAY2"), "borderless window overhang maps by centre")
# qt screen matching by geometry
class FakeScreen:
    def __init__(self, x, y, name): self._x, self._y, self._n = x, y, name
    def geometry(self):
        class G:
            def __init__(s, x, y): s._x, s._y = x, y
            def x(s): return s._x
            def y(s): return s._y
        return G(self._x, self._y)
    def devicePixelRatio(self): return 1.0
    def name(self): return self._n
qs = [FakeScreen(0, -1080, "top"), FakeScreen(0, 0, "bottom")]
check(screens.qt_screen_for_output(outs[0], qs).name() == "bottom" and screens.qt_screen_for_output(outs[1], qs).name() == "top",
      "Qt screen matched by geometry, not order")
# real enumeration on this machine
real = screens.enumerate_outputs()
if os.name == "nt" and real:
    check(all(o.device.startswith("\\\\.\\DISPLAY") for o in real), f"real outputs have device names: {[o.short for o in real]}")
    check(sum(o.primary for o in real) == 1, "exactly one primary among real outputs")
    check("DISPLAY" in screens.describe_index(real[0].idx) and "identity unknown" in screens.describe_index(99), "describe_index")
else:
    print("     (no real outputs here; skipped)")
print("\n%d failures" % len(fails)); sys.exit(1 if fails else 0)
