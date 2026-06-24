"""Recording engine: DXGI capture -> ffmpeg encode. UI-agnostic."""
import logging
import re
import subprocess
import threading
import time
from typing import List, Optional, Tuple

import dxcam

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

# The dshow "real-time buffer too full ... ! frame dropped!" line is our
# xrun signal: every match is irrecoverably lost audio. ffmpeg's logger
# collapses runs of it into "Last message repeated N times", so count both.
_DROP_RE = re.compile(r"frame dropped", re.IGNORECASE)
_REPEAT_RE = re.compile(r"Last message repeated (\d+) times")


class Recorder:
    """Owns the dxcam camera and ffmpeg subprocess.

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
    ):
        self.output = output
        self.fps = fps
        self.monitor = monitor
        self.region = tuple(region) if region else None
        self.encoder = encoder
        self.duration = duration
        self.audio_devices = list(audio_devices) if audio_devices else []
        self.stall_timeout = stall_timeout

        self._camera = None
        self._proc = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

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
        cam = dxcam.create(output_idx=self.monitor, output_color="BGR")
        if cam is None:
            raise RuntimeError(f"No DXGI output at monitor index {self.monitor}")
        self._camera = cam
        self._camera.start(target_fps=self.fps, video_mode=True, region=self.region)

        first = self._camera.get_latest_frame()
        self.height, self.width = first.shape[:2]
        log.info("dxcam started: %dx%d", self.width, self.height)

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}", "-r", str(self.fps),
            "-thread_queue_size", "1024",
            # Stamp each piped frame with its real arrival time. Without this
            # the pipe is treated as constant-rate `-r fps`, so a slow/stalled
            # capture makes video's media clock lag wall time; ffmpeg then
            # throttles the pipe read and the live dshow audio buffer
            # overflows -> dropped audio. Wall-clock PTS keeps video on the
            # same real timeline as audio, so audio is never starved: the
            # continuous audio is effectively the master, video conforms
            # (sparse/stretched). `-r` above stays only as the nominal rate.
            "-use_wallclock_as_timestamps", "1",
            "-i", "-",
        ]
        # dshow audio is a live source with no EOF, so -shortest is required:
        # without it, ffmpeg waits forever after we close the video pipe.
        for dev in self.audio_devices:
            cmd += [
                "-f", "dshow", "-thread_queue_size", "1024",
                "-rtbufsize", AUDIO_RTBUFSIZE,
                "-i", f"audio={dev}",
            ]
        cmd += encoder_args(self.encoder)
        n_audio = len(self.audio_devices)
        if n_audio == 1:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        elif n_audio > 1:
            # Inputs 1..N are audio (input 0 is the video pipe). amix mixes them
            # to one stereo track. duration=shortest stops mixing when any input
            # ends; -shortest then closes the file when video ends.
            inputs = "".join(f"[{i + 1}:a]" for i in range(n_audio))
            filter_str = f"{inputs}amix=inputs={n_audio}:duration=shortest[aout]"
            cmd += [
                "-filter_complex", filter_str,
                "-map", "0:v", "-map", "[aout]",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
            ]
        # Wall-clock video PTS make the stream variable-rate by nature;
        # tell the muxer so instead of re-padding it back to CFR (which
        # would reintroduce the media-clock-vs-wall-clock divergence the
        # input flag exists to remove). Tradeoff: output is VFR.
        cmd += ["-fps_mode", "vfr"]
        cmd += [self.output]
        log.info("ffmpeg cmd: %s", subprocess.list2cmdline(cmd))
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE
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

        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name="capture-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def run(self):
        """Capture loop. Blocks until request_stop() or duration is reached."""
        try:
            while not self._stop.is_set():
                frame = self._camera.get_latest_frame()
                if frame is None:
                    # dxcam clears its frame buffer and returns None only after
                    # its capture thread has exited (typically via an unhandled
                    # backend exception logged just above). Surface a clean
                    # error and let shutdown() finalize the MP4.
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
        It cannot handle get_latest_frame() *blocking* forever: run() never
        returns from the call, so neither the None check nor _stop is ever
        reached. Here we watch frame_count from a side thread and, on a
        stall, stop the *camera* (never ffmpeg). dxcam then returns None on
        the next/blocked get_latest_frame(), run()'s existing None branch
        breaks the loop, and the caller's shutdown() finalizes the MP4
        normally -- the "shutdown must not kill ffmpeg" invariant holds.

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
                    log.exception("watchdog: dxcam stop raised")
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
                log.exception("dxcam stop raised")
        if self._proc is not None:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
            except Exception:
                log.exception("closing ffmpeg stdin raised")
            try:
                rc = self._proc.wait(timeout=finalize_timeout)
                log.info("ffmpeg exited rc=%d", rc)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg did not finalize in %.0fs; killing", finalize_timeout)
                self._proc.kill()
                self._proc.wait()
                self.error = (self.error or "") + \
                    f" (ffmpeg did not finalize in {finalize_timeout:.0f}s; output may be unplayable)"
        if self._watchdog_thread is not None:
            # request_stop() above already set _stop; the watchdog wakes
            # from its <=1 s wait and exits on its own.
            self._watchdog_thread.join(timeout=2.0)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2.0)
        log.info("shutdown done")

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
