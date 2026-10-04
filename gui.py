"""PyQt6 GUI for the screen recorder."""
import argparse
import logging
import os
import subprocess
import sys
import webbrowser

from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets

import cli
import highlights
import league
import ledger
import loadouts
import logging_setup
import preflight
import screens
import server
import windows_integration
from recorder import Recorder

log = logging.getLogger("screecap.gui")

# Set the AppUserModelID before any window is created or any shell COM is
# touched. Without this, taskbar pinning and the jump list wouldn't attach to
# the running process. Safe to call at import time — it's a no-op on non-Win.
windows_integration.set_app_user_model_id()

# Single-instance namespace. Per-user by default on QLocalServer (Windows).
SINGLE_INSTANCE_KEY = "screecap-gui"

try:
    from pynput import keyboard as _pynput_keyboard
except ImportError:
    _pynput_keyboard = None


class LCUBridge(QtCore.QObject):
    """Marshals LCU events from the EventStream's worker thread to the GUI thread.

    Same pattern as HotkeyBridge: pynput / websocket-client both deliver
    callbacks on background threads, and Qt slots can only be touched from
    the thread that owns the widget. Emitting a signal from a QObject is
    thread-safe and Qt automatically uses QueuedConnection across threads.
    """
    phase_changed = QtCore.pyqtSignal(str)
    connected = QtCore.pyqtSignal()
    disconnected = QtCore.pyqtSignal()


class ApiBridge(QtCore.QObject):
    """Marshals actions requested over the local API (server threads) onto the GUI thread."""
    start_record = QtCore.pyqtSignal()
    stop_record = QtCore.pyqtSignal()
    show = QtCore.pyqtSignal()
    quit = QtCore.pyqtSignal()


class LoadoutBridge(QtCore.QObject):
    """Marshals LoadoutTracker session updates from the websocket thread to the GUI."""
    session_changed = QtCore.pyqtSignal(object)  # dict or None


class HotkeyBridge(QtCore.QObject):
    """Routes a key event from pynput's listener thread to the Qt main thread.

    pynput's callbacks run in a background thread; calling Qt slots directly
    from there is unsafe. Emitting a signal from a QObject is thread-safe and
    Qt automatically uses QueuedConnection across threads.
    """
    toggle_pressed = QtCore.pyqtSignal()
    mark_pressed = QtCore.pyqtSignal()


def _format_pynput_key(key) -> str:
    """Human-readable label for a pynput Key (special) or KeyCode (char)."""
    if key is None:
        return "(none)"
    if hasattr(key, "name") and key.name is not None:
        return key.name.upper()
    char = getattr(key, "char", None)
    return char.upper() if char else "?"


def _pynput_keys_match(pressed, target) -> bool:
    """Compare two pynput key objects, with case-insensitive char matching."""
    if pressed == target:
        return True
    pressed_char = getattr(pressed, "char", None)
    target_char = getattr(target, "char", None)
    if pressed_char and target_char:
        return pressed_char.lower() == target_char.lower()
    return False


class HotkeyCaptureDialog(QtWidgets.QDialog):
    """Modal dialog that captures the next pynput keypress.

    Owns a one-shot pynput listener for the duration of the dialog. The main
    listener must be paused by the caller before showing this dialog so the
    captured keypress doesn't simultaneously trigger a record toggle.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set hotkey")
        self.setModal(True)
        self.captured_key = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "Press the key you want to use as the global record-toggle hotkey.\n"
            "(Modifier-only combos are not supported; pick a function or letter key.)"
        ))
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(cancel_btn)
        self._listener = _pynput_keyboard.Listener(on_press=self._on_press)
        self._listener.daemon = True
        self._listener.start()

    def _on_press(self, key):
        # Modifier-only presses (Shift/Ctrl/Alt) are skipped — wait for the next
        # actual key. Returning True keeps the listener running.
        if isinstance(key, _pynput_keyboard.Key) and key.name in (
            "shift", "shift_r", "ctrl", "ctrl_r", "alt", "alt_r",
            "alt_gr", "cmd", "cmd_r",
        ):
            return True
        self.captured_key = key
        # Cross-thread: marshal accept() back to the main thread.
        QtCore.QMetaObject.invokeMethod(self, "accept", QtCore.Qt.ConnectionType.QueuedConnection)
        return False

    def closeEvent(self, event):
        if self._listener.is_alive():
            self._listener.stop()
        super().closeEvent(event)


class RegionPicker(QtWidgets.QWidget):
    """Fullscreen translucent overlay; emit region in physical pixels on drag-release.

    Coords from event.position() are in Qt logical pixels for the screen the widget
    sits on. dxcam's region argument is in physical pixels, so we multiply by the
    screen's devicePixelRatio before emitting.
    """
    region_selected = QtCore.pyqtSignal(int, int, int, int)
    cancelled = QtCore.pyqtSignal()

    def __init__(self, screen: QtGui.QScreen):
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self._dpr = screen.devicePixelRatio()
        self._start = None
        self._end = None
        self._committed = False
        self.setGeometry(screen.geometry())

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._start = event.position().toPoint()
            self._end = self._start
            self.update()

    def mouseMoveEvent(self, event):
        if self._start is not None:
            self._end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton or self._start is None:
            return
        self._end = event.position().toPoint()
        rect = QtCore.QRect(self._start, self._end).normalized()
        if rect.width() < 4 or rect.height() < 4:
            self.close()
            return
        left = int(rect.left() * self._dpr)
        top = int(rect.top() * self._dpr)
        right = int(rect.right() * self._dpr)
        bottom = int(rect.bottom() * self._dpr)
        self._committed = True
        self.region_selected.emit(left, top, right, bottom)
        self.close()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 110))
        if self._start is None or self._end is None:
            return
        rect = QtCore.QRect(self._start, self._end).normalized()
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(rect, QtCore.Qt.GlobalColor.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 230), 2))
        painter.drawRect(rect)
        # Show the size in physical pixels under the rect
        w_phys = int(rect.width() * self._dpr)
        h_phys = int(rect.height() * self._dpr)
        text = f"{w_phys} x {h_phys}"
        painter.setPen(QtGui.QColor(255, 255, 255))
        painter.drawText(rect.left() + 4, rect.bottom() + 16, text)

    def closeEvent(self, event):
        if not self._committed:
            self.cancelled.emit()
        super().closeEvent(event)


def _grab_monitor_thumbnail(idx: int, size: QtCore.QSize) -> "QtGui.QPixmap | None":
    """Grab one frame from the dxcam output at `idx` and scale it to a thumbnail.

    Uses the same dxcam index the recorder will use, so the thumbnail and the
    actual recorded source are guaranteed to match — even if Qt's screen
    ordering differs from dxcam's. Matches the recorder's output_color="BGR"
    so the cached dxcam instance is consistent when recorder.start() runs later.
    """
    try:
        import dxcam
        cam = dxcam.create(output_idx=idx, output_color="BGR")
        if cam is None:
            return None
        frame = cam.grab()
        if frame is None:
            # First grab() can return None if the desktop hasn't changed since
            # the duplication API was initialized. One retry is usually enough.
            frame = cam.grab()
        if frame is None:
            return None
        h, w = frame.shape[:2]
        rgb = frame[:, :, ::-1].copy()  # BGR -> RGB, contiguous bytes for QImage
        img = QtGui.QImage(rgb.data, w, h, w * 3, QtGui.QImage.Format.Format_RGB888)
        return QtGui.QPixmap.fromImage(img).scaled(
            size,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
    except Exception:
        log.exception("thumbnail grab failed for monitor %d", idx)
        return None


class MonitorTile(QtWidgets.QToolButton):
    """A clickable thumbnail tile representing one dxcam output."""

    THUMB = QtCore.QSize(192, 108)  # 16:9; tiles fit ~3 monitors at min window width

    def __init__(self, idx: int, label: str, spec: str = "", text: str = "", parent=None):
        super().__init__(parent)
        self.idx = idx          # dxcam index at enumeration time (-1 for auto)
        self.label = label
        self.spec = spec or str(idx)   # what gets saved: "auto" or a device name
        self.setCheckable(True)
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setIconSize(self.THUMB)
        self.setIcon(QtGui.QIcon(self._make_placeholder()))
        self.setText(text or f"{idx}: {label}")
        self.setToolTip(label)
        self.setStyleSheet(
            "QToolButton { padding: 6px; border: 1px solid palette(mid);"
            " border-radius: 4px; }"
            "QToolButton:checked { border: 2px solid palette(highlight); }"
            "QToolButton:hover:!checked { border-color: palette(light); }"
        )

    def set_thumbnail(self, pixmap: QtGui.QPixmap):
        if pixmap is not None and not pixmap.isNull():
            self.setIcon(QtGui.QIcon(pixmap))

    def _make_placeholder(self) -> QtGui.QPixmap:
        return self._make_placeholder_text("loading...")

    def _make_placeholder_text(self, text: str) -> QtGui.QPixmap:
        pix = QtGui.QPixmap(self.THUMB)
        pix.fill(QtGui.QColor(35, 35, 35))
        p = QtGui.QPainter(pix)
        p.setPen(QtGui.QColor(170, 170, 170))
        p.drawText(pix.rect(), int(QtCore.Qt.AlignmentFlag.AlignCenter), text)
        p.end()
        return pix


class MonitorPicker(QtWidgets.QWidget):
    """Thumbnail-based monitor selector — replaces the index dropdown.

    Shows one MonitorTile per dxcam output with a live thumbnail, plus a
    Refresh button to re-detect after plugging/unplugging displays.
    """
    selection_changed = QtCore.pyqtSignal(str)   # the selected spec

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tiles: list[MonitorTile] = []
        self._selected_spec = screens.AUTO

        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idToggled.connect(self._on_button_toggled)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self._row = QtWidgets.QHBoxLayout()
        self._row.setSpacing(6)
        outer.addLayout(self._row)

        bottom = QtWidgets.QHBoxLayout()
        self._readout = QtWidgets.QLabel("(no monitor)")
        self._readout.setStyleSheet("color: gray;")
        self._refresh_btn = QtWidgets.QPushButton("Refresh")
        self._refresh_btn.setToolTip("Re-detect monitors and refresh thumbnails")
        self._refresh_btn.clicked.connect(self.refresh)
        bottom.addWidget(self._readout, 1)
        bottom.addWidget(self._refresh_btn)
        outer.addLayout(bottom)

        self.refresh()

    def selected_spec(self) -> str:
        """"auto" or a device name — never a bare index (they are not stable)."""
        return self._selected_spec

    def set_selected_spec(self, spec: str) -> bool:
        """Check the tile for `spec` if present. Returns True on match."""
        for t in self._tiles:
            if t.spec.lower() == (spec or "").lower():
                t.setChecked(True)
                return True
        return False

    def selected_index(self) -> int:
        """Resolve the selection to a dxcam index now (-1 if it cannot be)."""
        out, _why = screens.resolve(self._selected_spec)
        return out.idx if out else -1

    def refresh(self):
        # Tear down existing tiles before re-enumerating.
        for tile in self._tiles:
            self._group.removeButton(tile)
            self._row.removeWidget(tile)
            tile.deleteLater()
        self._tiles = []
        while self._row.count():
            item = self._row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        outputs = screens.enumerate_outputs()
        if not outputs:
            note = QtWidgets.QLabel("(could not enumerate monitors; will use the primary)")
            note.setStyleSheet("color: gray;")
            self._row.addWidget(note)
            self._selected_spec = screens.AUTO
            self._readout.setText("Selected: auto (assumed)")
            return

        auto = MonitorTile(-1, "Follow the League game window; otherwise the primary display",
                           spec=screens.AUTO, text="Auto\n(game window)", parent=self)
        auto.setIcon(QtGui.QIcon(auto._make_placeholder_text("auto")))
        self._row.addWidget(auto)
        self._group.addButton(auto, 0)
        self._tiles.append(auto)
        for n, o in enumerate(outputs, 1):
            text = f"{o.short}  {o.width}x{o.height}" + ("\nprimary" if o.primary else f"\n@({o.left},{o.top})")
            tile = MonitorTile(o.idx, o.describe(), spec=o.device, text=text, parent=self)
            self._row.addWidget(tile)
            self._group.addButton(tile, n)
            self._tiles.append(tile)

        # Preserve the previous selection if that display still exists; else auto.
        if not self.set_selected_spec(self._selected_spec):
            self._tiles[0].setChecked(True)
        self._update_readout()

        # Grab thumbnails one at a time, processing events between each so the
        # UI stays responsive (each grab is ~50-300ms depending on resolution).
        self._refresh_btn.setEnabled(False)
        try:
            for tile in list(self._tiles):
                if tile.idx < 0:
                    continue
                QtWidgets.QApplication.processEvents()
                pix = _grab_monitor_thumbnail(tile.idx, MonitorTile.THUMB)
                if pix is not None:
                    tile.set_thumbnail(pix)
        finally:
            self._refresh_btn.setEnabled(True)

    def _on_button_toggled(self, button_id: int, checked: bool):
        if not checked:
            return
        btn = self._group.button(button_id)
        self._selected_spec = getattr(btn, "spec", screens.AUTO)
        self._update_readout()
        self.selection_changed.emit(self._selected_spec)

    def _update_readout(self):
        for tile in self._tiles:
            if tile.spec == self._selected_spec:
                self._readout.setStyleSheet("")
                self._readout.setText(f"Selected: {tile.label}")
                return


# Spell highlight colours for the live "Now" line in LoadoutPanel.
SPELL_MATCH_COLOR = "#27ae60"     # selected spell equals the saved/locked one
SPELL_MISMATCH_COLOR = "#e67e22"  # selected spell differs from the saved one
SPELL_NEUTRAL_COLOR = "gray"      # nothing saved to compare against / unset


class LoadoutPanel(QtWidgets.QGroupBox):
    """Per-champion loadout memory: shows last spells/runes for the picked champion.

    Visible only while a champ-select session is live. Apply pushes the stored
    loadout back via apply_loadout — spells via PATCH /my-selection, runes
    written into the dedicated SCRATCH_PAGE_NAME page.

    The "Now" line tracks the spells currently selected in champ select on
    every session frame and colours each one against the saved pair. "Lock
    spells" pins the pair for this champion: the tracker re-applies it on pick
    and session-end recording no longer overwrites it.
    """

    def __init__(self, parent=None):
        super().__init__("Loadout memory", parent)
        self._tracker: "loadouts.LoadoutTracker | None" = None
        self._in_session = False
        self._current_champ_id = 0
        self._current_spells: tuple[int, int] = (0, 0)
        self._pick_locked = False
        self._current_loadout: "loadouts.Loadout | None" = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(4)

        self._champ_label = QtWidgets.QLabel("(not in champ select)")
        self._champ_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._champ_label)

        self._now_label = QtWidgets.QLabel("")
        self._now_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self._now_label.setToolTip(
            "Summoner spells currently selected in champ select.\n"
            "Green = matches the saved pair, orange = differs."
        )
        layout.addWidget(self._now_label)

        self._loadout_label = QtWidgets.QLabel("")
        self._loadout_label.setWordWrap(True)
        layout.addWidget(self._loadout_label)

        self._meta_label = QtWidgets.QLabel("")
        self._meta_label.setStyleSheet("color: gray;")
        layout.addWidget(self._meta_label)

        bottom = QtWidgets.QHBoxLayout()
        self._apply_btn = QtWidgets.QPushButton(f"Apply  {loadouts.SCRATCH_PAGE_NAME}")
        self._apply_btn.setToolTip(
            "Set summoner spells via the LCU and overwrite the dedicated\n"
            f"'{loadouts.SCRATCH_PAGE_NAME}' rune page with the saved snapshot,\n"
            "then make it current. Your other rune pages are not modified."
        )
        self._apply_btn.clicked.connect(self._apply)
        self._apply_btn.setEnabled(False)
        self._lock_check = QtWidgets.QCheckBox("Lock spells")
        self._lock_check.setToolTip(
            "Pin the summoner spells for this champion.\n"
            "Locks the pair selected right now (or the saved pair if none is\n"
            "selected yet). While locked: the pair is re-applied automatically\n"
            "each time you pick this champion, and the spells you last played\n"
            "no longer overwrite it. Runes are unaffected."
        )
        self._lock_check.setEnabled(False)
        self._lock_check.toggled.connect(self._on_lock_toggled)
        self._status_label = QtWidgets.QLabel("")
        self._status_label.setStyleSheet("color: gray;")
        bottom.addWidget(self._apply_btn)
        bottom.addWidget(self._lock_check)
        bottom.addWidget(self._status_label, 1)
        layout.addLayout(bottom)

        self.setVisible(False)

    def attach_tracker(self, tracker: "loadouts.LoadoutTracker | None"):
        self._tracker = tracker
        if tracker is None:
            self._reset()

    def _reset(self):
        self._in_session = False
        self._current_champ_id = 0
        self._current_spells = (0, 0)
        self._pick_locked = False
        self._current_loadout = None
        self._champ_label.setText("(not in champ select)")
        self._now_label.setText("")
        self._loadout_label.setText("")
        self._meta_label.setText("")
        self._status_label.setText("")
        self._apply_btn.setEnabled(False)
        self._set_lock_checked(False)
        self._lock_check.setEnabled(False)
        self.setVisible(False)

    def _set_lock_checked(self, checked: bool):
        # Programmatic sync must not fire the user-toggle handler.
        self._lock_check.blockSignals(True)
        self._lock_check.setChecked(checked)
        self._lock_check.blockSignals(False)

    @QtCore.pyqtSlot(object)
    def on_session_changed(self, session):
        if not isinstance(session, dict):
            self._reset()
            return
        cell = loadouts._find_my_cell(session)
        if cell is None:
            self._reset()
            return
        champ_id = loadouts._intended_champion_id(cell)
        spells = loadouts.current_spells(cell)
        pick_locked = loadouts.pick_locked_in(session)
        # Cheap no-op: ignore identical updates so the apply-status flash isn't
        # wiped out by every websocket frame. The first frame of a session
        # always renders, even if it looks like the idle state (no pick yet).
        if (self._in_session and champ_id == self._current_champ_id
                and spells == self._current_spells
                and pick_locked == self._pick_locked):
            return
        champ_changed = not self._in_session or champ_id != self._current_champ_id
        self._in_session = True
        self._current_champ_id = champ_id
        self._current_spells = spells
        self._pick_locked = pick_locked
        self.setVisible(True)
        if champ_changed:
            self._status_label.setText("")
            self._refresh_loadout(champ_id)
        self._refresh_champ_label()
        self._refresh_now_line()

    def _refresh_champ_label(self):
        champ_id = self._current_champ_id
        if champ_id <= 0:
            self._champ_label.setText("Champ select — no champion picked yet")
            return
        name = self._tracker.champion_name(champ_id) if self._tracker else f"#{champ_id}"
        state = "locked in" if self._pick_locked else "hovering"
        self._champ_label.setText(f"Champ select: {name}  ({state})")

    def _refresh_loadout(self, champ_id: int):
        """Reload the saved row for a newly picked champion and redraw its lines."""
        if champ_id <= 0:
            self._loadout_label.setText("")
            self._meta_label.setText("")
            self._current_loadout = None
            self._apply_btn.setEnabled(False)
            self._set_lock_checked(False)
            self._lock_check.setEnabled(False)
            return
        loadout = None
        puuid = self._tracker.puuid if self._tracker else None
        if puuid:
            loadout = loadouts.get_loadout(puuid, champ_id)
        self._current_loadout = loadout
        # Locking is possible as soon as a champion is picked — with no history
        # it pins whatever is selected right now.
        self._lock_check.setEnabled(bool(puuid))
        self._set_lock_checked(bool(loadout and loadout.locked))
        self._refresh_saved_line()

    def _refresh_saved_line(self):
        loadout = self._current_loadout
        if loadout is None:
            self._loadout_label.setText(
                "(no history yet — your next game on this champion will be saved)"
            )
            self._meta_label.setText("")
            self._apply_btn.setEnabled(False)
            return
        name = (self._tracker.champion_name(loadout.champion_id)
                if self._tracker else f"#{loadout.champion_id}")
        spells = (
            f"D = {loadouts.spell_name(loadout.spell1_id)},   "
            f"F = {loadouts.spell_name(loadout.spell2_id)}"
        )
        head = f"Locked on {name}:" if loadout.locked else f"Last on {name}:"
        runes_name = (loadout.rune_page or {}).get("name") or "(no runes saved)"
        self._loadout_label.setText(f"{head}  {spells}\nRunes:  {runes_name}")
        last = loadout.last_seen.astimezone().strftime("%Y-%m-%d %H:%M")
        played = f"played {loadout.count}x" if loadout.count else "never played"
        self._meta_label.setText(f"Last seen {last} · {played}")
        self._apply_btn.setEnabled(True)

    def _refresh_now_line(self):
        """Redraw the live D/F line, colouring each spell against the saved pair."""
        if self._current_champ_id <= 0:
            self._now_label.setText("")
            return
        saved = self._current_loadout
        saved_pair = (saved.spell1_id, saved.spell2_id) if saved else None
        parts = []
        for key, spell_id, saved_id in (
            ("D", self._current_spells[0], saved_pair[0] if saved_pair else 0),
            ("F", self._current_spells[1], saved_pair[1] if saved_pair else 0),
        ):
            if spell_id <= 0:
                color, text = SPELL_NEUTRAL_COLOR, "—"
            else:
                text = loadouts.spell_name(spell_id)
                if saved_pair is None:
                    color = SPELL_NEUTRAL_COLOR
                elif spell_id == saved_id:
                    color = SPELL_MATCH_COLOR
                else:
                    color = SPELL_MISMATCH_COLOR
            parts.append(
                f"{key} = <span style='color:{color}; font-weight:bold'>{text}</span>"
            )
        suffix = ""
        if saved_pair is not None and self._current_spells == saved_pair:
            suffix = (f"&nbsp;&nbsp;<span style='color:{SPELL_MATCH_COLOR}'>"
                      f"{'🔒 locked in' if saved.locked else '✓ matches saved'}</span>")
        elif saved_pair is not None and all(s > 0 for s in self._current_spells):
            suffix = (f"&nbsp;&nbsp;<span style='color:{SPELL_MISMATCH_COLOR}'>"
                      "≠ saved</span>")
        self._now_label.setText("Now:  " + ",&nbsp;&nbsp; ".join(parts) + suffix)

    def _on_lock_toggled(self, checked: bool):
        puuid = self._tracker.puuid if self._tracker else None
        champ_id = self._current_champ_id
        if not puuid or champ_id <= 0:
            self._set_lock_checked(False)
            return
        # Lock what is selected right now; fall back to the saved pair when
        # the selection is incomplete (set_locked treats zeros as "keep").
        s1, s2 = self._current_spells
        if not (s1 > 0 and s2 > 0):
            s1, s2 = 0, 0
        try:
            loadout = loadouts.set_locked(puuid, champ_id, checked, s1, s2)
        except Exception as e:
            self._status_label.setStyleSheet("color: #c0392b;")
            self._status_label.setText(f"lock failed: {e}")
            log.exception("set_locked from GUI raised")
            self._set_lock_checked(not checked)
            return
        if checked and loadout is None:
            # No row and nothing selected to pin: nothing to lock yet.
            self._status_label.setStyleSheet("color: gray;")
            self._status_label.setText("pick both spells first")
            self._set_lock_checked(False)
            return
        self._current_loadout = loadout
        if self._tracker is not None:
            self._tracker.forget_auto_apply(champ_id)
        self._refresh_saved_line()
        self._refresh_now_line()
        self._status_label.setStyleSheet("color: gray;")
        self._status_label.setText("spells locked" if checked else "unlocked")
        # A fresh lock whose pair isn't what's selected: apply it now rather
        # than waiting for the next champ select.
        if (checked and loadout is not None and self._tracker is not None
                and self._tracker.client is not None
                and self._current_spells != (loadout.spell1_id, loadout.spell2_id)):
            try:
                loadouts.apply_spells(self._tracker.client, loadout)
                self._status_label.setStyleSheet("color: #27ae60;")
                self._status_label.setText("locked + applied")
            except Exception as e:
                self._status_label.setStyleSheet("color: #c0392b;")
                self._status_label.setText(f"locked, apply failed: {e}")
                log.exception("apply_spells after lock raised")

    def _apply(self):
        if (self._tracker is None or self._tracker.client is None
                or self._current_loadout is None):
            self._status_label.setStyleSheet("color: #c0392b;")
            self._status_label.setText("not connected")
            return
        # The PATCH + PUT chain is ~hundreds of ms on the LCU. Disable the
        # button so impatient double-clicks don't queue a duplicate apply.
        self._apply_btn.setEnabled(False)
        self._status_label.setStyleSheet("color: gray;")
        self._status_label.setText("applying...")
        QtWidgets.QApplication.processEvents()
        try:
            loadouts.apply_loadout(self._tracker.client, self._current_loadout)
            self._status_label.setStyleSheet("color: #27ae60;")
            self._status_label.setText("applied")
        except Exception as e:
            self._status_label.setStyleSheet("color: #c0392b;")
            self._status_label.setText(f"failed: {e}")
            log.exception("apply_loadout from GUI raised")
        finally:
            self._apply_btn.setEnabled(True)


class PostgameDialog(QtWidgets.QDialog):
    """Postgame reminder: keep or change the summoner spells for the champion just played.

    Non-modal and stays on top so it is visible over the League client's
    end-of-game screen. "Keep" locks the pair that will be used next time
    (the played pair when nothing is locked). "Change…" reveals two spell
    pickers; "Save" locks the chosen pair. Runes are never touched here.
    """

    def __init__(self, puuid: str, played: "loadouts.PlayedGame",
                 champion_name: str, parent=None):
        super().__init__(parent)
        self._puuid = puuid
        self._played = played
        self.setWindowTitle(f"Postgame — {champion_name}")
        self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)

        current = loadouts.get_loadout(puuid, played.champion_id)
        self._locked = bool(current and current.locked)
        if self._locked:
            next_pair = (current.spell1_id, current.spell2_id)
        else:
            next_pair = (played.spell1_id, played.spell2_id)
        self._next_pair = next_pair

        layout = QtWidgets.QVBoxLayout(self)
        played_txt = (f"You played <b>{champion_name}</b> with "
                      f"D = <b>{loadouts.spell_name(played.spell1_id)}</b>, "
                      f"F = <b>{loadouts.spell_name(played.spell2_id)}</b>.")
        layout.addWidget(QtWidgets.QLabel(played_txt))
        if self._locked and next_pair != (played.spell1_id, played.spell2_id):
            nxt = (f"Locked for next time: "
                   f"<span style='color:{SPELL_MISMATCH_COLOR}'>"
                   f"{loadouts.spell_name(next_pair[0])} + "
                   f"{loadouts.spell_name(next_pair[1])}</span> (differs from this game).")
        elif self._locked:
            nxt = (f"<span style='color:{SPELL_MATCH_COLOR}'>🔒 Locked</span> — "
                   "this pair is applied automatically next time.")
        else:
            nxt = ("Not locked — next time starts from this pair, "
                   "but whatever you play last overwrites it.")
        self._next_label = QtWidgets.QLabel(nxt)
        self._next_label.setWordWrap(True)
        layout.addWidget(self._next_label)

        # Change… reveals this row.
        self._change_box = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(self._change_box)
        row.setContentsMargins(0, 0, 0, 0)
        self._d_combo = QtWidgets.QComboBox()
        self._f_combo = QtWidgets.QComboBox()
        for combo in (self._d_combo, self._f_combo):
            for sid, name in sorted(loadouts.SUMMONER_SPELL_NAMES.items(),
                                    key=lambda kv: kv[1]):
                combo.addItem(name, sid)
        self._select_spell(self._d_combo, next_pair[0])
        self._select_spell(self._f_combo, next_pair[1])
        row.addWidget(QtWidgets.QLabel("D:"))
        row.addWidget(self._d_combo, 1)
        row.addWidget(QtWidgets.QLabel("F:"))
        row.addWidget(self._f_combo, 1)
        self._change_box.setVisible(False)
        layout.addWidget(self._change_box)

        self._status = QtWidgets.QLabel("")
        self._status.setStyleSheet("color: gray;")
        layout.addWidget(self._status)

        buttons = QtWidgets.QHBoxLayout()
        self._keep_btn = QtWidgets.QPushButton(
            "Keep" if self._locked else "Keep (lock these)")
        self._keep_btn.setToolTip(
            "Leave the lock as it is." if self._locked else
            "Lock the pair you just played so it is applied automatically\n"
            "next time you pick this champion."
        )
        self._keep_btn.clicked.connect(self._keep)
        self._change_btn = QtWidgets.QPushButton("Change…")
        self._change_btn.clicked.connect(self._show_change)
        self._save_btn = QtWidgets.QPushButton("Save + lock")
        self._save_btn.clicked.connect(self._save_change)
        self._save_btn.setVisible(False)
        self._dismiss_btn = QtWidgets.QPushButton("Dismiss")
        self._dismiss_btn.clicked.connect(self.reject)
        buttons.addWidget(self._keep_btn)
        buttons.addWidget(self._change_btn)
        buttons.addWidget(self._save_btn)
        buttons.addStretch()
        buttons.addWidget(self._dismiss_btn)
        layout.addLayout(buttons)

    @staticmethod
    def _select_spell(combo: QtWidgets.QComboBox, spell_id: int):
        idx = combo.findData(spell_id)
        if idx < 0 and spell_id > 0:
            combo.addItem(loadouts.spell_name(spell_id), spell_id)
            idx = combo.count() - 1
        combo.setCurrentIndex(max(idx, 0))

    def chosen_pair(self) -> tuple[int, int]:
        return (int(self._d_combo.currentData()), int(self._f_combo.currentData()))

    def _keep(self):
        if self._locked:
            self.accept()
            return
        self._lock_pair(self._next_pair)

    def _show_change(self):
        self._change_box.setVisible(True)
        self._save_btn.setVisible(True)
        self._change_btn.setVisible(False)
        self._keep_btn.setVisible(False)

    def _save_change(self):
        d, f = self.chosen_pair()
        if d == f:
            self._status.setStyleSheet("color: #c0392b;")
            self._status.setText("D and F must differ")
            return
        self._lock_pair((d, f))

    def _lock_pair(self, pair: tuple[int, int]):
        try:
            loadouts.set_locked(self._puuid, self._played.champion_id, True,
                                pair[0], pair[1])
        except Exception as e:
            self._status.setStyleSheet("color: #c0392b;")
            self._status.setText(f"failed: {e}")
            log.exception("postgame lock failed")
            return
        self.accept()


class AudioMeterWorker(QtCore.QObject):
    """Runs preflight.measure_audio_levels for a list of devices, one at a time.

    Emits `result` per device so the UI can update incrementally instead of
    waiting for the whole batch. Sequential — running multiple `-f dshow`
    captures in parallel can fight for the same device on some drivers.
    """
    result = QtCore.pyqtSignal(str, object)  # (device_name, AudioLevels)
    finished = QtCore.pyqtSignal()

    def __init__(self, devices: list, duration_s: float = 3.0):
        super().__init__()
        self._devices = list(devices)
        self._duration = duration_s

    @QtCore.pyqtSlot()
    def run(self):
        for name in self._devices:
            try:
                levels = preflight.measure_audio_levels(name, self._duration)
            except Exception as e:
                levels = preflight.AudioLevels(False, None, None, str(e))
            self.result.emit(name, levels)
        self.finished.emit()


def _make_tray_icon(color: QtGui.QColor) -> QtGui.QIcon:
    """Build a simple solid-color circle as the tray icon (no asset files)."""
    pix = QtGui.QPixmap(64, 64)
    pix.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setBrush(QtGui.QBrush(color))
    painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 90), 2))
    painter.drawEllipse(8, 8, 48, 48)
    painter.end()
    return QtGui.QIcon(pix)


# Single-source-of-truth for the action keys passed via --jump and over IPC.
# Kept in lockstep with windows_integration.DEFAULT_JUMP_TASKS — those are the
# labels users see, these are the verbs the running instance dispatches on.
JUMP_ACTIONS = {
    "show",
    "toggle-record",
    "open-last",
    "toggle-auto-record",
    "open-data-dir",
    "open-web",
    "open-terminal",
}


class CaptureWorker(QtCore.QObject):
    """Drives Recorder.start/run/shutdown inside a worker thread.

    Emits started(width, height) once capture is live, and finished() with
    a (frames, elapsed, error_or_none) tuple once shutdown completes.
    """
    started = QtCore.pyqtSignal(int, int)
    finished = QtCore.pyqtSignal(int, float, object)

    def __init__(self, recorder: Recorder):
        super().__init__()
        self.recorder = recorder

    @QtCore.pyqtSlot()
    def run(self):
        try:
            self.recorder.start()
        except Exception as e:
            self.finished.emit(0, 0.0, f"Could not start: {e}")
            return
        self.started.emit(self.recorder.width, self.recorder.height)
        self.recorder.run()
        self.recorder.shutdown()
        self.finished.emit(
            self.recorder.frame_count,
            self.recorder.elapsed,
            self.recorder.error,
        )


class HighlightWorker(QtCore.QObject):
    """Runs highlights.cut_highlights() off the GUI thread.

    Emits finished(result_or_none, error_or_none). ffmpeg re-encodes each
    window, which can take tens of seconds for a long game, so this must
    never run on the GUI thread.
    """
    finished = QtCore.pyqtSignal(object, object)

    def __init__(self, video: str, sidecar: "highlights.Sidecar", mode: str):
        super().__init__()
        self.video = video
        self.sidecar = sidecar
        self.mode = mode

    @QtCore.pyqtSlot()
    def run(self):
        try:
            res = highlights.cut_highlights(self.video, self.sidecar, mode=self.mode)
        except Exception as e:
            log.exception("highlight cut failed")
            self.finished.emit(None, str(e))
            return
        self.finished.emit(res, None)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen Recorder")
        self.setMinimumWidth(420)

        self.recorder: Recorder = None
        self.thread: QtCore.QThread = None
        self.worker: CaptureWorker = None
        self._last_output_path: str = None
        self._settings = QtCore.QSettings()
        self._lcu_bridge: "LCUBridge" = None
        self._lcu_stream = None
        self._loadout_bridge: "LoadoutBridge" = None
        self._loadout_tracker: "loadouts.LoadoutTracker" = None
        # Tracks whether the *current* recording was triggered by auto-record.
        # We only auto-stop a recording we auto-started — manual recordings
        # ignore phase transitions so the user keeps full control.
        self._auto_started = False
        self._lcu_phase = ""
        self._lcu_connected = False
        # Local API + browser view (server.py). Started after the UI exists so
        # status/actions have something to talk to; stopped on real quit.
        self._api: "server.Server | None" = None
        self._api_bridge = ApiBridge()
        self._api_bridge.start_record.connect(self._api_start_record)
        self._api_bridge.stop_record.connect(self._stop_recording)
        self._api_bridge.show.connect(self._show_window)
        self._api_bridge.quit.connect(self._quit)

        # Highlights: a Tagger writes markers beside the recording; after it
        # ends a HighlightWorker cuts them. See highlights.py.
        self._sidecar: "highlights.Sidecar | None" = None
        self._tagger: "highlights.Tagger | None" = None
        self._hl_thread: "QtCore.QThread | None" = None
        self._hl_worker: "HighlightWorker | None" = None
        self._last_highlights_dir: "str | None" = None

        # Tray-icon state. closeEvent hides the window unless _really_quit is
        # set by the tray's Quit action (or by closeEvent's own re-entry path
        # if the tray isn't available, so X still works as a quit fallback).
        self.tray: "QtWidgets.QSystemTrayIcon | None" = None
        self._tray_icon_idle: "QtGui.QIcon | None" = None
        self._tray_icon_recording: "QtGui.QIcon | None" = None
        self._action_show: "QtGui.QAction | None" = None
        self._action_record: "QtGui.QAction | None" = None
        self._action_open_last: "QtGui.QAction | None" = None
        self._action_auto_record: "QtGui.QAction | None" = None
        self._action_launch_on_login: "QtGui.QAction | None" = None
        self._really_quit = False

        self._build_ui()
        self._build_menu()
        self._populate_encoders()
        self._populate_audio_devices()
        self._load_on_exist_pref()
        self._load_auto_record_pref()
        self._load_highlight_prefs()
        self._load_persistent_prefs()
        self._start_api()
        self._build_tray()

        self.poll_timer = QtCore.QTimer(self)
        self.poll_timer.setInterval(250)
        self.poll_timer.timeout.connect(self._poll_metrics)

        self._hotkey_target = None
        self._mark_hotkey_target = None
        self._listener = None
        self._hotkey_bridge = None
        if _pynput_keyboard is not None:
            self._hotkey_bridge = HotkeyBridge()
            self._hotkey_bridge.toggle_pressed.connect(self._toggle_record)
            self._hotkey_bridge.mark_pressed.connect(self._on_mark_pressed)
            self._set_hotkey(self._load_hotkey_pref() or _pynput_keyboard.Key.f9)
            self._set_mark_hotkey(
                self._load_hotkey_pref("hotkey/mark") or _pynput_keyboard.Key.f10)
        else:
            self.hotkey_btn.setText("(pynput missing)")
            self.hotkey_btn.setEnabled(False)
            self.hotkey_mark_btn.setText("(pynput missing)")
            self.hotkey_mark_btn.setEnabled(False)
            self.statusBar().showMessage(
                "Install 'pynput' to enable global hotkeys (pip install pynput)."
            )

    def _build_ui(self):
        central = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(central)

        out_row = QtWidgets.QHBoxLayout()
        self.output_edit = QtWidgets.QLineEdit("recording.mp4")
        self.output_edit.editingFinished.connect(self._save_output_pref)
        browse = QtWidgets.QPushButton("Browse...")
        browse.clicked.connect(self._browse_output)
        out_row.addWidget(self.output_edit)
        out_row.addWidget(browse)
        form.addRow("Output:", out_row)

        # Policy for what to do when the target file already exists.
        # Values are stable strings — also used as QSettings keys.
        self.on_exist_combo = QtWidgets.QComboBox()
        self.on_exist_combo.addItem("Append (n)", "suffix")
        self.on_exist_combo.addItem("Append timestamp", "timestamp")
        self.on_exist_combo.addItem("Ask before overwrite", "prompt")
        self.on_exist_combo.setToolTip(
            "How to handle the output filename:\n"
            "  Append (n)        -> recording (1).mp4, recording (2).mp4, ...\n"
            "  Append timestamp  -> recording_2026-04-30_19-46-10.mp4 (always)\n"
            "  Ask before overwrite -> confirm dialog if file exists"
        )
        self.on_exist_combo.currentIndexChanged.connect(self._save_on_exist_pref)
        form.addRow("If exists:", self.on_exist_combo)

        self.monitor_picker = MonitorPicker()
        self.monitor_picker.selection_changed.connect(self._save_monitor_pref)
        form.addRow("Monitor:", self.monitor_picker)

        self.fps_spin = QtWidgets.QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(60)
        self.fps_spin.valueChanged.connect(self._save_fps_pref)
        form.addRow("FPS:", self.fps_spin)

        self.encoder_combo = QtWidgets.QComboBox()
        self.encoder_combo.currentIndexChanged.connect(self._save_encoder_pref)
        form.addRow("Encoder:", self.encoder_combo)

        self.audio_list = QtWidgets.QListWidget()
        self.audio_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        self.audio_list.setMaximumHeight(110)
        self.audio_list.itemChanged.connect(self._save_audio_pref)
        self.audio_list.setToolTip(
            "Tick one device for plain audio capture, or multiple to mix them "
            "(e.g. microphone + Stereo Mix for system audio)."
        )
        audio_box = QtWidgets.QVBoxLayout()
        audio_box.setContentsMargins(0, 0, 0, 0)
        audio_box.addWidget(self.audio_list)
        test_row = QtWidgets.QHBoxLayout()
        self.audio_test_btn = QtWidgets.QPushButton("Test ticked (3s)")
        self.audio_test_btn.setToolTip(
            "Capture 3 seconds from each ticked device and show peak/mean dBFS. "
            "Speak / play audio during the test."
        )
        self.audio_test_btn.clicked.connect(self._test_audio_devices)
        self.audio_test_label = QtWidgets.QLabel("")
        self.audio_test_label.setStyleSheet("color: gray;")
        test_row.addWidget(self.audio_test_btn)
        test_row.addWidget(self.audio_test_label, 1)
        audio_box.addLayout(test_row)
        form.addRow("Audio:", audio_box)
        self.audio_hint = QtWidgets.QLabel("")
        self.audio_hint.setWordWrap(True)
        self.audio_hint.setStyleSheet("color: #e67e22;")
        self.audio_hint.setVisible(False)
        form.addRow("", self.audio_hint)

        self.region_check = QtWidgets.QCheckBox("Limit to region")
        self.region_check.toggled.connect(self._toggle_region_inputs)
        self.region_check.toggled.connect(self._save_region_pref)
        form.addRow(self.region_check)

        region_grid = QtWidgets.QGridLayout()
        self.region_inputs = {}
        for i, (name, default) in enumerate([("left", 0), ("top", 0),
                                             ("right", 1920), ("bottom", 1080)]):
            spin = QtWidgets.QSpinBox()
            spin.setRange(0, 99999)
            spin.setValue(default)
            spin.setEnabled(False)
            spin.valueChanged.connect(self._save_region_pref)
            self.region_inputs[name] = spin
            region_grid.addWidget(QtWidgets.QLabel(f"{name}:"), i // 2, (i % 2) * 2)
            region_grid.addWidget(spin, i // 2, (i % 2) * 2 + 1)
        form.addRow("", region_grid)

        self.region_pick_btn = QtWidgets.QPushButton("Pick region on screen...")
        self.region_pick_btn.clicked.connect(self._open_region_picker)
        form.addRow("", self.region_pick_btn)

        hotkey_row = QtWidgets.QHBoxLayout()
        self.hotkey_btn = QtWidgets.QPushButton("F9")
        self.hotkey_btn.setToolTip("Click to change the global toggle hotkey.")
        self.hotkey_btn.clicked.connect(self._capture_hotkey)
        hotkey_row.addWidget(self.hotkey_btn)
        hotkey_row.addSpacing(12)
        hotkey_row.addWidget(QtWidgets.QLabel("Mark highlight:"))
        self.hotkey_mark_btn = QtWidgets.QPushButton("F10")
        self.hotkey_mark_btn.setToolTip(
            "Click to change the global 'mark highlight' hotkey.\n"
            "Pressing it while recording drops a marker at the current moment;\n"
            "the clip around it is cut when the recording ends."
        )
        self.hotkey_mark_btn.clicked.connect(self._capture_mark_hotkey)
        hotkey_row.addWidget(self.hotkey_mark_btn)
        hotkey_row.addStretch()
        form.addRow("Hotkey:", hotkey_row)

        league_row = QtWidgets.QHBoxLayout()
        self.lcu_check = QtWidgets.QCheckBox("Loadout memory")
        self.lcu_check.setToolTip(
            "Connect to the League client: remember spells/runes per champion,\n"
            "show them in champ select, and apply locked spells. No recording."
        )
        self.lcu_check.toggled.connect(self._toggle_lcu)
        self.postgame_check = QtWidgets.QCheckBox("Postgame reminder")
        self.postgame_check.setToolTip(
            "After each match, ask whether to keep or change the summoner\n"
            "spells for the champion you just played."
        )
        self.postgame_check.toggled.connect(self._save_postgame_pref)
        league_row.addWidget(self.lcu_check)
        league_row.addWidget(self.postgame_check)
        league_row.addStretch()
        form.addRow("League:", league_row)
        self.auto_record_status = QtWidgets.QLabel("(off)")
        form.addRow("", self.auto_record_status)

        self.auto_record_check = QtWidgets.QCheckBox("League matches (opt-in)")
        self.auto_record_check.setToolTip(
            "Start recording when the League gameflow phase becomes 'InProgress'\n"
            "and stop on any other phase. Manual recordings are unaffected.\n"
            "Off by default; needs the League connection above."
        )
        self.auto_record_check.toggled.connect(self._toggle_auto_record)
        form.addRow("Auto-record:", self.auto_record_check)

        hl_row = QtWidgets.QHBoxLayout()
        self.auto_cut_check = QtWidgets.QCheckBox("Cut after recording")
        self.auto_cut_check.setToolTip(
            "When a recording ends with highlight markers (League kills, deaths,\n"
            "multikills, objectives, or the mark hotkey), cut one clip per moment\n"
            "plus a combined reel into <recording>_highlights\\ next to the file."
        )
        self.auto_cut_check.toggled.connect(self._save_auto_cut_pref)
        self.hl_fast_check = QtWidgets.QCheckBox("Fast cut")
        self.hl_fast_check.setToolTip(
            "Stream-copy instead of re-encoding: instant, but each clip starts on\n"
            "the nearest keyframe (up to 2 s early). Off = frame-accurate re-encode\n"
            "with the recording encoder."
        )
        self.hl_fast_check.toggled.connect(self._save_hl_fast_pref)
        hl_row.addWidget(self.auto_cut_check)
        hl_row.addWidget(self.hl_fast_check)
        hl_row.addStretch()
        form.addRow("Highlights:", hl_row)

        self.loadout_panel = LoadoutPanel()
        form.addRow(self.loadout_panel)

        self.status_label = QtWidgets.QLabel("Idle")
        form.addRow("Status:", self.status_label)
        self.metrics_label = QtWidgets.QLabel("Frames: 0   Elapsed: 0.0s   Avg fps: —")
        form.addRow("", self.metrics_label)

        result_row = QtWidgets.QHBoxLayout()
        self.open_btn = QtWidgets.QPushButton("Open")
        self.show_in_folder_btn = QtWidgets.QPushButton("Show in folder")
        self.open_btn.setEnabled(False)
        self.show_in_folder_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open_last_output)
        self.show_in_folder_btn.clicked.connect(self._show_last_output_in_folder)
        self.highlights_btn = QtWidgets.QPushButton("Highlights")
        self.highlights_btn.setEnabled(False)
        self.highlights_btn.clicked.connect(self._on_highlights_btn)
        result_row.addWidget(self.open_btn)
        result_row.addWidget(self.show_in_folder_btn)
        result_row.addWidget(self.highlights_btn)
        result_row.addStretch()
        form.addRow("", result_row)

        self.record_btn = QtWidgets.QPushButton("● Record")
        self.record_btn.setMinimumHeight(36)
        self.record_btn.clicked.connect(self._toggle_record)
        form.addRow(self.record_btn)

        self.setCentralWidget(central)

    def _build_menu(self):
        integration_menu = self.menuBar().addMenu("&Integration")
        self._action_launch_on_login = QtGui.QAction(
            "Launch on Windows &login", self
        )
        self._action_launch_on_login.setCheckable(True)
        self._action_launch_on_login.setChecked(
            windows_integration.is_launch_on_login_enabled()
        )
        self._action_launch_on_login.toggled.connect(self._toggle_launch_on_login)
        integration_menu.addAction(self._action_launch_on_login)

        install_shortcut_act = QtGui.QAction("&Install Start Menu shortcut...", self)
        install_shortcut_act.triggered.connect(self._install_shortcut)
        integration_menu.addAction(install_shortcut_act)

        refresh_jl_act = QtGui.QAction("Refresh taskbar &jump list", self)
        refresh_jl_act.triggered.connect(self._refresh_jump_list)
        integration_menu.addAction(refresh_jl_act)

        view_menu = self.menuBar().addMenu("&View")
        open_web = QtGui.QAction("Open &web view", self)
        open_web.setToolTip("The ledger and controls in your browser")
        open_web.triggered.connect(self._open_web_view)
        view_menu.addAction(open_web)
        open_term = QtGui.QAction("Open &terminal view", self)
        open_term.setToolTip("Watch every action live in a terminal")
        open_term.triggered.connect(self._open_terminal_view)
        view_menu.addAction(open_term)

        help_menu = self.menuBar().addMenu("&Help")
        open_log = QtGui.QAction("Open &log file", self)
        open_log.triggered.connect(self._open_log_file)
        help_menu.addAction(open_log)
        open_log_dir = QtGui.QAction("Open log &folder", self)
        open_log_dir.triggered.connect(self._open_log_folder)
        help_menu.addAction(open_log_dir)

    def _toggle_launch_on_login(self, on: bool):
        ok = windows_integration.set_launch_on_login(on)
        if not ok:
            QtWidgets.QMessageBox.warning(
                self, "Launch on login",
                "Could not update the HKCU\\Run entry. See the log for details."
            )
            # Revert the menu state to whatever the registry actually says,
            # without re-firing the toggle handler.
            self._action_launch_on_login.blockSignals(True)
            self._action_launch_on_login.setChecked(
                windows_integration.is_launch_on_login_enabled()
            )
            self._action_launch_on_login.blockSignals(False)

    def _install_shortcut(self):
        path = windows_integration.install_shortcut()
        if path:
            QtWidgets.QMessageBox.information(
                self, "Shortcut installed", f"Created:\n{path}"
            )
        else:
            QtWidgets.QMessageBox.warning(
                self, "Shortcut", "Could not create the shortcut. See the log for details."
            )

    def _refresh_jump_list(self):
        ok = windows_integration.refresh_jump_list()
        if not ok:
            QtWidgets.QMessageBox.warning(
                self, "Jump list",
                "Could not refresh the taskbar jump list. See the log for details."
            )

    def _open_log_file(self):
        path = logging_setup.log_path()
        if os.path.exists(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        else:
            QtWidgets.QMessageBox.information(
                self, "Log file", f"No log file yet at:\n{path}"
            )

    def _load_on_exist_pref(self):
        # Default: "suffix" — Windows-style (1), (2), ... avoids silent overwrite
        # while not being as noisy as a timestamp on every record.
        saved = self._settings.value("output/on_exist", "suffix", type=str)
        idx = self.on_exist_combo.findData(saved)
        if idx < 0:
            idx = self.on_exist_combo.findData("suffix")
        self.on_exist_combo.setCurrentIndex(idx)

    def _save_on_exist_pref(self):
        self._settings.setValue("output/on_exist", self.on_exist_combo.currentData())

    def _load_persistent_prefs(self):
        """Restore output path, fps, encoder, monitor, audio devices, and region.

        Called once at startup after widgets are populated. The change signals
        wired in _build_ui will trigger save handlers as we apply each value;
        those rewrites are no-ops since QSettings already holds the same data.
        """
        out = self._settings.value("output/path", "", type=str)
        if out:
            self.output_edit.setText(out)

        fps = self._settings.value("capture/fps", 60, type=int)
        self.fps_spin.setValue(fps)

        encoder = self._settings.value("capture/encoder", "", type=str)
        if encoder:
            idx = self.encoder_combo.findData(encoder)
            if idx >= 0:
                self.encoder_combo.setCurrentIndex(idx)

        spec = self._settings.value("capture/monitor_spec", "", type=str)
        if not spec:
            legacy = self._settings.value("capture/monitor", -1, type=int)
            if legacy >= 0:
                # A saved index is ambiguous (DXGI order is not stable), so it
                # becomes "auto" rather than a guess at which display it meant.
                log.info("capture/monitor=%d migrated to monitor_spec=auto", legacy)
                ledger.note("settings:capture/monitor",
                            f"legacy index {legacy} replaced by 'auto' (indices are not stable)")
            spec = screens.AUTO
        self.monitor_picker.set_selected_spec(spec)

        # QSettings.value with type=list is unreliable across backends; coerce
        # whatever shape comes back into a list of strings.
        raw = self._settings.value("audio/devices", [])
        if isinstance(raw, str):
            saved_audio = [raw] if raw else []
        elif raw is None:
            saved_audio = []
        else:
            saved_audio = [str(x) for x in raw]
        if saved_audio:
            wanted = set(saved_audio)
            # Block itemChanged so the per-tick save doesn't fire mid-load and
            # rewrite the saved list with a partial state — important when a
            # previously-saved device isn't plugged in right now.
            self.audio_list.blockSignals(True)
            try:
                for i in range(self.audio_list.count()):
                    item = self.audio_list.item(i)
                    name = item.data(QtCore.Qt.ItemDataRole.UserRole)
                    if name in wanted:
                        item.setCheckState(QtCore.Qt.CheckState.Checked)
            finally:
                self.audio_list.blockSignals(False)
            self._update_audio_hint()

        if self._settings.value("region/enabled", False, type=bool):
            for name in ("left", "top", "right", "bottom"):
                v = self._settings.value(f"region/{name}", None, type=int)
                if v is not None:
                    self.region_inputs[name].setValue(v)
            self.region_check.setChecked(True)

    def _save_output_pref(self):
        self._settings.setValue("output/path", self.output_edit.text())

    def _save_fps_pref(self):
        self._settings.setValue("capture/fps", self.fps_spin.value())

    def _save_encoder_pref(self):
        data = self.encoder_combo.currentData()
        if data:
            self._settings.setValue("capture/encoder", data)

    def _save_monitor_pref(self):
        self._settings.setValue("capture/monitor_spec", self.monitor_picker.selected_spec())

    def _update_audio_hint(self):
        """Two inputs are mixed into one track: a source on both is recorded twice."""
        checked = [self.audio_list.item(i).text() for i in range(self.audio_list.count())
                   if self.audio_list.item(i).checkState() == QtCore.Qt.CheckState.Checked]
        if len(checked) >= 2:
            self.audio_hint.setText(
                f"{len(checked)} inputs are mixed into one track. Anything routed to more than "
                "one of them (e.g. a mic sent to both VoiceMeeter buses) is recorded twice and "
                "sounds doubled. Route each source to one input, or tick one device.")
            self.audio_hint.setVisible(True)
        else:
            self.audio_hint.setVisible(False)

    def _save_audio_pref(self):
        self._update_audio_hint()
        # Devices the user has currently ticked.
        visible_checked = []
        visible_names = set()
        for i in range(self.audio_list.count()):
            item = self.audio_list.item(i)
            name = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if not name:
                continue
            visible_names.add(name)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                visible_checked.append(name)
        # Preserve saved entries for devices not currently enumerated (e.g. a
        # mic that's unplugged right now) so reconnecting it later restores
        # the tick instead of silently losing it.
        prior = self._settings.value("audio/devices", [])
        if isinstance(prior, str):
            prior = [prior] if prior else []
        elif prior is None:
            prior = []
        offline = [str(x) for x in prior if str(x) not in visible_names]
        self._settings.setValue("audio/devices", visible_checked + offline)

    def _save_region_pref(self):
        self._settings.setValue("region/enabled", self.region_check.isChecked())
        for name, spin in self.region_inputs.items():
            self._settings.setValue(f"region/{name}", spin.value())

    def _load_hotkey_pref(self, setting: str = "hotkey/target"):
        """Deserialize the saved hotkey into a pynput Key/KeyCode, or None."""
        if _pynput_keyboard is None:
            return None
        s = self._settings.value(setting, "", type=str)
        if not s:
            return None
        kind, _, value = s.partition(":")
        if kind == "key":
            # Special keys like F9 live as attributes on pynput.keyboard.Key;
            # an unknown saved name (e.g. older serializer, renamed key) is
            # treated as no preference and falls back to the F9 default.
            return getattr(_pynput_keyboard.Key, value, None)
        if kind == "char" and value:
            return _pynput_keyboard.KeyCode.from_char(value)
        return None

    def _save_hotkey_pref(self, key, setting: str = "hotkey/target"):
        if _pynput_keyboard is None or key is None:
            return
        if isinstance(key, _pynput_keyboard.Key):
            self._settings.setValue(setting, f"key:{key.name}")
        else:
            char = getattr(key, "char", None)
            if char:
                self._settings.setValue(setting, f"char:{char}")

    def _load_highlight_prefs(self):
        self.auto_cut_check.blockSignals(True)
        self.hl_fast_check.blockSignals(True)
        self.auto_cut_check.setChecked(
            self._settings.value("highlights/auto_cut", True, type=bool))
        self.hl_fast_check.setChecked(
            self._settings.value("highlights/fast", False, type=bool))
        self.auto_cut_check.blockSignals(False)
        self.hl_fast_check.blockSignals(False)

    def _save_auto_cut_pref(self, enabled: bool):
        self._settings.setValue("highlights/auto_cut", enabled)

    def _save_hl_fast_pref(self, enabled: bool):
        self._settings.setValue("highlights/fast", enabled)

    def _load_auto_record_pref(self):
        # QSettings.value with type=bool handles the str-vs-bool quirk on Windows.
        # League integration (loadout memory, reminders) is on by default;
        # recording matches is opt-in.
        self.postgame_check.blockSignals(True)
        self.postgame_check.setChecked(
            self._settings.value("lcu/postgame_reminder", True, type=bool))
        self.postgame_check.blockSignals(False)
        lcu = self._settings.value("lcu/enabled", True, type=bool)
        auto = self._settings.value("lcu/auto_record", False, type=bool)
        # Triggers _toggle_lcu, which actually starts the stream.
        self.lcu_check.setChecked(lcu or auto)
        self.auto_record_check.setChecked(auto)

    def _save_auto_record_pref(self, enabled: bool):
        self._settings.setValue("lcu/auto_record", enabled)

    def _save_postgame_pref(self, enabled: bool):
        self._settings.setValue("lcu/postgame_reminder", enabled)

    def _toggle_auto_record(self, on: bool):
        self._save_auto_record_pref(on)
        if on and not self.lcu_check.isChecked():
            # Auto-record needs the phase stream.
            self.lcu_check.setChecked(True)
        log.info("auto-record %s", "enabled" if on else "disabled")

    def _toggle_lcu(self, on: bool):
        self._settings.setValue("lcu/enabled", on)
        if on:
            self._enable_lcu()
        else:
            self._disable_lcu()
            if self.auto_record_check.isChecked():
                self.auto_record_check.setChecked(False)

    def _enable_lcu(self):
        if self._lcu_stream is not None:
            return
        try:
            self._lcu_bridge = LCUBridge()
            self._lcu_bridge.phase_changed.connect(self._on_lcu_phase_changed)
            self._lcu_bridge.connected.connect(self._on_lcu_connected)
            self._lcu_bridge.disconnected.connect(self._on_lcu_disconnected)
            bridge = self._lcu_bridge

            self._loadout_bridge = LoadoutBridge()
            self._loadout_bridge.session_changed.connect(
                self.loadout_panel.on_session_changed
            )
            loadout_bridge = self._loadout_bridge

            def emit_session(session):
                loadout_bridge.session_changed.emit(session)

            self._loadout_tracker = loadouts.LoadoutTracker(
                on_session_changed=emit_session
            )
            tracker = self._loadout_tracker

            def on_phase(phase: str):
                bridge.phase_changed.emit(phase)

            def on_connect(lockfile):
                bridge.connected.emit()
                tracker.on_connect(lockfile)
                self.loadout_panel.attach_tracker(tracker)
                # EventStream only fires on transitions; emit the *current*
                # phase too so enabling mid-game still triggers a start.
                try:
                    bridge.phase_changed.emit(league.Client(lockfile).gameflow_phase())
                except Exception:
                    log.exception("initial phase query failed")

            def on_disconnect():
                bridge.disconnected.emit()
                tracker.on_disconnect()

            self._lcu_stream = league.EventStream(
                lockfile_provider=league.discover_lockfile,
                on_phase=on_phase,
                on_event=tracker.on_event,
                on_connect=on_connect,
                on_disconnect=on_disconnect,
            )
            self._lcu_stream.start()
            self.auto_record_status.setText("waiting for League client...")
            log.info("League integration enabled")
        except RuntimeError as e:
            # websocket-client missing.
            QtWidgets.QMessageBox.warning(self, "League integration", str(e))
            self.lcu_check.setChecked(False)

    def _disable_lcu(self):
        if self._lcu_stream is not None:
            self._lcu_stream.stop(timeout=2.0)
            self._lcu_stream = None
        self._lcu_bridge = None
        self._loadout_bridge = None
        self._loadout_tracker = None
        self.loadout_panel.attach_tracker(None)
        self.auto_record_status.setText("(off)")
        log.info("League integration disabled")

    @QtCore.pyqtSlot()
    def _on_lcu_connected(self):
        self._lcu_connected = True
        self.auto_record_status.setText("connected")

    @QtCore.pyqtSlot()
    def _on_lcu_disconnected(self):
        self._lcu_connected = False
        self._lcu_phase = ""
        self.auto_record_status.setText("disconnected; retrying...")

    @QtCore.pyqtSlot(str)
    def _on_lcu_phase_changed(self, phase: str):
        if phase != self._lcu_phase:
            ledger.note("league:phase", phase)
        self._lcu_phase = phase
        self.auto_record_status.setText(f"phase: {phase}")
        log.info("LCU phase: %s (recording=%s, auto_started=%s)",
                 phase, self.thread is not None, self._auto_started)
        if phase == "InProgress":
            if self.thread is None and self.auto_record_check.isChecked():
                self._auto_started = True
                self._start_recording()
        else:
            if self._auto_started and self.recorder is not None:
                self._stop_recording()
        if loadouts.is_postgame_phase(phase):
            self._maybe_show_postgame()

    def _maybe_show_postgame(self):
        """Show the keep/change reminder once per game, if enabled."""
        tracker = self._loadout_tracker
        if tracker is None or not tracker.puuid:
            return
        played = tracker.take_last_played()
        if played is None or not self.postgame_check.isChecked():
            return
        name = tracker.champion_name(played.champion_id)
        dlg = PostgameDialog(tracker.puuid, played, name, parent=self)
        dlg.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(
                f"Postgame — {name}",
                f"Played {loadouts.spell_name(played.spell1_id)} + "
                f"{loadouts.spell_name(played.spell2_id)}. Keep or change?",
                QtWidgets.QSystemTrayIcon.MessageIcon.Information, 8000,
            )

    # ------------------------------------------------------------------
    # Tray icon
    # ------------------------------------------------------------------

    def _build_tray(self):
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            log.info("system tray not available; skipping tray icon")
            return
        self._tray_icon_idle = _make_tray_icon(QtGui.QColor(160, 160, 160))
        self._tray_icon_recording = _make_tray_icon(QtGui.QColor(220, 60, 60))
        self.tray = QtWidgets.QSystemTrayIcon(self._tray_icon_idle, self)
        self.tray.setToolTip("Screen Recorder — idle")
        self.setWindowIcon(self._tray_icon_idle)

        menu = QtWidgets.QMenu(self)
        self._action_show = menu.addAction("Show window")
        self._action_show.triggered.connect(self._toggle_window_visibility)
        menu.addSeparator()
        self._action_record = menu.addAction("● Start recording")
        self._action_record.triggered.connect(self._toggle_record)
        self._action_open_last = menu.addAction("Open last recording")
        self._action_open_last.triggered.connect(self._open_last_output)
        self._action_open_last.setEnabled(False)
        menu.addSeparator()
        self._action_auto_record = menu.addAction("Auto-record League matches")
        self._action_auto_record.setCheckable(True)
        self._action_auto_record.setChecked(self.auto_record_check.isChecked())
        # Drive the GUI checkbox from the tray (and vice-versa) so they stay
        # in sync regardless of which surface the user toggles from.
        self._action_auto_record.toggled.connect(self.auto_record_check.setChecked)
        self.auto_record_check.toggled.connect(self._action_auto_record.setChecked)
        menu.addAction("Open web view").triggered.connect(self._open_web_view)
        menu.addAction("Open terminal view").triggered.connect(self._open_terminal_view)
        menu.addAction("Open data folder").triggered.connect(self._open_log_folder)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self._quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        self._update_tray_state()

    @QtCore.pyqtSlot(QtWidgets.QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason):
        # Left-click toggles window visibility. Right-click is handled by Qt
        # via the context menu we installed.
        if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window_visibility()

    def _toggle_window_visibility(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._show_window()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._really_quit = True
        # close() runs closeEvent's cleanup (API + lockfile, LCU stream, hotkey,
        # recorder) — QApplication.quit() alone would skip it.
        self.close()
        QtWidgets.QApplication.quit()

    def _update_tray_state(self):
        """Refresh tray icon, tooltip, and menu labels for the current state."""
        recording = self.thread is not None
        if self.tray is not None:
            icon = (self._tray_icon_recording if recording
                    else self._tray_icon_idle)
            self.tray.setIcon(icon)
            self.setWindowIcon(icon)
            tip = "Screen Recorder — recording" if recording else "Screen Recorder — idle"
            self.tray.setToolTip(tip)
        if self._action_record is not None:
            self._action_record.setText("■ Stop recording" if recording
                                        else "● Start recording")
        if self._action_show is not None:
            self._action_show.setText(
                "Hide window" if (self.isVisible() and not self.isMinimized())
                else "Show window"
            )
        if self._action_open_last is not None:
            self._action_open_last.setEnabled(
                bool(self._last_output_path
                     and os.path.exists(self._last_output_path))
            )

    # ------------------------------------------------------------------
    # Jump-list dispatcher (called locally for --jump=, and over IPC)
    # ------------------------------------------------------------------

    @QtCore.pyqtSlot(str)
    def apply_jump_action(self, action: str):
        """Run the verb behind a jump-list / IPC command. Safe to call repeatedly."""
        log.info("jump action: %s", action)
        if action == "show":
            self._show_window()
        elif action == "toggle-record":
            self._toggle_record()
        elif action == "open-last":
            self._open_last_output()
        elif action == "toggle-auto-record":
            self.auto_record_check.setChecked(not self.auto_record_check.isChecked())
        elif action == "open-data-dir":
            self._open_log_folder()
        elif action == "open-web":
            self._open_web_view()
        elif action == "open-terminal":
            self._open_terminal_view()
        else:
            log.warning("unknown jump action: %r", action)

    # ------------------------------------------------------------------
    # Local API + browser/terminal views
    # ------------------------------------------------------------------

    def _start_api(self):
        hooks = server.Hooks(
            status=self._api_status,
            actions={
                "record-start": self._api_action_start,
                "record-stop": self._api_action_stop,
                "show": self._api_action_show,
                "quit": self._api_action_quit,
                "terminal": cli.launch_terminal_watch,
            },
        )
        try:
            self._api = server.Server(hooks).start()
        except OSError as e:
            log.exception("local API failed to start")
            self.statusBar().showMessage(f"Local API unavailable: {e}", 8000)
            self._api = None

    def _api_status(self) -> dict:
        """Called on an API thread: read-only, advisory integers/strings only."""
        rec = self.recorder
        recording = self.thread is not None and rec is not None
        return {
            "recording": recording,
            "output": (rec.output if rec is not None else None) or self._last_output_path,
            "frames": rec.frame_count if recording else 0,
            "elapsed": rec.elapsed if recording and rec.start_time else 0.0,
            "avg_fps": rec.avg_fps if recording and rec.start_time else 0.0,
            "auto_started": self._auto_started,
            "league": self.lcu_check.isChecked(),
            "lcu_connected": self._lcu_connected,
            "phase": self._lcu_phase,
            "auto_record": self.auto_record_check.isChecked(),
            "data_dir": logging_setup.log_dir(),
            "log_path": logging_setup.log_path(),
            "ledger_path": ledger.path(),
        }

    def _api_action_start(self):
        if self.thread is not None:
            return "already recording"
        self._api_bridge.start_record.emit()
        return "queued on the GUI thread"

    def _api_action_stop(self):
        if self.thread is None:
            return "not recording"
        self._api_bridge.stop_record.emit()
        return "queued on the GUI thread"

    def _api_action_show(self):
        self._api_bridge.show.emit()
        return "queued on the GUI thread"

    def _api_action_quit(self):
        if self.thread is not None:
            return "refused: a recording is in progress"
        self._api_bridge.quit.emit()
        return "quitting"

    @QtCore.pyqtSlot()
    def _api_start_record(self):
        if self.thread is None:
            self._start_recording()

    def _open_web_view(self):
        if self._api is None:
            self.statusBar().showMessage("Local API is not running.", 5000)
            return
        ledger.begin("exec", "browser",
                     f"{self._api.base_url}/?t=<token>  (one-time link; the token becomes a cookie)").ok()
        webbrowser.open(self._api.url)

    def _open_terminal_view(self):
        try:
            msg = cli.launch_terminal_watch()
        except OSError as e:
            msg = f"could not open a terminal: {e}"
            log.exception("launch_terminal_watch failed")
        self.statusBar().showMessage(msg, 5000)

    def _open_log_folder(self):
        folder = logging_setup.log_dir()
        if os.path.isdir(folder):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))
        else:
            QtWidgets.QMessageBox.information(
                self, "Log folder", f"Log folder does not exist yet:\n{folder}"
            )

    def _toggle_region_inputs(self, on: bool):
        for spin in self.region_inputs.values():
            spin.setEnabled(on)

    def _open_region_picker(self):
        out, why = screens.resolve(self.monitor_picker.selected_spec())
        qscreen = screens.qt_screen_for_output(out, QtGui.QGuiApplication.screens()) if out else None
        if qscreen is None:
            QtWidgets.QMessageBox.warning(
                self, "Region picker",
                f"Cannot find the display to pick on: {why}")
            return
        log.info("region picker on %s (%s)", qscreen.name(), why)
        picker = RegionPicker(qscreen)
        picker.region_selected.connect(self._on_region_picked)
        picker.cancelled.connect(self.show)
        self._region_picker = picker  # keep a reference so it isn't garbage-collected
        self.hide()
        picker.showFullScreen()
        picker.activateWindow()
        picker.raise_()

    @QtCore.pyqtSlot(int, int, int, int)
    def _on_region_picked(self, left: int, top: int, right: int, bottom: int):
        self.region_inputs["left"].setValue(left)
        self.region_inputs["top"].setValue(top)
        self.region_inputs["right"].setValue(right)
        self.region_inputs["bottom"].setValue(bottom)
        self.region_check.setChecked(True)
        self.show()

    def _browse_output(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save recording as", self.output_edit.text(), "MP4 video (*.mp4)")
        if path:
            self.output_edit.setText(path)
            self._save_output_pref()

    def _populate_encoders(self):
        ffmpeg_check = preflight.check_ffmpeg()
        if not ffmpeg_check.ok:
            self._show_remediation("ffmpeg not available", [ffmpeg_check])
            self.record_btn.setEnabled(False)
            self.encoder_combo.addItem("(none)")
            self.encoder_combo.setEnabled(False)
            return
        self.encoder_combo.addItem("auto", "auto")
        for enc in preflight.ENCODER_PRIORITY:
            if preflight.check_encoder_listed(enc).ok:
                self.encoder_combo.addItem(enc, enc)

    def _populate_audio_devices(self):
        devices = preflight.list_audio_devices()
        if not devices:
            placeholder = QtWidgets.QListWidgetItem("(no dshow audio devices found)")
            placeholder.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.audio_list.addItem(placeholder)
            self.audio_list.setEnabled(False)
            self.audio_test_btn.setEnabled(False)
            self.audio_list.setToolTip(
                "No dshow audio devices detected. For system audio, enable 'Stereo Mix' "
                "in Windows Sound settings or install a virtual audio cable."
            )
            return
        for name in devices:
            item = QtWidgets.QListWidgetItem(name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.audio_list.addItem(item)

    def _audio_item_for(self, name: str):
        for i in range(self.audio_list.count()):
            item = self.audio_list.item(i)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == name:
                return item
        return None

    def _set_audio_item_label(self, name: str, suffix: str):
        item = self._audio_item_for(name)
        if item is not None:
            item.setText(f"{name}    {suffix}" if suffix else name)

    def _test_audio_devices(self):
        if self.thread is not None:
            return  # don't run while recording
        targets = []
        for i in range(self.audio_list.count()):
            item = self.audio_list.item(i)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                targets.append(item.data(QtCore.Qt.ItemDataRole.UserRole)
                               or item.text())
        if not targets:
            self.audio_test_label.setText("Tick at least one device first.")
            return

        for name in targets:
            self._set_audio_item_label(name, "(measuring...)")

        self.audio_test_btn.setEnabled(False)
        self.audio_list.setEnabled(False)
        self.record_btn.setEnabled(False)
        self.audio_test_label.setText(
            f"Measuring {len(targets)} device(s) — speak / play audio now..."
        )

        self._meter_thread = QtCore.QThread(self)
        self._meter_worker = AudioMeterWorker(targets, duration_s=3.0)
        self._meter_worker.moveToThread(self._meter_thread)
        self._meter_thread.started.connect(self._meter_worker.run)
        self._meter_worker.result.connect(self._on_audio_test_result)
        self._meter_worker.finished.connect(self._on_audio_test_done)
        self._meter_worker.finished.connect(self._meter_thread.quit)
        self._meter_thread.finished.connect(self._meter_worker.deleteLater)
        self._meter_thread.finished.connect(self._meter_thread.deleteLater)
        self._meter_thread.start()

    @QtCore.pyqtSlot(str, object)
    def _on_audio_test_result(self, name: str, levels):
        if not levels.ok:
            self._set_audio_item_label(name, f"(error: {levels.error})")
            return
        peak = "-inf" if levels.peak_db is None else f"{levels.peak_db:.1f}"
        mean = "-inf" if levels.mean_db is None else f"{levels.mean_db:.1f}"
        self._set_audio_item_label(name, f"peak {peak} dB  /  mean {mean} dB")

    @QtCore.pyqtSlot()
    def _on_audio_test_done(self):
        self.audio_test_btn.setEnabled(True)
        self.audio_list.setEnabled(True)
        self.record_btn.setEnabled(True)
        self.audio_test_label.setText("Done.")
        self._meter_worker = None
        self._meter_thread = None

    def _show_remediation(self, title: str, checks):
        body = []
        for c in checks:
            body.append(f"[{'OK' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
            if not c.ok and c.remediation:
                body.append("    " + c.remediation.replace("\n", "\n    "))
        QtWidgets.QMessageBox.warning(self, title, "\n".join(body))

    def _collect_args(self):
        encoder = self.encoder_combo.currentData() or "auto"
        region = None
        if self.region_check.isChecked():
            region = (
                self.region_inputs["left"].value(),
                self.region_inputs["top"].value(),
                self.region_inputs["right"].value(),
                self.region_inputs["bottom"].value(),
            )
        audio_devices = []
        if self.audio_list.isEnabled():
            for i in range(self.audio_list.count()):
                item = self.audio_list.item(i)
                if item.checkState() == QtCore.Qt.CheckState.Checked:
                    name = item.data(QtCore.Qt.ItemDataRole.UserRole) or item.text()
                    audio_devices.append(name)
        spec = self.monitor_picker.selected_spec()
        out, why = screens.resolve(spec)
        if out is None:
            raise RuntimeError(f"monitor: {why}")
        # Auto with the game running: capture the game window itself (WGC),
        # so nothing else on that display can end up in the recording.
        window = screens.find_game_window() if spec == screens.AUTO else None
        if window:
            why = f"auto: League game window {window[1]!r} hwnd={window[0]} (window capture)"
        log.info("capture %r -> %s", spec, why)
        ledger.note("screen:choice", f"{spec!r} -> {why}")
        return {
            "output": self.output_edit.text().strip() or "recording.mp4",
            "fps": self.fps_spin.value(),
            "monitor": out.idx,
            "window": window,
            "encoder": encoder,
            "region": region,
            "audio_devices": audio_devices,
        }

    def _toggle_record(self):
        if self.thread is None:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        log.info("user requested start")
        try:
            args = self._collect_args()
        except RuntimeError as e:
            self._show_remediation("Monitor problem", [preflight.Check(
                False, "monitor", str(e), "Click Refresh under Monitor and pick a display.")])
            return

        out_check = preflight.check_output(args["output"])
        if not out_check.ok:
            self._show_remediation("Output path problem", [out_check])
            return

        policy = self.on_exist_combo.currentData()
        if policy == "timestamp":
            args["output"] = preflight.timestamped_path(args["output"])
        elif policy == "suffix":
            args["output"] = preflight.next_numbered_path(args["output"])
        elif policy == "prompt" and os.path.exists(args["output"]):
            ans = QtWidgets.QMessageBox.question(
                self,
                "Overwrite existing file?",
                f"{args['output']} already exists.\n\nOverwrite it?",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if ans != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        log.info("resolved output path (policy=%s): %s", policy, args["output"])

        self.status_label.setText(f"Resolving encoder ({args['encoder']})...")
        QtWidgets.QApplication.processEvents()
        encoder = preflight.resolve_encoder(args["encoder"])
        if encoder is None:
            diagnostic = []
            for enc in (preflight.ENCODER_PRIORITY if args["encoder"] == "auto"
                        else [args["encoder"]]):
                listed = preflight.check_encoder_listed(enc)
                diagnostic.append(listed)
                if listed.ok:
                    diagnostic.append(preflight.probe_encoder(enc))
            self._show_remediation("No working encoder", diagnostic)
            self.status_label.setText("Idle")
            return

        for dev in args["audio_devices"]:
            self.status_label.setText(f"Probing audio device: {dev}...")
            QtWidgets.QApplication.processEvents()
            audio_check = preflight.probe_audio_device(dev)
            if not audio_check.ok:
                self._show_remediation("Audio device problem", [audio_check])
                self.status_label.setText("Idle")
                return

        window = args.get("window")
        self.recorder = Recorder(
            output=args["output"],
            fps=args["fps"],
            monitor=args["monitor"],
            region=args["region"],
            encoder=encoder,
            audio_devices=args["audio_devices"],
            window_hwnd=window[0] if window else None,
            window_title=window[1] if window else "",
        )
        # Marker collection runs beside the recorder; started once capture is
        # live (so the clock is valid), stopped in _on_capture_finished.
        self._sidecar = highlights.Sidecar(args["output"])
        rec = self.recorder
        self._tagger = highlights.Tagger(
            self._sidecar,
            clock=lambda: rec.elapsed if rec.start_time else None,
            league_events=self.lcu_check.isChecked(),
        )
        self.thread = QtCore.QThread(self)
        self.worker = CaptureWorker(self.recorder)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.started.connect(self._on_capture_started)
        self.worker.finished.connect(self._on_capture_finished)
        self.worker.finished.connect(self.thread.quit)
        # IMPORTANT: never call self.thread.wait() inside _on_capture_finished —
        # quit() is queued on the GUI thread, so wait() would deadlock with the
        # very event that lets the worker exit. Final cleanup must happen here,
        # after thread.finished fires (the worker's exec() has truly returned).
        self.thread.finished.connect(self._on_thread_finished)

        self._set_inputs_enabled(False)
        self.open_btn.setEnabled(False)
        self.show_in_folder_btn.setEnabled(False)
        self.record_btn.setText("■ Stop")
        self.status_label.setText("Starting...")
        self.thread.start()

    def _stop_recording(self):
        if self.recorder:
            log.info("user requested stop")
            self.status_label.setText("Stopping (finalizing MP4)...")
            self.record_btn.setEnabled(False)
            self.recorder.request_stop()

    @QtCore.pyqtSlot(int, int)
    def _on_capture_started(self, width: int, height: int):
        out = os.path.basename(self.recorder.output) if self.recorder else ""
        self.status_label.setText(
            f"Recording {width}x{height} @ {self.fps_spin.value()}fps -> {out}"
        )
        self.poll_timer.start()
        if self._tagger is not None:
            self._tagger.start()
        self._update_tray_state()

    @QtCore.pyqtSlot(int, float, object)
    def _on_capture_finished(self, frames: int, elapsed: float, error):
        self.poll_timer.stop()
        tagger, self._tagger = self._tagger, None
        if tagger is not None:
            tagger.stop(timeout=2.0)
        avg = (frames / elapsed) if elapsed > 0 else 0.0
        output_path = self.recorder.output if self.recorder else None
        ok, detail = (
            self.recorder.health() if self.recorder
            else (not error, str(error) if error else "ok")
        )
        log.info(
            "capture finished: frames=%d elapsed=%.2fs error=%s health=%s output=%s",
            frames, elapsed, error, detail, output_path,
        )
        if error:
            self.status_label.setText("Error")
            QtWidgets.QMessageBox.warning(self, "Recording finished with error", str(error))
        elif not ok:
            # Valid file, but degraded (e.g. audio dropped). Advisory, not
            # an error: warn without the red "Error" state.
            self.status_label.setText("Finished (audio degraded)")
            QtWidgets.QMessageBox.warning(
                self, "Recording finished — audio degraded", detail
            )
        else:
            size_str = ""
            if output_path and os.path.exists(output_path):
                try:
                    size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    size_str = f", {size_mb:.1f} MB"
                except OSError:
                    pass
                self._last_output_path = output_path
                self.open_btn.setEnabled(True)
                self.show_in_folder_btn.setEnabled(True)
                if self.tray is not None:
                    self.tray.showMessage(
                        "Recording saved",
                        f"{os.path.basename(output_path)}",
                        QtWidgets.QSystemTrayIcon.MessageIcon.Information,
                        4000,
                    )
            self.status_label.setText(
                f"Done -> {os.path.basename(output_path)}{size_str}"
            )
        self.metrics_label.setText(
            f"Frames: {frames}   Elapsed: {elapsed:.1f}s   Avg fps: {avg:.1f}"
        )
        # Re-enable UI immediately. The worker thread will exit shortly via the
        # queued quit() signal; references are nulled in _on_thread_finished.
        self.record_btn.setText("● Record")
        self.record_btn.setEnabled(True)
        self._set_inputs_enabled(True)
        if not error and output_path and os.path.exists(output_path):
            self._after_recording_highlights(output_path)

    @QtCore.pyqtSlot()
    def _on_thread_finished(self):
        self.thread = None
        self.worker = None
        self.recorder = None
        self._auto_started = False
        self._update_tray_state()

    # ---- highlights -------------------------------------------------------

    def _after_recording_highlights(self, output_path: str):
        """Decide what to do with the sidecar once a recording has finished."""
        self._last_highlights_dir = None
        sidecar = highlights.Sidecar.load_if_present(output_path)
        n = len(sidecar.markers) if sidecar else 0
        log.info("recording ended with %d highlight marker(s)", n)
        if not n:
            self.highlights_btn.setEnabled(False)
            self.highlights_btn.setText("Highlights")
            return
        self.highlights_btn.setEnabled(True)
        self.highlights_btn.setText(f"Cut {n} highlight(s)")
        if self.auto_cut_check.isChecked():
            self._cut_highlights(output_path, sidecar)

    def _cut_highlights(self, video: str, sidecar: "highlights.Sidecar"):
        if self._hl_thread is not None:
            self.statusBar().showMessage("A highlight cut is already running.", 3000)
            return
        mode = "copy" if self.hl_fast_check.isChecked() else "reencode"
        n = len(sidecar.markers)
        log.info("cutting %d highlight marker(s) from %s (mode=%s)", n, video, mode)
        self.status_label.setText(f"Cutting {n} highlight(s)...")
        self.highlights_btn.setEnabled(False)
        self._hl_thread = QtCore.QThread(self)
        self._hl_worker = HighlightWorker(video, sidecar, mode)
        self._hl_worker.moveToThread(self._hl_thread)
        self._hl_thread.started.connect(self._hl_worker.run)
        self._hl_worker.finished.connect(self._on_highlights_done)
        self._hl_worker.finished.connect(self._hl_thread.quit)
        self._hl_thread.finished.connect(self._on_hl_thread_finished)
        self._hl_thread.start()

    @QtCore.pyqtSlot(object, object)
    def _on_highlights_done(self, result, error):
        if error:
            self.status_label.setText("Highlights failed (see log)")
            self.statusBar().showMessage(f"Highlights: {error}", 8000)
            self.highlights_btn.setEnabled(True)
            return
        self._last_highlights_dir = result.out_dir
        n = len(result.clips)
        self.status_label.setText(
            f"Done -> {os.path.basename(result.reel or result.out_dir)}"
            f" ({n} highlight clip{'s' if n != 1 else ''}, {result.encoder})"
        )
        self.highlights_btn.setText("Highlights folder")
        self.highlights_btn.setEnabled(True)
        if self.tray is not None:
            self.tray.showMessage(
                "Highlights ready",
                f"{n} clip{'s' if n != 1 else ''} -> {os.path.basename(result.out_dir)}",
                QtWidgets.QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    @QtCore.pyqtSlot()
    def _on_hl_thread_finished(self):
        self._hl_thread = None
        self._hl_worker = None

    def _on_highlights_btn(self):
        if self._last_highlights_dir and os.path.isdir(self._last_highlights_dir):
            try:
                os.startfile(self._last_highlights_dir)  # noqa: S606 (Windows-only app)
            except OSError:
                log.exception("could not open highlights folder")
            return
        video = self._last_output_path
        if not video or not os.path.exists(video):
            return
        sidecar = highlights.Sidecar.load_if_present(video)
        if sidecar and sidecar.markers:
            self._cut_highlights(video, sidecar)

    @QtCore.pyqtSlot()
    def _on_mark_pressed(self):
        tagger = self._tagger
        if tagger is None or self.recorder is None:
            self.statusBar().showMessage("Mark highlight: not recording.", 2000)
            return
        m = tagger.mark()
        if m is None:
            self.statusBar().showMessage("Mark highlight: capture not live yet.", 2000)
            return
        t = int(m.t)
        self.statusBar().showMessage(f"Highlight marked at {t // 60}m{t % 60:02d}s", 3000)

    def _open_last_output(self):
        if self._last_output_path and os.path.exists(self._last_output_path):
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(os.path.abspath(self._last_output_path))
            )

    def _show_last_output_in_folder(self):
        # Windows: `explorer /select,<path>` opens the containing folder
        # with the file pre-selected. The flag/path must be one token (no space).
        if self._last_output_path and os.path.exists(self._last_output_path):
            path = os.path.abspath(self._last_output_path)
            subprocess.Popen(["explorer", f"/select,{path}"])

    def _poll_metrics(self):
        if self.recorder and self.recorder.is_running:
            r = self.recorder
            self.metrics_label.setText(
                f"Frames: {r.frame_count}   Elapsed: {r.elapsed:.1f}s   "
                f"Avg fps: {r.avg_fps:.1f}"
            )

    def _set_inputs_enabled(self, on: bool):
        for w in (self.output_edit, self.monitor_picker, self.fps_spin,
                  self.encoder_combo, self.region_check, self.region_pick_btn):
            w.setEnabled(on)
        for spin in self.region_inputs.values():
            spin.setEnabled(on and self.region_check.isChecked())
        # Only re-enable the audio list if it was actually populated.
        first_item = self.audio_list.item(0) if self.audio_list.count() else None
        audio_populated = first_item is not None and bool(
            first_item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable
        )
        self.audio_list.setEnabled(on and audio_populated)
        if hasattr(self, "audio_test_btn"):
            self.audio_test_btn.setEnabled(on and audio_populated)
        if hasattr(self, "hotkey_btn"):
            self.hotkey_btn.setEnabled(on)
        if hasattr(self, "hotkey_mark_btn"):
            self.hotkey_mark_btn.setEnabled(on)

    def _install_hotkey_listener(self):
        """(Re)build the single pynput listener for both global hotkeys."""
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        bridge = self._hotkey_bridge
        toggle, mark = self._hotkey_target, self._mark_hotkey_target

        def on_press(k):
            if toggle is not None and _pynput_keys_match(k, toggle):
                bridge.toggle_pressed.emit()
            elif mark is not None and _pynput_keys_match(k, mark):
                bridge.mark_pressed.emit()

        self._listener = _pynput_keyboard.Listener(on_press=on_press)
        self._listener.daemon = True
        self._listener.start()

    def _set_hotkey(self, key):
        """Install (or replace) the global record-toggle hotkey."""
        if _pynput_keyboard is None:
            return
        self._hotkey_target = key
        self._install_hotkey_listener()
        label = _format_pynput_key(key)
        self.hotkey_btn.setText(label)
        self.record_btn.setToolTip(f"{label} toggles recording globally.")
        self.statusBar().showMessage(f"Global hotkey: {label}")
        self._save_hotkey_pref(key)

    def _set_mark_hotkey(self, key):
        """Install (or replace) the global 'mark highlight' hotkey."""
        if _pynput_keyboard is None:
            return
        if self._hotkey_target is not None and _pynput_keys_match(key, self._hotkey_target):
            self.statusBar().showMessage(
                "Mark hotkey must differ from the record hotkey.", 4000)
            return
        self._mark_hotkey_target = key
        self._install_hotkey_listener()
        label = _format_pynput_key(key)
        self.hotkey_mark_btn.setText(label)
        self._save_hotkey_pref(key, "hotkey/mark")

    def _capture_hotkey(self):
        if _pynput_keyboard is None:
            return
        # Pause the main listener so the user's chosen keypress doesn't
        # simultaneously trigger a record toggle through the old hotkey.
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        previous = self._hotkey_target
        dlg = HotkeyCaptureDialog(self)
        accepted = dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted
        new_target = dlg.captured_key if (accepted and dlg.captured_key) else previous
        if new_target is not None:
            self._set_hotkey(new_target)
        else:
            self._install_hotkey_listener()

    def _capture_mark_hotkey(self):
        if _pynput_keyboard is None:
            return
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        previous = self._mark_hotkey_target
        dlg = HotkeyCaptureDialog(self)
        accepted = dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted
        new_target = dlg.captured_key if (accepted and dlg.captured_key) else previous
        if new_target is not None:
            self._set_mark_hotkey(new_target)
        else:
            self._install_hotkey_listener()

    def closeEvent(self, event):
        # Hide-to-tray when the user clicks the X. Only the tray's "Quit"
        # action (or QApplication.quit) sets _really_quit, which lets us
        # actually exit. Falls through to a real close if no tray is present
        # so the app can still be shut down on systems without a tray.
        if (not self._really_quit
                and self.tray is not None
                and self.tray.isVisible()):
            event.ignore()
            self.hide()
            self._update_tray_state()
            return
        if self._api is not None:
            self._api.stop()
            self._api = None
        if self._lcu_stream is not None:
            self._lcu_stream.stop(timeout=2.0)
            self._lcu_stream = None
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self.recorder:
            self.recorder.request_stop()
        if self.thread:
            # Same deadlock concern as _on_capture_finished: quit() must be
            # invoked directly so the worker's event loop receives it,
            # otherwise wait() blocks the queued quit from ever dispatching.
            self.thread.quit()
            self.thread.wait(20000)
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._update_tray_state()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_tray_state()


class _SingleInstanceServer(QtCore.QObject):
    """QLocalServer wrapper that hands incoming command lines to a callback.

    Each incoming connection is one short ASCII line: either "show" or
    "jump:<action>". Anything else is logged and discarded. The callback runs
    on the GUI thread because QLocalServer's signals are queued by default
    when the server lives on it.
    """

    def __init__(self, key: str, dispatch):
        super().__init__()
        self._dispatch = dispatch
        self._server = QtNetwork.QLocalServer(self)
        # Remove a stale name from a prior crash. On Windows this is a no-op
        # for the kernel-level named pipe, so it's safe to call unconditionally.
        QtNetwork.QLocalServer.removeServer(key)
        if not self._server.listen(key):
            log.warning("QLocalServer.listen(%s) failed: %s",
                        key, self._server.errorString())
            return
        self._server.newConnection.connect(self._on_new_connection)
        log.info("single-instance server listening on %s", key)

    @QtCore.pyqtSlot()
    def _on_new_connection(self):
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        if not sock.waitForReadyRead(500):
            sock.disconnectFromServer()
            return
        try:
            line = bytes(sock.readAll()).decode("utf-8", errors="replace").strip()
        finally:
            sock.disconnectFromServer()
        if not line:
            return
        if line == "show":
            self._dispatch("show")
            return
        if line == "ping":
            return  # a --minimized launch checking whether we exist
        if line.startswith("jump:"):
            action = line.split(":", 1)[1]
            if action in JUMP_ACTIONS:
                self._dispatch(action)
            else:
                log.warning("ignoring unknown jump action over IPC: %r", action)
            return
        log.warning("ignoring unknown IPC line: %r", line)


def _try_send_to_running_instance(message: str, key: str = SINGLE_INSTANCE_KEY,
                                  timeout_ms: int = 500) -> bool:
    """Send `message` to an already-running instance. True if accepted."""
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(key)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(message.encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True


def _parse_argv(argv):
    p = argparse.ArgumentParser(prog="screecap-gui", add_help=True)
    p.add_argument("--minimized", action="store_true",
                   help="Start hidden in the system tray (used by --enable-startup).")
    p.add_argument("--jump", default=None, choices=sorted(JUMP_ACTIONS),
                   help="Run an action and exit if another instance is running; "
                        "otherwise launch the GUI and dispatch the action.")
    return p.parse_args(argv)


def main():
    logging_setup.configure()
    args = _parse_argv(sys.argv[1:])
    log.info("GUI start: minimized=%s jump=%s", args.minimized, args.jump)

    # Single-instance handoff: if another GUI is already running, forward our
    # intent to it and exit without spinning up a duplicate Qt app.
    # A --minimized launch (run-on-login, or the CLI starting us) must not
    # become a second instance either: it pings and exits if one answers.
    handoff = f"jump:{args.jump}" if args.jump else ("ping" if args.minimized else "show")
    if _try_send_to_running_instance(handoff):
        log.info("handed off to running instance: %s", handoff)
        return

    QtCore.QCoreApplication.setOrganizationName("screecap")
    QtCore.QCoreApplication.setApplicationName("screecap")
    # Closing the main window must NOT quit the app — the tray icon keeps it
    # alive. Tray's Quit action calls QApplication.quit explicitly.
    QtWidgets.QApplication.setQuitOnLastWindowClosed(False)
    app = QtWidgets.QApplication(sys.argv)

    win = MainWindow()

    instance = _SingleInstanceServer(SINGLE_INSTANCE_KEY, win.apply_jump_action)
    win._instance_server = instance  # keep a strong reference

    # Refresh the jump list on every launch — keeps it consistent if we ship
    # new tasks. Cheap and best-effort; failure is logged and ignored.
    windows_integration.refresh_jump_list()

    if not args.minimized:
        win.show()
    if args.jump:
        # First-instance dispatch: we just became the server, so handoff above
        # returned False. Run the action against ourselves now that the GUI
        # exists. Queued so it fires after the event loop is up.
        QtCore.QTimer.singleShot(0, lambda: win.apply_jump_action(args.jump))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
