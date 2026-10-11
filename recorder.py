"""Recording engine: DXGI capture -> ffmpeg encode. UI-agnostic."""
import logging
import os
import re
import subprocess
import threading
import time
from typing import List, Optional, Tuple

import capture
import ledger

from preflight import encoder_args

log = logging.getLogger(__name__)
ffmpeg_log = logging.getLogger("screecap.ffmpeg")

# dshow is a live source; ffmpeg's default real-time buffer is ~3 MB. Under
# load (or when the video clock stalls and audio backs up) it overflows
# continuously and ffmpeg drops *every* audio frame -> a silent track. This
# is a different knob from -thread_queue_size and must be set explicitly.
AUDIO_RTBUFSIZE = "256M"

# Seconds of zero frame-count progress before the watchdog aborts. With
# video_mode=True dxcam delivers frames every loop iteration at ~target fps,
# so frame_count freezing for this long means the capture loop is wedged
# (e.g. get_latest_frame() blocked), never a slow-but-alive recording.
STALL_TIMEOUT = 30.0

# GOP length in seconds (-g = fps * this). See the comment at the -g flag.
KEYFRAME_INTERVAL_S = 2.0

# The dshow "real-time buffer too full ... ! frame dropped!" line is our
# xrun signal: every match is irrecoverably lost audio. ffmpeg's logger
# collapses runs of it into "Last message repeated N times", so count both.
_DROP_RE = re.compile(r"frame dropped", re.IGNORECASE)
_REPEAT_RE = re.compile(r"Last message repeated (\d+) times")


class Recorder:
    """Owns the frame source (a display via dxcam, or one window via WGC) and ffmpeg.

    Lifecycle:
        r = Recorder(...)
        r.start()        # spin up camera + ffmpeg, grab first frame
        r.run()          # blocks: capture -> pipe loop until request_stop()
        r.shutdown()     # close pipe, wait for ffmpeg to finalize MP4

    For GUI use: drive run() inside a worker thread; call request_stop()
    from the UI thread to break the loop.
    """

    def __init__(
        self,
        output: str,
        fps: int = 60,
        monitor: int = 0,
        region: Optional[Tuple[int, int, int, int]] = None,
        encoder: str = "libx264",
        duration: Optional[float] = None,
        audio_devices: Optional[List[str]] = None,
        stall_timeout: float = STALL_TIMEOUT,
        window_hwnd: Optional[int] = None,
        window_title: str = "",
    ):
        self.output = output
        self.fps = fps
        self.monitor = monitor
        # When set, capture this window (Windows Graphics Capture) instead of
        # the monitor; `monitor`/`region` are then ignored.
        self.window_hwnd = window_hwnd
        self.window_title = window_title
        self.region = tuple(region) if region else None
        self.encoder = encoder
        self.duration = duration
        self.audio_devices = list(audio_devices) if audio_devices else []
        self.stall_timeout = stall_timeout

        self._camera = None   # the frame source (capture.DisplaySource / WindowSource)
        self._proc = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Newest frame published by _pump_frames(), consumed by run().
        self._frame_thread: Optional[threading.Thread] = None
        self._frame_cv = threading.Condition()
        self._frame = None
        self._frame_seq = 0
        self._frame_exc: Optional[BaseException] = None

        self.frame_count = 0
        self.audio_drops = 0
        self.start_time: Optional[float] = None
        self.width: Optional[int] = None
        self.height: Optional[int] = None
        self.error: Optional[str] = None
        self.is_running = False

    def start(self):
        """Initialize capture + encoder. Raises on failure."""
        log.info(
            "Recorder.start: output=%s fps=%d monitor=%d region=%s encoder=%s "
            "duration=%s audio=%s",
            self.output, self.fps, self.monitor, self.region, self.encoder,
            self.duration, self.audio_devices,
        )
        if self.window_hwnd:
            source = capture.WindowSource(self.window_hwnd, self.fps, self.window_title)
        else:
            source = capture.DisplaySource(self.monitor, self.fps, self.region)
        cam_entry = ledger.begin("read", source.describe(), source.detail())
        try:
            self.width, self.height = source.start()
        except Exception as e:
            cam_entry.err(str(e))
            raise
        self._camera = source
        first = source.first_frame()
        cam_entry.ok(f"{self.width}x{self.height}")
        log.info("%s source started: %dx%d", source.kind, self.width, self.height)

        n_audio = len(self.audio_devices)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]
        # Audio inputs come FIRST. ffmpeg opens inputs in command-line order
        # and rebases every input so its own first packet lands at t=0, so
        # whatever real-time gap separates the first video stamp from the
        # first audio sample becomes a constant A/V offset in the file. With
        # the pipe first, that gap was the dshow device start-up (~1 s, audio
        # early). With dshow first, the graph is already running when ffmpeg
        # probes the pipe, and the first frame (written before launch) is
        # stamped at that same instant: the gap is milliseconds.
        # dshow audio is a live source with no EOF, so -shortest is required:
        # without it, ffmpeg waits forever after we close the video pipe.
        for dev in self.audio_devices:
            cmd += [
                "-f", "dshow", "-thread_queue_size", "1024",
                "-rtbufsize", AUDIO_RTBUFSIZE,
                "-i", f"audio={dev}",
            ]
        video_in = n_audio   # the pipe's index among ffmpeg's inputs
        cmd += [
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}",
            # `-framerate`, NOT `-r`. Both set the rawvideo time base, but
            # `-r` as an *input* option also tells ffmpeg to discard the
            # input timestamps and regenerate constant-rate ones, which
            # silently cancelled -use_wallclock_as_timestamps below. That
            # was the A/V drift: Python delivers ~55-57 fps, ffmpeg counted
            # each frame as 1/60 s, the video track ran ~6% fast and audio
            # fell a minute behind over a 20-minute game.
            "-framerate", str(self.fps),
            "-thread_queue_size", "1024",
            # Stamp each piped frame with its real arrival time. The video
            # timeline is then wall time however many frames we managed to
            # deliver: a slow capture gives a sparse (VFR) track, never a
            # fast-running one. It also keeps video on the same real
            # timeline as the live dshow audio, so the muxer never holds
            # audio back waiting for video to catch up (which is what
            # overflowed the dshow buffer and dropped audio). Resolution is
            # one 1/fps tick; two frames read within one tick collapse to
            # one under -fps_mode vfr.
            "-use_wallclock_as_timestamps", "1",
            "-i", "-",
        ]
        cmd += encoder_args(self.encoder)
        # Keyframe every KEYFRAME_INTERVAL_S so highlight cuts (highlights.py)
        # that stream-copy land within that distance of the marker. Encoder
        # defaults are ~4 s (250 frames) and -g is honoured by every encoder
        # in ENCODER_PRIORITY. Costs a little bitrate on static screens.
        cmd += ["-g", str(max(1, int(self.fps * KEYFRAME_INTERVAL_S)))]
        if n_audio == 1:
            cmd += [
                "-map", f"{video_in}:v", "-map", "0:a",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
            ]
        elif n_audio > 1:
            # Inputs 0..N-1 are audio (the pipe is input N). amix mixes them
            # to one stereo track. duration=shortest stops mixing when any
            # input ends; -shortest then closes the file when video ends.
            inputs = "".join(f"[{i}:a]" for i in range(n_audio))
            filter_str = f"{inputs}amix=inputs={n_audio}:duration=shortest[aout]"
            cmd += [
                "-filter_complex", filter_str,
                "-map", f"{video_in}:v", "-map", "[aout]",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
            ]
        # Wall-clock video PTS make the stream variable-rate by nature;
        # tell the muxer so instead of re-padding it back to CFR (which
        # would reintroduce the media-clock-vs-wall-clock divergence the
        # input flag exists to remove). Tradeoff: output is VFR.
        cmd += ["-fps_mode", "vfr"]
        cmd += [self.output]
        log.info("ffmpeg cmd: %s", subprocess.list2cmdline(cmd))
        # Announced before launch; answered in shutdown() with the exit code.
        self._ledger_ffmpeg = ledger.begin("exec", "ffmpeg", subprocess.list2cmdline(cmd))
        self._ledger_output = ledger.begin("write", self.output, "MP4 output (ffmpeg writes it)")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=ledger.NO_WINDOW,  # no console flash under pythonw
        )
        self._stderr_thread = threading.Thread(
            target=self._pump_ffmpeg_stderr,
            args=(self._proc.stderr,),
            name="ffmpeg-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        self._proc.stdin.write(first.tobytes())

        self.frame_count = 1
        self.start_time = time.time()
        self.is_running = True

        self._frame_thread = threading.Thread(
            target=self._pump_frames, name="capture-frames", daemon=True
        )
        self._frame_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name="capture-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def _pump_frames(self):
        """Fetch frames from the source and publish the newest one.

        Runs on its own thread so a source call that never returns cannot
        wedge run() (dxcam's output-recovery loop can spin for hours after
        a display mode switch). Latest-wins: run() takes whatever is newest
        when it looks, so a slow pipe skips frames exactly as before. Ends
        on None (source gone), on an exception (handed to run()), or on
        stop.
        """
        try:
            while not self._stop.is_set():
                frame = self._camera.get_latest_frame()
                with self._frame_cv:
                    self._frame = frame
                    self._frame_seq += 1
                    self._frame_cv.notify()
                if frame is None:
                    return
        except Exception as e:
            with self._frame_cv:
                self._frame_exc = e
                self._frame_seq += 1
                self._frame_cv.notify()

    def run(self):
        """Capture loop. Blocks until request_stop() or duration is reached.

        Frames come from _pump_frames(); this loop only waits on a condition
        variable with a short timeout, so request_stop() always gets through.
        """
        seen = 0
        try:
            while not self._stop.is_set():
                with self._frame_cv:
                    while self._frame_seq == seen and not self._stop.is_set():
                        self._frame_cv.wait(0.5)
                    if self._stop.is_set():
                        break
                    seen = self._frame_seq
                    frame, exc = self._frame, self._frame_exc
                if exc is not None:
                    raise exc
                if frame is None:
                    # dxcam returns None only after its capture thread died;
                    # a WindowSource returns None once the window closed.
                    # Either way: end cleanly and let shutdown() finalize.
                    if getattr(self._camera, "kind", "") == "window":
                        log.info("captured window closed; ending recording")
                    else:
                        self.error = "dxcam capture stopped unexpectedly (see log for details)"
                        log.warning(self.error)
                    break
                self._proc.stdin.write(frame.tobytes())
                self.frame_count += 1
                if self.duration and self.elapsed >= self.duration:
                    log.info("duration reached after %d frames", self.frame_count)
                    break
        except BrokenPipeError:
            self.error = "ffmpeg closed the pipe unexpectedly"
            log.exception("broken pipe in capture loop after %d frames", self.frame_count)
        except Exception as e:
            self.error = f"capture loop error: {e}"
            log.exception("capture loop error after %d frames", self.frame_count)
        finally:
            self.is_running = False
            log.info("capture loop exited: frames=%d elapsed=%.2fs",
                     self.frame_count, self.elapsed)

    def _watchdog(self):
        """Abort a wedged capture loop.

        commit 0806b28 handles dxcam returning None (capture thread died).
        It cannot handle get_latest_frame() *blocking* forever. Here we
        watch frame_count from a side thread and, on a stall, stop the
        *camera* (never ffmpeg) and then end the loop ourselves with
        request_stop(): run() waits on a condition variable, not on the
        source, so this gets through even when the source call never
        returns (2026-10-10: dxcam sat in its output-recovery loop for 12 h,
        run() never exited, shutdown() never ran, ffmpeg was orphaned and
        the MP4 got no moov atom). The caller's shutdown() then finalizes
        the MP4 normally -- the "shutdown must not kill ffmpeg" invariant
        holds.

        Note: this catches a hard hang, not the asleep-screen case where
        video_mode keeps emitting duplicated frames (frame_count still
        advances). That needs an auto-record duration cap in the front-end.
        """
        last_count = self.frame_count
        last_progress = time.time()
        while not self._stop.wait(1.0):
            now = time.time()
            if self.frame_count != last_count:
                last_count = self.frame_count
                last_progress = now
                continue
            if now - last_progress >= self.stall_timeout:
                self.error = (
                    f"capture stalled: no new frames for "
                    f"{self.stall_timeout:.0f}s; aborting"
                )
                log.warning(self.error)
                try:
                    if self._camera is not None:
                        self._camera.stop()
                except Exception:
                    log.exception("watchdog: source stop raised")
                self.request_stop()
                return

    def request_stop(self):
        """Signal the run loop to exit. Non-blocking, signal-safe."""
        self._stop.set()

    def shutdown(self, finalize_timeout: float = 15.0):
        """Tear down camera + ffmpeg. Blocks while ffmpeg writes the moov atom."""
        log.info("shutdown begin")
        self.request_stop()
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                log.exception("source stop raised")
        if self._proc is not None:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
            except Exception:
                log.exception("closing ffmpeg stdin raised")
            try:
                rc = self._proc.wait(timeout=finalize_timeout)
                log.info("ffmpeg exited rc=%d", rc)
                self._answer_ledger(rc)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg did not finalize in %.0fs; killing", finalize_timeout)
                self._proc.kill()
                self._proc.wait()
                self.error = (self.error or "") + \
                    f" (ffmpeg did not finalize in {finalize_timeout:.0f}s; output may be unplayable)"
                self._answer_ledger(None)
        if self._watchdog_thread is not None:
            # request_stop() above already set _stop; the watchdog wakes
            # from its <=1 s wait and exits on its own.
            self._watchdog_thread.join(timeout=2.0)
        if self._frame_thread is not None:
            # May be blocked inside the source forever; it is a daemon.
            self._frame_thread.join(timeout=2.0)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2.0)
        log.info("shutdown done")

    def _answer_ledger(self, rc):
        """Close the exec/write ledger entries opened in start()."""
        ffm = getattr(self, "_ledger_ffmpeg", None)
        out = getattr(self, "_ledger_output", None)
        if ffm is None:
            return
        try:
            size = os.path.getsize(self.output)
        except OSError:
            size = 0
        summary = (f"{self.frame_count} frames, {self.elapsed:.1f}s, "
                   f"{self.avg_fps:.1f} fps avg, {size / 1e6:.1f} MB")
        if rc is None:
            ffm.err(f"killed after finalize timeout; {summary}")
            if out is not None:
                out.err("may be unplayable (no moov atom)")
        elif rc == 0:
            ffm.ok(f"rc=0, {summary}")
            if out is not None:
                out.ok(f"{size / 1e6:.1f} MB")
        else:
            ffm.err(f"rc={rc}, {summary}")
            if out is not None:
                out.err(f"ffmpeg rc={rc}")

    def _pump_ffmpeg_stderr(self, stream):
        """Forward ffmpeg's stderr to the logger and tally audio xruns.

        `audio_drops` is incremented from this thread and read from the
        finishing thread in health(); like frame_count it's an advisory
        int (atomic write, display/verdict only), so no lock is needed.
        """
        prev_was_drop = False
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                ffmpeg_log.warning(line)
                if _DROP_RE.search(line):
                    self.audio_drops += 1
                    prev_was_drop = True
                    continue
                m = _REPEAT_RE.search(line)
                if m and prev_was_drop:
                    self.audio_drops += int(m.group(1))
                    continue
                prev_was_drop = False
        except Exception:
            log.exception("ffmpeg stderr pump crashed")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @property
    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time

    @property
    def avg_fps(self) -> float:
        e = self.elapsed
        return self.frame_count / e if e > 0 else 0.0

    def health(self) -> Tuple[bool, str]:
        """Post-record quality verdict, distinct from `error`.

        `error` means the recording hard-failed. `health()` catches the
        failure class an rc=0 ffmpeg hides: a technically valid file whose
        audio is gappy or silent because dshow buffers overflowed. Call
        after shutdown(). Returns (ok, human-readable detail).
        """
        if self.error:
            return False, self.error
        if self.audio_devices and self.audio_drops:
            return False, (
                f"audio degraded: ffmpeg dropped {self.audio_drops} dshow "
                f"buffer(s) during capture; the audio track may be gappy "
                f"or silent"
            )
        return True, "ok"
