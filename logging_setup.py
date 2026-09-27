"""Rotating-file logging for screecap, intended primarily for debugging.

Logs land in `%LOCALAPPDATA%\\screecap\\screecap.log` (with up to 3 rotated
backups). Both front-ends call `configure()` at startup; the engine modules
just use `logging.getLogger(__name__)` and the records flow to the file.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

_CONFIGURED = False


def log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "screecap")


def log_path() -> str:
    return os.path.join(log_dir(), "screecap.log")


def configure(also_stderr: bool = False) -> Optional[str]:
    """Install handlers on the root logger. Returns the log file path on success.

    Idempotent. Pass `also_stderr=True` for the CLI so warnings still surface
    in the terminal even though they now go through the logger.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return log_path()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    path = log_path()
    try:
        os.makedirs(log_dir(), exist_ok=True)
        fh = RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        # Falling back to stderr is better than silently dropping the channel.
        sys.stderr.write(f"screecap: could not open log file {path}: {e}\n")
        path = None

    if also_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.WARNING)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # comtypes is extremely chatty at DEBUG; clamp it.
    logging.getLogger("comtypes").setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger("screecap").info("logging initialized -> %s", path)
    return path
