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
# audio hint: visible only when two or more inputs are ticked
al = win.audio_list
if al.count() >= 2:
    for i in range(al.count()): al.item(i).setCheckState(QtCore.Qt.CheckState.Unchecked)
    al.item(0).setCheckState(QtCore.Qt.CheckState.Checked)
    check(not win.audio_hint.isVisibleTo(win), "one audio input: no hint")
    al.item(1).setCheckState(QtCore.Qt.CheckState.Checked)
    check(win.audio_hint.isVisibleTo(win) and "recorded twice" in win.audio_hint.text(), "two audio inputs: doubled-source hint shown")
else:
    print("     (fewer than two audio devices here; hint check skipped)")
# settings over the API. The request must come from another thread while this
# (GUI) thread pumps events, because apply runs on the GUI thread via GuiCall.
import threading, json, urllib.request, urllib.error
def api_from_thread(path, method="GET", body=None):
    box = {}
    def go():
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r: box["status"], box["body"] = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e: box["status"], box["body"] = e.code, json.loads(e.read() or b"{}")
        except Exception as e: box["status"], box["body"] = -1, {"error": repr(e)}
    t = threading.Thread(target=go, daemon=True); t.start()
    for _ in range(400):
        app.processEvents(); time.sleep(0.01)
        if not t.is_alive(): break
    return box.get("status"), box.get("body")
st, s0 = api_from_thread("/api/settings")
check(st == 200 and "fps" in s0 and "available" in s0 and s0["monitor"] == screens.AUTO, "GET /api/settings returns the snapshot")
newfps = 30 if s0["fps"] != 30 else 24
st, s1 = api_from_thread("/api/settings", "POST", {"fps": newfps, "postgame_reminder": not s0["postgame_reminder"]})
check(st == 200 and s1["fps"] == newfps and win.fps_spin.value() == newfps and win.postgame_check.isChecked() == (not s0["postgame_reminder"]),
      "POST applies fps + a checkbox on the GUI thread")
check(win._settings.value("capture/fps", 0, type=int) == newfps, "applied fps is persisted to QSettings")
check(ledger.tail(2)[0]["kind"] == "api" and f'"fps": {newfps}' in ledger.tail(2)[0]["detail"], "settings change announced verbatim in the ledger")
st, b = api_from_thread("/api/settings", "POST", {"fps": 9999})
check(st == 400 and "fps" in b["error"], "out-of-range fps -> 400")
st, b = api_from_thread("/api/settings", "POST", {"nope": 1})
check(st == 400 and "unknown" in b["error"], "unknown key -> 400")
st, b = api_from_thread("/api/settings", "POST", {"monitor": "DISPLAY99"})
check(st == 400, "unknown monitor spec -> 400")
if s0["available"]["monitors"][1:]:
    spec = s0["available"]["monitors"][1]["spec"]
    st, b = api_from_thread("/api/settings", "POST", {"monitor": spec})
    check(st == 200 and win.monitor_picker.selected_spec() == spec, "monitor spec applied to the picker")
    api_from_thread("/api/settings", "POST", {"monitor": screens.AUTO})
win.thread = object()
st, b = api_from_thread("/api/settings", "POST", {"fps": 60})
check(st == 409, "settings refused while recording (409)")
win.thread = None
api_from_thread("/api/settings", "POST", {"fps": s0["fps"], "postgame_reminder": s0["postgame_reminder"]})
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
