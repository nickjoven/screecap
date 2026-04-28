"""PyQt6 GUI for the screen recorder."""
import os
import sys

from PyQt6 import QtCore, QtGui, QtWidgets

import preflight
from recorder import Recorder

try:
    from pynput import keyboard as _pynput_keyboard
except ImportError:
    _pynput_keyboard = None


class HotkeyBridge(QtCore.QObject):
    """Routes a key event from pynput's listener thread to the Qt main thread.

    pynput's callbacks run in a background thread; calling Qt slots directly
    from there is unsafe. Emitting a signal from a QObject is thread-safe and
    Qt automatically uses QueuedConnection across threads.
    """
    toggle_pressed = QtCore.pyqtSignal()


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


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen Recorder")
        self.setMinimumWidth(420)

        self.recorder: Recorder = None
        self.thread: QtCore.QThread = None
        self.worker: CaptureWorker = None

        self._build_ui()
        self._populate_monitors()
        self._populate_encoders()
        self._populate_audio_devices()

        self.poll_timer = QtCore.QTimer(self)
        self.poll_timer.setInterval(250)
        self.poll_timer.timeout.connect(self._poll_metrics)

        self._hotkey_target = None
        self._listener = None
        self._hotkey_bridge = None
        if _pynput_keyboard is not None:
            self._hotkey_bridge = HotkeyBridge()
            self._hotkey_bridge.toggle_pressed.connect(self._toggle_record)
            self._set_hotkey(_pynput_keyboard.Key.f9)
        else:
            self.hotkey_btn.setText("(pynput missing)")
            self.hotkey_btn.setEnabled(False)
            self.statusBar().showMessage(
                "Install 'pynput' to enable global hotkeys (pip install pynput)."
            )

    def _build_ui(self):
        central = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(central)

        out_row = QtWidgets.QHBoxLayout()
        self.output_edit = QtWidgets.QLineEdit("recording.mp4")
        browse = QtWidgets.QPushButton("Browse...")
        browse.clicked.connect(self._browse_output)
        out_row.addWidget(self.output_edit)
        out_row.addWidget(browse)
        form.addRow("Output:", out_row)

        self.monitor_combo = QtWidgets.QComboBox()
        form.addRow("Monitor:", self.monitor_combo)

        self.fps_spin = QtWidgets.QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(60)
        form.addRow("FPS:", self.fps_spin)

        self.encoder_combo = QtWidgets.QComboBox()
        form.addRow("Encoder:", self.encoder_combo)

        self.audio_list = QtWidgets.QListWidget()
        self.audio_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        self.audio_list.setMaximumHeight(110)
        self.audio_list.setToolTip(
            "Tick one device for plain audio capture, or multiple to mix them "
            "(e.g. microphone + Stereo Mix for system audio)."
        )
        form.addRow("Audio:", self.audio_list)

        self.region_check = QtWidgets.QCheckBox("Limit to region")
        self.region_check.toggled.connect(self._toggle_region_inputs)
        form.addRow(self.region_check)

        region_grid = QtWidgets.QGridLayout()
        self.region_inputs = {}
        for i, (name, default) in enumerate([("left", 0), ("top", 0),
                                             ("right", 1920), ("bottom", 1080)]):
            spin = QtWidgets.QSpinBox()
            spin.setRange(0, 99999)
            spin.setValue(default)
            spin.setEnabled(False)
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
        hotkey_row.addStretch()
        form.addRow("Hotkey:", hotkey_row)

        self.status_label = QtWidgets.QLabel("Idle")
        form.addRow("Status:", self.status_label)
        self.metrics_label = QtWidgets.QLabel("Frames: 0   Elapsed: 0.0s   Avg fps: —")
        form.addRow("", self.metrics_label)

        self.record_btn = QtWidgets.QPushButton("● Record")
        self.record_btn.setMinimumHeight(36)
        self.record_btn.clicked.connect(self._toggle_record)
        form.addRow(self.record_btn)

        self.setCentralWidget(central)

    def _toggle_region_inputs(self, on: bool):
        for spin in self.region_inputs.values():
            spin.setEnabled(on)

    def _open_region_picker(self):
        monitor_idx = self.monitor_combo.currentData() or 0
        screens = QtGui.QGuiApplication.screens()
        if monitor_idx >= len(screens):
            QtWidgets.QMessageBox.warning(
                self, "Region picker",
                f"Qt sees {len(screens)} screen(s); cannot open picker for monitor "
                f"index {monitor_idx}. Note: Qt and dxcam screen ordering may differ "
                f"if the primary display has changed since this app started."
            )
            return
        picker = RegionPicker(screens[monitor_idx])
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

    def _populate_monitors(self):
        monitors = preflight.list_monitors()
        if not monitors:
            self.monitor_combo.addItem("0  (could not enumerate; assuming primary)", 0)
            return
        for idx, label in monitors:
            self.monitor_combo.addItem(f"{idx}  {label}", idx)

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
            self.audio_list.setToolTip(
                "No dshow audio devices detected. For system audio, enable 'Stereo Mix' "
                "in Windows Sound settings or install a virtual audio cable."
            )
            return
        for name in devices:
            item = QtWidgets.QListWidgetItem(name)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.audio_list.addItem(item)

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
                    audio_devices.append(item.text())
        return {
            "output": self.output_edit.text().strip() or "recording.mp4",
            "fps": self.fps_spin.value(),
            "monitor": self.monitor_combo.currentData() or 0,
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
        args = self._collect_args()

        out_check = preflight.check_output(args["output"])
        if not out_check.ok:
            self._show_remediation("Output path problem", [out_check])
            return

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

        self.recorder = Recorder(
            output=args["output"],
            fps=args["fps"],
            monitor=args["monitor"],
            region=args["region"],
            encoder=encoder,
            audio_devices=args["audio_devices"],
        )
        self.thread = QtCore.QThread(self)
        self.worker = CaptureWorker(self.recorder)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.started.connect(self._on_capture_started)
        self.worker.finished.connect(self._on_capture_finished)
        self.worker.finished.connect(self.thread.quit)

        self._set_inputs_enabled(False)
        self.record_btn.setText("■ Stop")
        self.status_label.setText("Starting...")
        self.thread.start()

    def _stop_recording(self):
        if self.recorder:
            self.status_label.setText("Stopping (finalizing MP4)...")
            self.record_btn.setEnabled(False)
            self.recorder.request_stop()

    @QtCore.pyqtSlot(int, int)
    def _on_capture_started(self, width: int, height: int):
        self.status_label.setText(f"Recording {width}x{height} @ {self.fps_spin.value()}fps")
        self.poll_timer.start()

    @QtCore.pyqtSlot(int, float, object)
    def _on_capture_finished(self, frames: int, elapsed: float, error):
        self.poll_timer.stop()
        avg = (frames / elapsed) if elapsed > 0 else 0.0
        if error:
            self.status_label.setText("Error")
            QtWidgets.QMessageBox.warning(self, "Recording finished with error", str(error))
        else:
            self.status_label.setText(f"Done -> {os.path.basename(self.recorder.output)}")
        self.metrics_label.setText(
            f"Frames: {frames}   Elapsed: {elapsed:.1f}s   Avg fps: {avg:.1f}"
        )
        if self.thread:
            self.thread.wait()
        self.thread = None
        self.worker = None
        self.recorder = None
        self.record_btn.setText("● Record")
        self.record_btn.setEnabled(True)
        self._set_inputs_enabled(True)

    def _poll_metrics(self):
        if self.recorder and self.recorder.is_running:
            r = self.recorder
            self.metrics_label.setText(
                f"Frames: {r.frame_count}   Elapsed: {r.elapsed:.1f}s   "
                f"Avg fps: {r.avg_fps:.1f}"
            )

    def _set_inputs_enabled(self, on: bool):
        for w in (self.output_edit, self.monitor_combo, self.fps_spin,
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
        if hasattr(self, "hotkey_btn"):
            self.hotkey_btn.setEnabled(on)

    def _set_hotkey(self, key):
        """Install (or replace) the global listener for the given pynput key."""
        if _pynput_keyboard is None:
            return
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._hotkey_target = key
        bridge = self._hotkey_bridge
        target = key

        def on_press(k):
            if _pynput_keys_match(k, target):
                bridge.toggle_pressed.emit()

        self._listener = _pynput_keyboard.Listener(on_press=on_press)
        self._listener.daemon = True
        self._listener.start()
        label = _format_pynput_key(key)
        self.hotkey_btn.setText(label)
        self.record_btn.setToolTip(f"{label} toggles recording globally.")
        self.statusBar().showMessage(f"Global hotkey: {label}")

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

    def closeEvent(self, event):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self.recorder:
            self.recorder.request_stop()
            if self.thread:
                self.thread.wait(20000)
        super().closeEvent(event)


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
