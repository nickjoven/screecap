import os, sys, tempfile, subprocess, time
sys.stdout.reconfigure(encoding="utf-8")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="screecap_t_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ledger, server
srv = server.Server(server.Hooks(status=lambda: {"recording": False})).start()
ledger.note("smoke", "before-watch")
outp = os.path.join(os.environ["LOCALAPPDATA"], "watch.out")
cmd = [sys.executable, "-u", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli.py"), "watch", "-n", "5", "--no-color"]
with open(outp, "w", encoding="utf-8") as f:
    proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=dict(os.environ))
    time.sleep(2.5)
    ledger.note("smoke", "while-watching")   # must arrive live over SSE
    time.sleep(1.5)
    proc.kill(); proc.wait()
srv.stop()
out = open(outp, encoding="utf-8", errors="replace").read()
print(out[-1200:])
ok = all(k in out for k in ("announces each action", "before-watch", "live from screecap pid", "while-watching"))
print("WATCH SMOKE:", "ok" if ok else "FAIL")
sys.exit(0 if ok else 1)
