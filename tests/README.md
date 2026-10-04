# tests

Offscreen suites. They need the Windows venv (PyQt6, dxcam) and set `QT_QPA_PLATFORM=offscreen`
plus a temporary `LOCALAPPDATA` themselves, so they never touch your real data folder.

```powershell
.venv\Scripts\python.exe tests\test_ledger.py        # ledger grammar, API auth/SSE, CLI formatting
.venv\Scripts\python.exe tests\test_gui_api.py       # real MainWindow offscreen + API round trip
.venv\Scripts\python.exe tests\test_watch_smoke.py   # `screecap watch` as a subprocess against a live API
```

From WSL: `.venv/Scripts/python.exe "$(wslpath -w tests/test_ledger.py)"`.
Each prints `ok`/`FAIL` per check and exits non-zero on any failure.
