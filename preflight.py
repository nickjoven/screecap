"""Preflight checks + encoder selection. Each failed Check carries a remediation."""
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

import dxcam


ENCODER_PRIORITY = ["h264_nvenc", "h264_qsv", "libx264", "mpeg4"]
ENCODER_CHOICES = ["auto"] + ENCODER_PRIORITY


@dataclass
class Check:
    ok: bool
    name: str
    detail: str
    remediation: Optional[str] = None


def check_ffmpeg() -> Check:
    path = shutil.which("ffmpeg")
    if not path:
        return Check(
            False, "ffmpeg", "not found on PATH",
            "Install with:  winget install --id Gyan.FFmpeg\n"
            "  or download the 'full' build from https://www.gyan.dev/ffmpeg/builds/\n"
            "  and add its bin/ folder to PATH (the 'essentials' build lacks libx264).",
        )
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=5)
        version = out.stdout.splitlines()[0] if out.stdout else "unknown version"
    except Exception as e:
        return Check(False, "ffmpeg", f"found at {path} but failed to run: {e}",
                     "Reinstall ffmpeg.")
    return Check(True, "ffmpeg", f"{path} ({version})")


def check_encoder_listed(encoder: str) -> Check:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        return Check(False, f"encoder:{encoder}", f"could not query ffmpeg: {e}",
                     "Reinstall ffmpeg.")
    listed = any(line.split()[1:2] == [encoder] for line in out.stdout.splitlines() if line.strip())
    if not listed:
        if encoder == "libx264":
            rem = ("Your ffmpeg build doesn't include libx264. Replace it with the 'full' "
                   "build from gyan.dev (the 'essentials' build is the usual culprit).")
        elif encoder == "h264_nvenc":
            rem = ("ffmpeg lacks NVENC support. Use a 'full' build, and confirm an NVIDIA "
                   "GPU with recent drivers is present.")
        elif encoder == "h264_qsv":
            rem = ("ffmpeg lacks Quick Sync. Use a 'full' build, and confirm an Intel "
                   "iGPU/dGPU with recent drivers is present.")
        else:
            rem = "Pick a different encoder."
        return Check(False, f"encoder:{encoder}", "not listed by ffmpeg", rem)
    return Check(True, f"encoder:{encoder}", "listed by ffmpeg")


def probe_encoder(encoder: str) -> Check:
    """Encode one black frame to /dev/null. Confirms the encoder actually works."""
    w, h, fps = 320, 240, 30
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{w}x{h}", "-r", str(fps),
        "-i", "-", "-c:v", encoder, "-frames:v", "1",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, input=b"\x00" * (w * h * 3),
                              capture_output=True, timeout=10)
    except Exception as e:
        return Check(False, f"probe:{encoder}", f"probe failed to launch: {e}",
                     "Check that ffmpeg runs at all (`ffmpeg -version`).")
    if proc.returncode != 0:
        err = (proc.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
        if encoder == "h264_nvenc":
            rem = "Update NVIDIA drivers, or fall back to libx264."
        elif encoder == "h264_qsv":
            rem = "Update Intel GPU drivers, or fall back to libx264."
        else:
            rem = "Try libx264 (CPU) or mpeg4 (compatibility)."
        return Check(False, f"probe:{encoder}", err or "non-zero exit", rem)
    return Check(True, f"probe:{encoder}", "1-frame probe encode succeeded")


def check_monitor(idx: int) -> Check:
    try:
        cam = dxcam.create(output_idx=idx)
    except Exception as e:
        return Check(False, f"monitor:{idx}", f"dxcam.create raised: {e}",
                     "Run `python -c \"import dxcam; print(dxcam.device_info())\"` to list devices.")
    if cam is None:
        try:
            info = dxcam.output_info()
        except Exception:
            info = "(could not enumerate outputs)"
        return Check(False, f"monitor:{idx}", "no DXGI output at this index",
                     f"Available outputs:\n{info}\nPick a valid index.")
    del cam
    return Check(True, f"monitor:{idx}", "DXGI output found")


def check_output(path: str) -> Check:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(parent):
        return Check(False, "output", f"directory does not exist: {parent}",
                     f"Create it:  mkdir \"{parent}\"")
    if not os.access(parent, os.W_OK):
        return Check(False, "output", f"directory not writable: {parent}",
                     "Choose a different output path, or fix the directory's permissions.")
    if os.path.exists(path):
        return Check(True, "output", f"will OVERWRITE existing file: {path}")
    return Check(True, "output", f"writable: {path}")


def print_checks(checks):
    if not checks:
        return
    width = max(len(c.name) for c in checks)
    for c in checks:
        sym = "OK  " if c.ok else "FAIL"
        print(f"  [{sym}] {c.name:<{width}}  {c.detail}")
        if not c.ok and c.remediation:
            for line in c.remediation.splitlines():
                print(f"         -> {line}")


def resolve_encoder(requested: str) -> Optional[str]:
    """Pick a working encoder. For 'auto', walk the priority list and probe each."""
    candidates = ENCODER_PRIORITY if requested == "auto" else [requested]
    for enc in candidates:
        if check_encoder_listed(enc).ok and probe_encoder(enc).ok:
            return enc
    return None


def encoder_args(encoder: str) -> list:
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-tune", "zerolatency"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20",
                "-pix_fmt", "yuv420p"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "20",
                "-pix_fmt", "nv12"]
    if encoder == "mpeg4":
        return ["-c:v", "mpeg4", "-q:v", "5"]
    raise ValueError(f"unknown encoder: {encoder}")


def list_monitors() -> list:
    """Return a list of (index, label) for every DXGI output."""
    try:
        info = dxcam.output_info()
    except Exception:
        return []
    monitors = []
    idx = 0
    for line in info.splitlines():
        line = line.strip()
        if not line or "Output[" not in line:
            continue
        monitors.append((idx, line))
        idx += 1
    return monitors


def list_audio_devices() -> list:
    """Parse `ffmpeg -f dshow -list_devices` output for audio device names.

    ffmpeg writes the device list to stderr (regardless of -loglevel), and its
    return code is non-zero because '-i dummy' fails after the listing.

    Two output formats, both handled:

    - Legacy (ffmpeg <= ~4.x): devices grouped under "DirectShow audio
      devices" / "DirectShow video devices" header lines, no per-device tag.
    - Modern (ffmpeg ~7.x+): the header lines are gone; each device line is
      tagged inline, e.g. `[in#0 @ ..] "Mic (USB)" (audio)`. Trust the tag.

    Parsing only the header form silently returns zero devices on modern
    ffmpeg even when devices exist.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "dshow", "-list_devices", "true",
             "-i", "dummy"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []
    devices = []
    in_audio = False
    for raw in proc.stderr.splitlines():
        line = raw.strip()
        if "Alternative name" in line:
            continue
        # Legacy header lines (absent on modern ffmpeg).
        if "DirectShow audio devices" in line:
            in_audio = True
            continue
        if "DirectShow video devices" in line:
            in_audio = False
            continue
        if '"' not in line:
            continue
        # Modern: inline tag at end of the device line wins. Legacy: no
        # tag, fall back to which header section we're in.
        if line.endswith("(audio)"):
            is_audio = True
        elif line.endswith("(video)"):
            is_audio = False
        else:
            is_audio = in_audio
        if not is_audio:
            continue
        start = line.index('"') + 1
        end = line.rindex('"')
        if end > start:
            devices.append(line[start:end])
    return devices


def probe_audio_device(name: str) -> Check:
    """Capture 0.1s from the dshow audio device to confirm it opens cleanly."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "dshow", "-i", f"audio={name}",
        "-t", "0.1", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception as e:
        return Check(False, f"audio:{name}", f"probe failed to launch: {e}",
                     "Check that ffmpeg runs (`ffmpeg -version`).")
    if proc.returncode != 0:
        err = (proc.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
        if "Stereo Mix" in name or "What U Hear" in name:
            rem = ("Stereo Mix is usually disabled by default. Right-click the speaker "
                   "icon -> Sounds -> Recording tab -> right-click empty area -> "
                   "Show Disabled Devices -> enable Stereo Mix.")
        else:
            rem = ("Open Sound Settings, confirm the device is enabled and not held "
                   "exclusively by another application, then retry.")
        return Check(False, f"audio:{name}", err or "non-zero exit", rem)
    return Check(True, f"audio:{name}", "audio probe succeeded")
