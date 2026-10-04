import os, sys, tempfile, json, threading, time, urllib.request, urllib.error, http.client, subprocess
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="screecap_t_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

import ledger, logging_setup
# ---- grammar ---------------------------------------------------------
e = ledger.begin("exec", "ffmpeg", "ffmpeg -y | weird\nline")
e.ok("rc=0")
lines = open(ledger.path(), encoding="utf-8").read().splitlines()
check(len(lines) == 2, "two lines: announce + answer")
cols = lines[0].split(" | ")
check(len(cols) == 6 and cols[2] == "exec" and cols[3] == "ffmpeg", "six columns, kind/target in place")
check("¦" in cols[4] and "⏎" in cols[4] and "|" not in cols[4], "pipe and newline escaped in detail")
check(cols[5].endswith("test_ledger.py:" + cols[5].rsplit(":", 1)[1]) and cols[5].startswith("tests/"), "src is the project-relative caller: " + cols[5])
r1, r2 = ledger.parse_line(lines[0]), ledger.parse_line(lines[1])
check(r2["kind"] == "ok" and r2["ref"] == r1["seq"] and r2["target"] == f"#{r1['seq']}", "answer references the announce seq")
check(ledger.parse_line("garbage") is None and ledger.parse_line("") is None, "parse rejects non-grammar")
# context manager, err on exception
try:
    with ledger.begin("write", "x.txt", "test") as w:
        raise RuntimeError("boom")
except RuntimeError: pass
last = ledger.tail(1)[0]
check(last["kind"] == "err" and "RuntimeError: boom" in last["detail"], "context manager answers err with the exception")
# double close ignored
w = ledger.begin("write", "y", ""); w.ok("a"); w.ok("b")
check(ledger.tail(1)[0]["detail"] == "a", "second close is ignored")
# manifest enforcement
ledger.begin("http", "GET https://example.com/x", "").ok()
recs = ledger.tail(3)
check(any(r["kind"] == "err" and "OUTSIDE MANIFEST" in r["detail"] for r in recs), "non-local http target flagged")
ledger.begin("http", "GET https://127.0.0.1:1234/x", "").ok()
check(not any("OUTSIDE" in r["detail"] for r in ledger.tail(2)), "local http target not flagged")
# subscribers + ring
got = []
ledger.subscribe(got.append)
ledger.note("league:phase", "InProgress")
ledger.unsubscribe(got.append)
check(len(got) == 1 and got[0]["kind"] == "note", "subscriber sees the record")
check(ledger.recent(0, 1)[0]["seq"] == got[0]["seq"], "ring holds the latest")
# seq continues across process restarts
seq_now = got[0]["seq"]
out = subprocess.run([sys.executable, "-c",
    "import os,sys; sys.path.insert(0, os.environ['SCREECAP_DIR']); import ledger; print(ledger.note('t','x')['seq'])"],
    capture_output=True, text=True, env=dict(os.environ, SCREECAP_DIR=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
check(out.stdout.strip() == str(seq_now + 1), f"seq continues from file in a new process ({out.stdout.strip()} vs {seq_now+1})")
# two processes interleaving writes keep seq strictly increasing in the file
child_code = "import os,sys; sys.path.insert(0, os.environ['SCREECAP_DIR']); import ledger; ledger.note('child','x')['seq']"
for _ in range(3):
    ledger.note("parent", "x")
    subprocess.run([sys.executable, "-c", child_code], check=True,
                   env=dict(os.environ, SCREECAP_DIR=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
seqs = [r["seq"] for r in ledger.tail(6)]
check(seqs == sorted(seqs) and len(set(seqs)) == 6 and [r["target"] for r in ledger.tail(6)] == ["parent","child"]*3,
      f"interleaved parent/child writes keep seq strictly increasing: {seqs}")
# ledger.run
p = ledger.run([sys.executable, "-c", "print('hi')"], capture_output=True, text=True)
recs = ledger.tail(2)
check(recs[0]["kind"] == "exec" and "-c" in recs[0]["detail"] and recs[1]["kind"] == "ok" and "rc=0" in recs[1]["detail"], "ledger.run announces argv and answers rc")
# password never in ledger via lockfile parse
import league.lockfile as lf
d = tempfile.mkdtemp(); lp = os.path.join(d, "lockfile")
open(lp, "w").write("LeagueClient:123:5555:SECRETPW:https")
lf._candidate_lockfile_paths = lambda: [__import__('pathlib').Path(lp)]
got_lf = lf.find_lockfile_via_paths()
txt = open(ledger.path(), encoding="utf-8").read()
check(got_lf is not None and got_lf.port == 5555 and "SECRETPW" not in txt and "port=5555" in txt, "lockfile read is announced, token never written")

# ---- server ----------------------------------------------------------
import server
state = {"recording": False}
calls = []
hooks = server.Hooks(status=lambda: dict(state), actions={"record-start": lambda: calls.append("start") or "queued", "boom": lambda: 1/0})
srv = server.Server(hooks).start()
port, token = srv.port, srv.token
base = f"http://127.0.0.1:{port}"
info = server.read_lockfile()
check(info and info["port"] == port and info["token"] == token and info["pid"] == os.getpid(), "lockfile written in Riot format with pid/port/token")
def req(path, method="GET", headers=None, body=None):
    r = urllib.request.Request(base + path, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
st, h, b = req("/api/status")
check(st == 401, "no auth -> 401")
st, h, b = req("/api/status", headers={"Authorization": f"Bearer {token}"})
check(st == 200 and json.loads(b)["port"] == port and json.loads(b)["recording"] is False, "bearer -> status")
st, h, b = req("/api/status", headers={"Authorization": f"Bearer {token}", "Host": "evil.example:80"})
check(st == 403, "wrong Host -> 403 (DNS rebinding)")
st, h, b = req("/api/status", headers={"Authorization": f"Bearer {token}", "Origin": "http://evil.example"})
check(st == 403, "foreign Origin -> 403")
st, h, b = req("/?t=wrong")
check(st == 403, "bad token link -> 403")
# token exchange -> cookie + redirect (no auto-follow: use http.client)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=5); c.request("GET", f"/?t={token}"); r = c.getresponse(); r.read()
sc = r.getheader("Set-Cookie") or ""
check(r.status == 302 and r.getheader("Location") == "/" and "HttpOnly" in sc and "SameSite=Strict" in sc and token in sc, "token link -> cookie + redirect to /")
st, h, b = req("/", headers={"Cookie": f"screecap={token}"})
check(st == 200 and b"<title>screecap ledger</title>" in b and "Content-Security-Policy" in h, "cookie -> index.html with CSP")
st, h, b = req("/")
check(st == 401, "index without cookie -> 401")
st, h, b = req("/api/manifest", headers={"Authorization": f"Bearer {token}"})
m = json.loads(b); check(st == 200 and m["manifest"]["never"] and m["ledger_path"] == ledger.path(), "manifest endpoint")
st, h, b = req("/api/ledger?since=0&limit=3", headers={"Authorization": f"Bearer {token}"})
check(st == 200 and len(json.loads(b)["entries"]) == 3, "ledger?since/limit")
st, h, b = req("/api/action/record-start", method="POST", headers={"Authorization": f"Bearer {token}"})
check(st == 202 and calls == ["start"] and ledger.tail(2)[0]["kind"] == "api", "POST action runs hook, announced as api")
st, h, b = req("/api/action/record-start", method="POST", headers={"Cookie": f"screecap={token}", "Origin": "http://evil.example"})
check(st == 403 and calls == ["start"], "POST with cookie but foreign Origin refused")
st, h, b = req("/api/action/boom", method="POST", headers={"Authorization": f"Bearer {token}"})
check(st == 500 and ledger.tail(1)[0]["kind"] == "err", "failing hook -> 500 + err line")
st, h, b = req("/api/action/nope", method="POST", headers={"Authorization": f"Bearer {token}"})
check(st == 404, "unknown action -> 404")
st, h, b = req("/api/source?src=ledger.py:10", headers={"Authorization": f"Bearer {token}"})
s = json.loads(b); check(st == 200 and s["line"] == 10 and len(s["lines"]) > 10 and "github.com/nickjoven/screecap" in s["github"], "source viewer returns context + GitHub link")
st, h, b = req("/api/source?src=..%2F..%2Fwindows%2Fwin.ini:1", headers={"Authorization": f"Bearer {token}"})
check(st == 404, "source viewer refuses paths outside the whitelist")
# SSE: replay then live
c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
c.request("GET", "/api/ledger/stream?since=0", headers={"Authorization": f"Bearer {token}"}); r = c.getresponse()
check(r.status == 200 and r.getheader("Content-Type", "").startswith("text/event-stream"), "SSE headers")
first = r.readline().decode()
check(first.startswith("id: "), "SSE replays with ids")
# drain replay quickly then emit live
deadline = time.time() + 3; seen_live = False
ledger.note("live:test", "ping-from-test")
while time.time() < deadline:
    line = r.readline().decode()
    if "ping-from-test" in line: seen_live = True; break
check(seen_live, "SSE delivers a live entry")
c.close()
srv.stop()
check(server.read_lockfile() is None, "stop removes the lockfile")
check(ledger.tail(1)[0]["detail"] == "API stopped", "stop announced")

# ---- cli -------------------------------------------------------------
import cli
cli._USE_COLOR = False
rec = {"ts": "2026-10-03T12:00:00.123Z", "seq": 42, "kind": "exec", "target": "ffmpeg", "detail": "ffmpeg -y x", "src": "recorder.py:150"}
line = cli.format_entry(rec)
check("    42  exec   ffmpeg  ffmpeg -y x  (recorder.py:150)" in line, "cli line format: " + line)
check(cli.running() is None, "cli.running() None when no instance")
print("\n%d failures" % len(fails)); sys.exit(1 if fails else 0)
