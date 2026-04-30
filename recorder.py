"""Recording engine: DXGI capture -> ffmpeg encode. UI-agnostic."""
import subprocess
import threading
import time
from typing import List, Optional, Tuple

import dxcam

from preflight import encoder_args


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
    ):
        self.output = output
        self.fps = fps
        self.monitor = monitor
        self.region = tuple(region) if region else None
        self.encoder = encoder
        self.duration = duration
        self.audio_devices = list(audio_devices) if audio_devices else []

        self._camera = None
        self._proc = None
        self._stop = threading.Event()

        self.frame_count = 0
        self.start_time: Optional[float] = None
        self.width: Optional[int] = None
        self.height: Optional[int] = None
        self.error: Optional[str] = None
        self.is_running = False

    def start(self):
        """Initialize capture + encoder. Raises on failure."""
        cam = dxcam.create(output_idx=self.monitor, output_color="BGR")
        if cam is None:
            raise RuntimeError(f"No DXGI output at monitor index {self.monitor}")
        self._camera = cam
        self._camera.start(target_fps=self.fps, video_mode=True, region=self.region)

        first = self._camera.get_latest_frame()
        self.height, self.width = first.shape[:2]

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}", "-r", str(self.fps),
            "-thread_queue_size", "1024",
            "-i", "-",
        ]
        # dshow audio is a live source with no EOF, so -shortest is required:
        # without it, ffmpeg waits forever after we close the video pipe.
        for dev in self.audio_devices:
            cmd += [
                "-f", "dshow", "-thread_queue_size", "1024",
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
        cmd += [self.output]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self._proc.stdin.write(first.tobytes())

        self.frame_count = 1
        self.start_time = time.time()
        self.is_running = True

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
                    break
                self._proc.stdin.write(frame.tobytes())
                self.frame_count += 1
                if self.duration and self.elapsed >= self.duration:
                    break
        except BrokenPipeError:
            self.error = "ffmpeg closed the pipe unexpectedly"
        except Exception as e:
            self.error = f"capture loop error: {e}"
        finally:
            self.is_running = False

    def request_stop(self):
        """Signal the run loop to exit. Non-blocking, signal-safe."""
        self._stop.set()

    def shutdown(self, finalize_timeout: float = 15.0):
        """Tear down camera + ffmpeg. Blocks while ffmpeg writes the moov atom."""
        self.request_stop()
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                pass
        if self._proc is not None:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=finalize_timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
                self.error = (self.error or "") + \
                    f" (ffmpeg did not finalize in {finalize_timeout:.0f}s; output may be unplayable)"

    @property
    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time

    @property
    def avg_fps(self) -> float:
        e = self.elapsed
        return self.frame_count / e if e > 0 else 0.0
