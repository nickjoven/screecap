"""LCU credential discovery: lockfile on disk, or running-process command line."""
import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

try:
    import psutil
except ImportError:
    psutil = None

log = logging.getLogger(__name__)


@dataclass
class Lockfile:
    """Credentials for the LCU. Source: lockfile on disk OR process command line."""
    pid: int
    port: int
    password: str
    protocol: str = "https"

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"

    @property
    def auth_header(self) -> str:
        token = base64.b64encode(f"riot:{self.password}".encode()).decode()
        return f"Basic {token}"


def _parse_lockfile_text(text: str) -> Lockfile:
    # Format: ProcessName:PID:Port:Password:Protocol
    parts = text.strip().split(":")
    if len(parts) != 5:
        raise ValueError(f"unexpected lockfile format: {text!r}")
    return Lockfile(pid=int(parts[1]), port=int(parts[2]),
                    password=parts[3], protocol=parts[4])


def _candidate_lockfile_paths() -> List[Path]:
    candidates = [
        Path("C:/Riot Games/League of Legends/lockfile"),
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Riot Games/League of Legends/lockfile",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Riot Games/League of Legends/lockfile")
    return candidates


def find_lockfile_via_paths() -> Optional[Lockfile]:
    for path in _candidate_lockfile_paths():
        if path.is_file():
            try:
                lf = _parse_lockfile_text(path.read_text(encoding="utf-8"))
                log.debug("lockfile from path: %s", path)
                return lf
            except (OSError, ValueError) as e:
                log.warning("could not read lockfile %s: %s", path, e)
    return None


def find_lockfile_via_process() -> Optional[Lockfile]:
    """Read --app-port and --remoting-auth-token from a running LeagueClientUx process.

    More robust than scanning known install paths — handles non-default installs.
    """
    if psutil is None:
        return None
    for proc in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("leagueclientux.exe", "leagueclient.exe"):
                continue
            port = None
            password = None
            for arg in proc.info.get("cmdline") or []:
                if arg.startswith("--app-port="):
                    port = int(arg.split("=", 1)[1])
                elif arg.startswith("--remoting-auth-token="):
                    password = arg.split("=", 1)[1]
            if port and password:
                log.debug("lockfile from process pid=%s", proc.info["pid"])
                return Lockfile(pid=proc.info["pid"], port=port, password=password)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            log.exception("process inspection failed")
    return None


def discover_lockfile() -> Optional[Lockfile]:
    """Best-effort: try the running process first, then known install paths."""
    return find_lockfile_via_process() or find_lockfile_via_paths()
