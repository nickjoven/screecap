"""A/V sync analysis for a finished recording. Read-only; never touches screecap state.

Three measurements, each answering a different question:

  report    Does the container agree with itself? Per-stream start, span and
            packet counts; the audio span by *sample count* (what a player
            actually plays) against the video span by *wall-clock pts*; pts
            gaps/overlaps inside the audio track; video frame-interval stats.
            Optional --log: the recorder's own "capture loop exited" elapsed.

  onsets    Where are the sharp audio transients in a window? (League pings,
            claps.) Energy-envelope onset detector on decoded mono PCM.

  frames    What does the picture look like around a moment? Dumps every
            frame in [t-before, t+after] as PNG named by its pts in ms, so the
            first frame showing the event can be read off by eye.

Offset at a moment = (first video frame showing the event) - (audio onset).
Drift = offset near the end - offset near the start. Positive offset means
audio leads the picture.

Runs anywhere with ffmpeg/ffprobe on PATH and numpy. Windows paths are
accepted under WSL (C:/... -> /mnt/c/...).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import List, Optional, Tuple

import numpy as np

SR = 48000


def _path(p: str) -> str:
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m and os.name != "nt":
        return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    return p


def _probe_json(path: str, *args: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", *args, path],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def _packets(path: str, stream: str) -> np.ndarray:
    """(pts_time, duration_time) per packet of one stream, in file order."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream,
         "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True,
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and parts[0] not in ("", "N/A"):
            dur = float(parts[1]) if parts[1] not in ("", "N/A") else 0.0
            rows.append((float(parts[0]), dur))
    return np.array(rows, dtype=np.float64).reshape(-1, 2)


def _log_elapsed(log_path: str, video_name: str) -> Optional[float]:
    """Elapsed seconds from the recorder log for this recording, if findable.

    The log names the output in the ffmpeg cmd line; the next "capture loop
    exited" line after it belongs to that recording.
    """
    stem = os.path.basename(video_name)
    seen = False
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if stem in line and "ffmpeg cmd" in line:
                seen = True
            elif seen and "capture loop exited" in line:
                m = re.search(r"elapsed=([\d.]+)s", line)
                return float(m.group(1)) if m else None
    return None


def report(path: str, log_path: Optional[str]) -> dict:
    info = _probe_json(path, "-show_streams", "-show_format")
    res: dict = {"file": path, "streams": []}
    for s in info["streams"]:
        res["streams"].append({k: s.get(k) for k in (
            "index", "codec_type", "codec_name", "start_time", "duration",
            "nb_frames", "sample_rate", "avg_frame_rate", "r_frame_rate")})

    v = _packets(path, "v:0")
    if len(v):
        pts = np.sort(v[:, 0])
        iv = np.diff(pts)
        res["video"] = {
            "frames": int(len(pts)),
            "first_pts": round(float(pts[0]), 4),
            "span_s": round(float(pts[-1] - pts[0] + (iv.mean() if len(iv) else 0)), 4),
            "mean_fps": round(float(1 / iv.mean()), 3) if len(iv) else None,
            "interval_ms_p50_p99_max": [round(float(x) * 1000, 2) for x in (
                np.percentile(iv, 50), np.percentile(iv, 99), iv.max())] if len(iv) else None,
            "gaps_over_100ms": int((iv > 0.1).sum()),
        }

    for idx, s in enumerate(t for t in info["streams"] if t["codec_type"] == "audio"):
        a = _packets(path, f"a:{idx}")
        if not len(a):
            continue
        pts, dur = a[:, 0], a[:, 1]
        expected_next = pts[:-1] + dur[:-1]
        jump = pts[1:] - expected_next          # >0 gap, <0 overlap
        # Negative pts is AAC encoder priming (1024 samples, ~21 ms), which
        # the edit list hides from players. Measure from where playback starts.
        priming = max(0.0, -float(pts[0]))
        sample_span = float(dur.sum()) - priming
        pts_span = float(pts[-1] + dur[-1]) - max(0.0, float(pts[0]))
        res.setdefault("audio", []).append({
            "stream": f"a:{idx}",
            "packets": int(len(pts)),
            "first_pts": round(max(0.0, float(pts[0])), 4),
            "priming_ms": round(priming * 1000, 2),
            "span_by_samples_s": round(sample_span, 4),
            "span_by_pts_s": round(pts_span, 4),
            "pts_minus_samples_ms": round((pts_span - sample_span) * 1000, 2),
            "gaps_over_1ms": int((jump > 0.001).sum()),
            "overlaps_over_1ms": int((jump < -0.001).sum()),
            "largest_gap_ms": round(float(jump.max()) * 1000, 2) if len(jump) else 0.0,
        })

    if "video" in res and res.get("audio"):
        a0 = res["audio"][0]
        vs = res["video"]["span_s"]
        res["start_offset_ms"] = round((a0["first_pts"] - res["video"]["first_pts"]) * 1000, 2)
        res["audio_samples_minus_video_span_ms"] = round((a0["span_by_samples_s"] - vs) * 1000, 2)
        res["implied_clock_ppm"] = round((a0["span_by_samples_s"] - vs) / vs * 1e6, 1) if vs else None

    if log_path:
        el = _log_elapsed(_path(log_path), path)
        res["log_elapsed_s"] = el
        if el and "video" in res:
            res["video_span_minus_log_elapsed_ms"] = round((res["video"]["span_s"] - el) * 1000, 2)
    return res


def _decode_mono(path: str, t0: float, t1: float, stream: str = "a:0") -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i", path,
         "-map", f"0:{stream}", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def onsets(path: str, t0: float, t1: float, rise_db: float = 12.0,
           hop_ms: float = 2.0, refractory_ms: float = 150.0) -> List[Tuple[float, float]]:
    """Return [(time_s, rise_db)] for sharp energy rises in [t0, t1].

    Envelope: RMS over 4 ms windows every hop_ms. An onset is a hop whose
    level exceeds the median of the preceding 200 ms by rise_db. Accurate to
    hop_ms; the -ss seek is sample-exact for audio-only decode.
    """
    x = _decode_mono(path, t0, t1)
    hop = int(SR * hop_ms / 1000)
    win = int(SR * 0.004)
    n = (len(x) - win) // hop
    if n <= 0:
        return []
    frames = np.lib.stride_tricks.as_strided(
        x, shape=(n, win), strides=(x.strides[0] * hop, x.strides[0]))
    db = 20 * np.log10(np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)) + 1e-9)
    ctx = int(200 / hop_ms)
    out: List[Tuple[float, float]] = []
    last = -1e9
    for i in range(ctx, n):
        base = np.median(db[i - ctx:i])
        rise = db[i] - base
        t = t0 + i * hop / SR
        if rise >= rise_db and db[i] > -50 and (t - last) * 1000 >= refractory_ms:
            out.append((round(t, 4), round(float(rise), 1)))
            last = t
    return out


def frames(path: str, t: float, out_dir: str, before: float = 0.3,
           after: float = 0.6, width: int = 960, crop: Optional[str] = None) -> List[str]:
    """Dump frames with pts in [t-before, t+after] as <out_dir>/<pts_ms>.png."""
    os.makedirs(out_dir, exist_ok=True)
    a, b = max(0.0, t - before), t + after
    vf = [f"select='between(t,{a:.4f},{b:.4f})'"]
    if crop:
        vf.append(f"crop={crop}")
    vf.append(f"scale={width}:-2")
    # Seek coarsely 2 s early, keep original timestamps, select exactly.
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-copyts", "-ss", f"{max(0.0, a - 2):.3f}",
         "-i", path, "-to", f"{b + 0.1:.3f}", "-map", "0:v:0", "-vf", ",".join(vf),
         "-vsync", "0", "-frame_pts", "1", "-enc_time_base", "0.001",
         os.path.join(out_dir, "%d.png")],
        check=True,
    )
    return sorted((f for f in os.listdir(out_dir) if f.endswith(".png")),
                  key=lambda f: int(f.split(".")[0]))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("video")
    r.add_argument("--log", help="screecap.log, for the recorder's own elapsed time")
    o = sub.add_parser("onsets")
    o.add_argument("video")
    o.add_argument("t0", type=float)
    o.add_argument("t1", type=float)
    o.add_argument("--rise-db", type=float, default=12.0)
    f = sub.add_parser("frames")
    f.add_argument("video")
    f.add_argument("t", type=float)
    f.add_argument("out_dir")
    f.add_argument("--before", type=float, default=0.3)
    f.add_argument("--after", type=float, default=0.6)
    f.add_argument("--width", type=int, default=960)
    f.add_argument("--crop", help="ffmpeg crop=w:h:x:y applied before scaling")
    args = ap.parse_args(argv)

    video = _path(args.video)
    if args.cmd == "report":
        print(json.dumps(report(video, args.log), indent=2))
    elif args.cmd == "onsets":
        for t, rise in onsets(video, args.t0, args.t1, args.rise_db):
            print(f"{t:10.4f}  +{rise:.1f} dB")
    else:
        for name in frames(video, args.t, args.out_dir, args.before, args.after,
                           args.width, args.crop):
            print(os.path.join(args.out_dir, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
