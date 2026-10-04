import os, sys, tempfile, json, time, io, contextlib
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="screecap_t_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

from PyQt6 import QtCore, QtWidgets
import gui, cli, ledger, server, windows_integration, screens
QtCore.QCoreApplication.setOrganizationName("screecap-test")
QtCore.QCoreApplication.setApplicationName("screecap-test")
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
t0 = time.time()
win = gui.MainWindow()
print(f"     (MainWindow constructed in {time.time()-t0:.1f}s)")
check(win._api is not None and win._api.port > 0, "GUI starts the local API")
info = cli.running()
check(info is not None and info["pid"] == os.getpid(), "cli.running() finds the GUI through the lockfile")
st = info["status"]
check(st["recording"] is False and "league" in st and st["ledger_path"] == ledger.path(), "status hook reports recorder + league state")
# monitor picker: Auto first and default; device tiles carry stable specs; legacy int migrates to auto
mp = win.monitor_picker
check(mp._tiles and mp._tiles[0].spec == screens.AUTO and mp.selected_spec() == screens.AUTO, "picker defaults to auto (first tile)")
devs = [t.spec for t in mp._tiles[1:]]
check(all(d.startswith("\\\\.\\DISPLAY") for d in devs) and len(devs) == len(screens.enumerate_outputs()), f"device tiles use device names: {devs}")
if devs:
    check(mp.set_selected_spec(devs[-1]) and mp.selected_spec() == devs[-1] and mp.selected_index() == screens.resolve(devs[-1])[0].idx, "selecting a device tile resolves to its current index")
    mp.set_selected_spec(screens.AUTO)
win._settings.setValue("capture/monitor_spec", ""); win._settings.setValue("capture/monitor", 1)
win._load_persistent_prefs()
check(mp.selected_spec() == screens.AUTO and ledger.tail(1)[0]["target"] == "settings:capture/monitor", "legacy capture/monitor=1 migrates to auto with a ledger note")
args = win._collect_args()
check(isinstance(args["monitor"], int) and args["monitor"] >= 0 and ledger.tail(1)[0]["target"] == "screen:choice", "collect_args resolves the spec and announces the choice")
# jump actions and jump list stay in lockstep
check({"open-web", "open-terminal"} <= gui.JUMP_ACTIONS, "new jump actions registered")
check({a for _, a, _ in windows_integration.DEFAULT_JUMP_TASKS} == gui.JUMP_ACTIONS - {"show"}, "jump list tasks == JUMP_ACTIONS minus show")
# API 'show' action marshals to the GUI thread
win.hide()
cli._request(info, "/api/action/show", method="POST")
app.processEvents(); time.sleep(0.2); app.processEvents()
check(win.isVisible(), "POST /api/action/show raises the window via the bridge")
# phase change writes a ledger note. Stop the League stream first: with a real
# client running it would interleave its own lockfile-read lines here.
win.lcu_check.setChecked(False)
time.sleep(0.3)
win._on_lcu_phase_changed("ChampSelect")
check(ledger.tail(1)[0]["kind"] == "note" and ledger.tail(1)[0]["detail"] == "ChampSelect", "phase change -> ledger note")
win._on_lcu_phase_changed("ChampSelect")
check(ledger.tail(1)[0]["detail"] == "ChampSelect" and ledger.tail(2)[0]["detail"] != "ChampSelect", "same phase not repeated")
# cli status output against the live GUI
cli._USE_COLOR = False
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.cmd_status(None)
out = buf.getvalue()
check(rc == 0 and f"pid {os.getpid()}" in out and "recorder:  idle" in out and "never" in out, "screecap status prints pid, state, manifest")
# cli ledger
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cli.main(["ledger", "-n", "3"])
check(buf.getvalue().count("\n") == 3, "screecap ledger -n 3 prints three lines")
# passthrough parse: unknown first arg routes to record (we only check routing, not recording)
routed = {}
cli.cmd_record = lambda rest: routed.setdefault("rest", rest) or 0
cli.main(["--duration", "1", "-o", "x.mp4"])
check(routed.get("rest") == ["--duration", "1", "-o", "x.mp4"], "unknown flags pass through to record")
# quit over the API: refused while "recording", otherwise quits via the bridge
win.thread = object()
r = cli._request(info, "/api/action/quit", method="POST")
check(r["result"].startswith("refused"), "quit refused while a recording is in progress")
win.thread = None
cli._request(info, "/api/action/quit", method="POST")
for _ in range(20):
    app.processEvents(); time.sleep(0.05)
check(win._api is None and server.read_lockfile() is None, "POST quit stops API and removes lockfile")
print("\n%d failures" % len(fails)); sys.exit(1 if fails else 0)
