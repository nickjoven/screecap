import os, sys, tempfile, json, time, types
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="screecap_t_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

import ledger, sidecars
d = tempfile.mkdtemp(prefix="screecap_sc_")
video = os.path.join(d, "vid_2026-10-23_20-00-00.mp4")
open(video, "wb").write(b"x")

# ---- writers ---------------------------------------------------------
p = sidecars.write_done(video, ffmpeg_rc=0, frames=10, elapsed_s=1.5)
body = json.load(open(p, encoding="utf-8"))
check(p.endswith("vid_2026-10-23_20-00-00.done.json"), "done sidecar shares the video stem")
check(body["schema"] == "screecap.done/1" and body["frames"] == 10 and body["video"] == os.path.basename(video), "done sidecar carries the recorder's facts")
check(ledger.tail(2)[0]["kind"] == "write" and ledger.tail(2)[0]["target"] == p and ledger.tail(1)[0]["kind"] == "ok", "done write announced, then answered")
p = sidecars.write_match(video, {"gameId": 7, "localPlayer": {"championName": "Ahri"}})
body = json.load(open(p, encoding="utf-8"))
check(body["eog"]["gameId"] == 7 and "gameId=7" in ledger.tail(2)[0]["detail"], "match sidecar keeps eog verbatim, ledger names the game")
check(not [f for f in os.listdir(d) if f.startswith(".sidecar-")], "no temp files left behind")

# ---- recorder hook ---------------------------------------------------
from recorder import Recorder
r = Recorder.__new__(Recorder)
r.output, r._proc, r.error = video, types.SimpleNamespace(returncode=0), None
r.frame_count, r.start_time, r.loop_elapsed = 600, time.time() - 99, 10.0
try:
    r._write_done_sidecar()
    body = json.load(open(sidecars.path_for(video, sidecars.DONE_SUFFIX), encoding="utf-8"))
    check(body["ffmpeg_rc"] == 0 and body["frames"] == 600 and body["elapsed_s"] == 10.0, f"Recorder writes done sidecar: {body}")
except Exception as e:
    check(False, f"Recorder._write_done_sidecar raised {e!r}")
r.output = os.path.join(d, "missing.mp4")
r._write_done_sidecar()
check(not os.path.exists(os.path.join(d, "missing.done.json")), "no done sidecar for a file that does not exist")

# ---- GUI phase flow (stub self; no window, no hotkeys) ----------------
import gui, league
calls = []
class FakeClient:
    def __init__(self, lf): pass
    def end_of_game_stats(self): calls.append(1); return {"gameId": 99}
league.discover_lockfile, league.Client = (lambda: object()), FakeClient
v2 = os.path.join(d, "vid_2026-10-23_21-00-00.mp4")
open(v2, "wb").write(b"x")
stub = types.SimpleNamespace(
    _lcu_phase="InProgress", _match_video=None, _auto_started=False, thread=None,
    recorder=types.SimpleNamespace(output=v2), auto_record_status=types.SimpleNamespace(setText=lambda s: None),
    _maybe_show_postgame=lambda: None)
stub._fetch_match_sidecar = types.MethodType(gui.MainWindow._fetch_match_sidecar, stub)
handler = gui.MainWindow._on_lcu_phase_changed
handler(stub, "WaitingForStats")
check(stub._match_video == v2, "leaving InProgress remembers the recording")
stub.recorder = None
handler(stub, "PreEndOfGame")
check(not calls, "no fetch before EndOfGame")
handler(stub, "EndOfGame")
mp = sidecars.path_for(v2, sidecars.MATCH_SUFFIX)
for _ in range(50):
    if os.path.exists(mp): break
    time.sleep(0.05)
check(os.path.exists(mp) and json.load(open(mp))["eog"]["gameId"] == 99, "EndOfGame writes <video>.match.json")
check(stub._match_video is None, "pending match cleared after fetch")
handler(stub, "Lobby"); handler(stub, "EndOfGame")
check(len(calls) == 1, "a second EndOfGame without a game does not fetch")

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
