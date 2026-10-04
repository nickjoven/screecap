"""Windows shell integration: AppUserModelID, run-on-login, shortcut, jump list.

All entry points degrade gracefully — if a dependency (pywin32, comtypes) is
missing or a COM call fails, the function logs and returns False so the GUI can
keep working without the integration. None of these are load-bearing for the
recorder itself.

CLI usage (idempotent):

    python -m windows_integration --install-shortcut   # Start Menu shortcut
    python -m windows_integration --enable-startup     # add HKCU\\Run entry
    python -m windows_integration --disable-startup    # remove it
    python -m windows_integration --refresh-jumplist   # rebuild taskbar tasks
"""
import argparse
import ledger
import logging
import os
import sys
from typing import Optional

log = logging.getLogger(__name__)

APP_USER_MODEL_ID = "screecap.recorder"
APP_DISPLAY_NAME = "Screen Recorder"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "screecap"


# ----------------------------------------------------------------------------
# Path helpers — every integration target needs these resolved consistently.
# ----------------------------------------------------------------------------

def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _gui_script() -> str:
    return os.path.join(_project_dir(), "gui.py")


def _python_launcher() -> str:
    """Path to pythonw.exe (no console window). Fall back to python.exe."""
    exe_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(exe_dir, "pythonw.exe")
    return pythonw if os.path.exists(pythonw) else sys.executable


def _quote(arg: str) -> str:
    """Wrap an argument in double quotes if it contains spaces."""
    return f'"{arg}"' if " " in arg and not arg.startswith('"') else arg


def _launch_args(extra: str = "") -> str:
    """Argv string for launching the GUI: '"<gui.py>" [extra]'."""
    parts = [_quote(_gui_script())]
    if extra:
        parts.append(extra)
    return " ".join(parts)


def _launch_command(extra: str = "") -> str:
    """Full command for HKCU\\Run: '"pythonw.exe" "<gui.py>" [extra]'."""
    return f"{_quote(_python_launcher())} {_launch_args(extra)}"


# ----------------------------------------------------------------------------
# AppUserModelID — required for taskbar grouping, jump lists, and toasts.
# Without this, a pinned shortcut and the running process appear as two
# separate icons (the running one inherits pythonw.exe's identity).
# ----------------------------------------------------------------------------

def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Tag this process with `app_id`. Call once, very early (before any UI)."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        log.info("AppUserModelID set: %s", app_id)
        return True
    except Exception:
        log.exception("SetCurrentProcessExplicitAppUserModelID failed")
        return False


# ----------------------------------------------------------------------------
# Run on Windows login (HKCU\Run). Per-user; no admin required.
# ----------------------------------------------------------------------------

def is_launch_on_login_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("read HKCU\\Run failed")
        return False


def set_launch_on_login(enabled: bool) -> bool:
    """Add or remove the HKCU\\Run entry. Returns True on success."""
    try:
        import winreg
    except ImportError:
        log.warning("winreg unavailable (non-Windows?); cannot toggle startup")
        return False
    try:
        if enabled:
            cmd = _launch_command("--minimized")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                                winreg.KEY_SET_VALUE) as key:
                with ledger.begin("write", f"HKCU\\{RUN_KEY_PATH}\\{RUN_VALUE_NAME}", cmd):
                    winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, cmd)
            log.info("launch-on-login enabled: %s", cmd)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    with ledger.begin("write", f"HKCU\\{RUN_KEY_PATH}\\{RUN_VALUE_NAME}", "delete"):
                        winreg.DeleteValue(key, RUN_VALUE_NAME)
                log.info("launch-on-login disabled")
            except FileNotFoundError:
                pass  # already absent — treat as success
        return True
    except OSError:
        log.exception("write HKCU\\Run failed")
        return False


# ----------------------------------------------------------------------------
# Start Menu / desktop shortcut (.lnk). Sets AppUserModelID on the link so
# pinning to the taskbar groups correctly with the running process.
# ----------------------------------------------------------------------------

def _start_menu_dir() -> str:
    appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")


def install_shortcut(path: Optional[str] = None) -> Optional[str]:
    """Create a .lnk for the GUI. Returns the .lnk path on success, else None.

    Default location: Start Menu\\Programs\\<APP_DISPLAY_NAME>.lnk.
    """
    target_path = path or os.path.join(_start_menu_dir(), f"{APP_DISPLAY_NAME}.lnk")
    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
    except OSError:
        log.exception("could not create shortcut directory")
        return None

    try:
        from win32com.client import Dispatch
    except ImportError:
        log.warning("pywin32 not installed; cannot create shortcut")
        return None

    try:
        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(target_path)
        shortcut.TargetPath = _python_launcher()
        # No --minimized here — clicking the shortcut should open the window.
        # The HKCU\Run entry, set separately, is what passes --minimized.
        shortcut.Arguments = _launch_args()
        shortcut.WorkingDirectory = _project_dir()
        shortcut.IconLocation = f"{_python_launcher()},0"
        shortcut.Description = APP_DISPLAY_NAME
        shortcut.Save()
    except Exception:
        log.exception("WScript.Shell shortcut creation failed")
        return None

    # Stamp the AppUserModelID onto the link so pinning groups correctly.
    # WScript.Shell doesn't expose this property, so we reopen via IShellLink
    # + IPropertyStore. Failure here is non-fatal — the .lnk still works.
    try:
        _stamp_app_id_on_link(target_path, APP_USER_MODEL_ID)
    except Exception:
        log.exception("stamping AppUserModelID on shortcut failed (non-fatal)")

    log.info("shortcut installed: %s", target_path)
    return target_path


def _stamp_app_id_on_link(lnk_path: str, app_id: str) -> None:
    """Re-open `lnk_path` via IShellLink and set System.AppUserModel.ID on it."""
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None,
        pythoncom.CLSCTX_INPROC_SERVER,
        shell.IID_IShellLink,
    )
    persist = link.QueryInterface(pythoncom.IID_IPersistFile)
    persist.Load(lnk_path, 0)

    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID,
                   propsys.PROPVARIANTType(app_id, pythoncom.VT_LPWSTR))
    store.Commit()
    persist.Save(lnk_path, True)


# ----------------------------------------------------------------------------
# Jump list — right-click the taskbar icon for custom tasks. Each task is a
# ShellLink that invokes the GUI with --jump=<action>; the running instance
# (single-instance lock) intercepts and dispatches without spawning a duplicate.
# ----------------------------------------------------------------------------

# (label, jump-action, tooltip)
DEFAULT_JUMP_TASKS = [
    ("Start / stop recording", "toggle-record", "Start or stop a recording"),
    ("Open last recording",     "open-last",     "Play the most recent MP4"),
    ("Toggle League auto-record", "toggle-auto-record",
     "Enable or disable starting recordings on match start"),
    ("Open data folder",        "open-data-dir", "Open %LOCALAPPDATA%\\screecap"),
    ("Open web view",           "open-web",      "The ledger and controls in your browser"),
    ("Open terminal view",      "open-terminal", "Watch every action live in a terminal"),
]


def refresh_jump_list(app_id: str = APP_USER_MODEL_ID, tasks=None) -> bool:
    """Replace the taskbar jump list for `app_id` with `tasks`. Returns True on success."""
    tasks = tasks or DEFAULT_JUMP_TASKS
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell
    except ImportError:
        log.warning("pywin32 not installed; cannot build jump list")
        return False

    try:
        cdl = pythoncom.CoCreateInstance(
            shell.CLSID_DestinationList, None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_ICustomDestinationList,
        )
        cdl.SetAppID(app_id)
        # BeginList returns (max_slots, removed_items_array). We don't honor
        # removed items (they're URIs the user explicitly hid); for our small
        # static task list there's no way for them to drift.
        cdl.BeginList()

        collection = pythoncom.CoCreateInstance(
            shell.CLSID_EnumerableObjectCollection, None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IObjectCollection,
        )

        for label, action, tooltip in tasks:
            link = _build_task_link(label, action, tooltip,
                                    pythoncom, propsys, pscon, shell)
            collection.AddObject(link)

        cdl.AddUserTasks(collection.QueryInterface(shell.IID_IObjectArray))
        cdl.CommitList()
        log.info("jump list refreshed (%d tasks) for %s", len(tasks), app_id)
        return True
    except Exception:
        log.exception("jump list refresh failed")
        return False


def _build_task_link(label, action, tooltip, pythoncom, propsys, pscon, shell):
    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None,
        pythoncom.CLSCTX_INPROC_SERVER,
        shell.IID_IShellLink,
    )
    link.SetPath(_python_launcher())
    link.SetArguments(_launch_args(f"--jump={action}"))
    link.SetWorkingDirectory(_project_dir())
    link.SetIconLocation(_python_launcher(), 0)
    link.SetDescription(tooltip)  # tooltip on hover

    # Visible label in the jump list comes from PKEY_Title on the link's
    # property store, not from SetDescription.
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_Title,
                   propsys.PROPVARIANTType(label, pythoncom.VT_LPWSTR))
    store.Commit()
    return link


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _main():
    import logging_setup
    logging_setup.configure(also_stderr=True)

    ap = argparse.ArgumentParser(description="Windows shell integration for screecap.")
    ap.add_argument("--install-shortcut", action="store_true",
                    help="Create a Start Menu shortcut for the GUI.")
    ap.add_argument("--shortcut-path", default=None,
                    help="Override shortcut destination (default: Start Menu).")
    ap.add_argument("--enable-startup", action="store_true",
                    help="Add HKCU\\Run entry so the GUI launches at login.")
    ap.add_argument("--disable-startup", action="store_true",
                    help="Remove the HKCU\\Run entry.")
    ap.add_argument("--refresh-jumplist", action="store_true",
                    help="Rebuild the taskbar jump list for this app.")
    args = ap.parse_args()

    # AppUserModelID is needed for the jump list to attach to the right pinned
    # icon. Setting it on the helper process is harmless even when no jump list
    # is being touched.
    set_app_user_model_id()

    did_anything = False
    if args.install_shortcut:
        did_anything = True
        path = install_shortcut(args.shortcut_path)
        print(f"shortcut: {path or 'FAILED'}")
    if args.enable_startup:
        did_anything = True
        ok = set_launch_on_login(True)
        print(f"launch-on-login enabled: {ok}")
    if args.disable_startup:
        did_anything = True
        ok = set_launch_on_login(False)
        print(f"launch-on-login disabled: {ok}")
    if args.refresh_jumplist:
        did_anything = True
        ok = refresh_jump_list()
        print(f"jump list refreshed: {ok}")
    if not did_anything:
        ap.print_help()


if __name__ == "__main__":
    _main()
