"""Highlight markers and cutting. UI-agnostic.

Two halves, joined by a sidecar file:

  during   Tagger runs beside the Recorder and appends markers to
           <video>.highlights.json as they happen: a hotkey press, or a
           League Live Client event involving the active player (kills,
           deaths, multikills, objectives). Each marker is in *video
           seconds*, so nothing later needs the game clock.

  after    cut_highlights() turns the markers into pre/post windows, merges
           the ones that overlap, and has ffmpeg write one clip per window
           plus a concatenated reel.

The sidecar is rewritten atomically on every marker, so a crash mid-game
still leaves every marker up to that point on disk.

Why video time is derived from the game clock at poll time, not from the
event's own timestamp alone: the Live Client event list is cumulative and
EventTime is on the in-game clock, which pauses when the game pauses. We
poll about once a second, so a *new* event is at most a second old; mapping
it with `now_video - (gameTime_now - EventTime)` is accurate to the poll
interval and immune to pauses that happened earlier in the match.
"""
import json
import logging
import os
import re
import shutil
import subprocess

import ledger
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".highlights.json"
DEFAULT_PRE_S = 10.0     # seconds kept before a marker
DEFAULT_POST_S = 5.0     # seconds kept after a marker
DEFAULT_MERGE_GAP_S = 2.0  # windows closer than this are merged into one clip
POLL_INTERVAL_S = 1.0

# Live Client event -> (label, weight) when the active player is involved.
# Weight is advisory (sorting/limiting); every matched event becomes a marker.
# `who` names the field that identifies the actor; `team` means a teammate's
# event also counts (objectives), `me` means only the active player's does.
_EVENT_RULES = {
    "ChampionKill": None,          # handled specially (kill / death / assist)
    "Multikill":    ("multikill", 4.0, "KillerName", "me"),
    "FirstBlood":   ("first blood", 3.0, "Recipient", "me"),
    "Ace":          ("ace", 3.0, "Acer", "team"),
    "DragonKill":   ("dragon", 1.5, "KillerName", "team"),
    "BaronKill":    ("baron", 2.0, "KillerName", "team"),
    "HeraldKill":   ("herald", 1.5, "KillerName", "team"),
    "AtakhanKill":  ("atakhan", 2.0, "KillerName", "team"),
    "TurretKilled": ("turret", 1.0, "KillerName", "me"),
    "InhibKilled":  ("inhibitor", 1.5, "KillerName", "team"),
}


# --------------------------------------------------------------------------
# Sidecar
# --------------------------------------------------------------------------

@dataclass
class Marker:
    t: float               # video seconds
    kind: str              # "hotkey" or the Live Client EventName
    label: str             # short human label used in clip names
    weight: float = 1.0
    game_time: Optional[float] = None   # in-game clock seconds, if known


def sidecar_path(video: str) -> str:
    root, _ = os.path.splitext(video)
    return root + SIDECAR_SUFFIX


class Sidecar:
    """Marker list persisted next to the video. Thread-safe `add`."""

    def __init__(self, video: str, path: Optional[str] = None):
        self.video = video
        self.path = path or sidecar_path(video)
        self.created = time.time()
        self.markers: List[Marker] = []
        self._lock = threading.Lock()

    def add(self, marker: Marker) -> Marker:
        with self._lock:
            self.markers.append(marker)
            self._write_locked()
        return marker

    def save(self):
        with self._lock:
            self._write_locked()

    def _write_locked(self):
        payload = {
            "version": 1,
            "video": os.path.basename(self.video),
            "created": self.created,
            "markers": [asdict(m) for m in self.markers],
        }
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".highlights-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=1)
            with ledger.begin("write", self.path, f"{len(self.markers)} highlight marker(s)"):
                os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, video: str, path: Optional[str] = None) -> "Sidecar":
        sc = cls(video, path)
        with open(sc.path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        sc.created = payload.get("created", sc.created)
        sc.markers = [Marker(**m) for m in payload.get("markers", [])]
        return sc

    @classmethod
    def load_if_present(cls, video: str) -> Optional["Sidecar"]:
        p = sidecar_path(video)
        if not os.path.exists(p):
            return None
        try:
            return cls.load(video, p)
        except (OSError, ValueError, TypeError):
            log.exception("unreadable sidecar %s", p)
            return None


# --------------------------------------------------------------------------
# Tagger (runs during the recording)
# --------------------------------------------------------------------------

def _name_variants(entry: dict) -> Set[str]:
    """Every spelling the API might use for one player, lower-cased.

    Events carry `KillerName`-style fields that are "GameName#TAG" on current
    patches and the plain summoner name on older ones; players and the active
    player expose both forms under different keys.
    """
    out: Set[str] = set()
    for k in ("summonerName", "riotId", "riotIdGameName"):
        v = entry.get(k)
        if isinstance(v, str) and v:
            out.add(v.lower())
    gn, tl = entry.get("riotIdGameName"), entry.get("riotIdTagLine")
    if isinstance(gn, str) and isinstance(tl, str) and gn and tl:
        out.add(f"{gn}#{tl}".lower())
    return out


def classify_event(ev: dict, me: Set[str], team: Set[str]) -> Optional[Tuple[str, str, float]]:
    """Return (kind, label, weight) if this event belongs in the highlights."""
    name = ev.get("EventName")
    if not name:
        return None

    def is_me(v) -> bool:
        return isinstance(v, str) and v.lower() in me

    def on_team(v) -> bool:
        return isinstance(v, str) and (v.lower() in me or v.lower() in team)

    if name == "ChampionKill":
        if is_me(ev.get("KillerName")):
            return name, "kill", 3.0
        if is_me(ev.get("VictimName")):
            return name, "death", 2.0
        if any(is_me(a) for a in ev.get("Assisters") or []):
            return name, "assist", 1.5
        return None

    rule = _EVENT_RULES.get(name)
    if not rule:
        return None
    label, weight, who, scope = rule
    actor = ev.get(who)
    if scope == "me" and is_me(actor):
        return name, label, weight
    if scope == "team" and on_team(actor):
        # own objective takes rank above a teammate's
        return name, label, weight + (0.5 if is_me(actor) else 0.0)
    return None


class Tagger:
    """Appends markers to a Sidecar while a recording runs.

    `clock` returns the current video position in seconds (None before the
    first frame is written). League polling is optional; `mark()` works
    regardless and is what the GUI's second hotkey calls.
    """

    def __init__(
        self,
        sidecar: Sidecar,
        clock: Callable[[], Optional[float]],
        league_events: bool = True,
        poll_interval: float = POLL_INTERVAL_S,
        fetch_events: Optional[Callable[[], Optional[list]]] = None,
        fetch_stats: Optional[Callable[[], Optional[dict]]] = None,
        fetch_player: Optional[Callable[[], Optional[dict]]] = None,
        fetch_players: Optional[Callable[[], Optional[list]]] = None,
    ):
        self.sidecar = sidecar
        self.clock = clock
        self.league_events = league_events
        self.poll_interval = poll_interval
        self._fetch_events = fetch_events
        self._fetch_stats = fetch_stats
        self._fetch_player = fetch_player
        self._fetch_players = fetch_players
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_event_id = -1
        self._me: Set[str] = set()
        self._team: Set[str] = set()
        self._identity_tries = 0
        self.in_match = False

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if not self.league_events:
            return
        if self._fetch_events is None:
            try:
                import league
                self._fetch_events = league.liveclient_events
                self._fetch_stats = league.liveclient_game_data
                self._fetch_player = league.liveclient_active_player
                self._fetch_players = league.liveclient_player_list
            except ImportError as e:
                log.warning("league package unavailable (%s); event markers off", e)
                return
        self._thread = threading.Thread(target=self._loop, name="highlight-tagger", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self.sidecar.markers:
            self.sidecar.save()

    # -- manual marker -----------------------------------------------------

    def mark(self, label: str = "mark", weight: float = 2.5) -> Optional[Marker]:
        t = self.clock()
        if t is None:
            return None
        m = Marker(t=round(t, 3), kind="hotkey", label=label, weight=weight)
        self.sidecar.add(m)
        log.info("highlight marker: %s at %.1fs", label, t)
        return m

    # -- League polling ----------------------------------------------------

    def _loop(self):
        while not self._stop.wait(self.poll_interval):
            try:
                self.poll_once()
            except Exception:
                log.exception("tagger poll failed")

    def poll_once(self):
        """One poll of the Live Client API. Public for tests."""
        stats = self._fetch_stats()
        if not isinstance(stats, dict) or "gameTime" not in stats:
            if self.in_match:
                log.info("Live Client gone; match over")
            self.in_match = False
            self._me.clear(); self._team.clear()
            self._last_event_id = -1
            self._identity_tries = 0
            return
        game_now = float(stats["gameTime"])
        if not self.in_match:
            self.in_match = True
            log.info("Live Client up: gameTime=%.0fs", game_now)
        if not self._me:
            self._resolve_identity()
            if not self._me:
                return
        now_video = self.clock()
        if now_video is None:
            return
        events = self._fetch_events() or []
        for ev in events:
            eid = ev.get("EventID", -1)
            if eid <= self._last_event_id:
                continue
            self._last_event_id = max(self._last_event_id, eid)
            hit = classify_event(ev, self._me, self._team)
            if not hit:
                continue
            kind, label, weight = hit
            try:
                et = float(ev.get("EventTime", game_now))
            except (TypeError, ValueError):
                et = game_now
            t = now_video - (game_now - et)
            if t < 0:
                # happened before this recording started (joined a running game)
                continue
            self.sidecar.add(Marker(t=round(t, 3), kind=kind, label=label,
                                    weight=weight, game_time=et))
            log.info("highlight marker: %s at %.1fs (game %.0fs)", label, t, et)

    def _resolve_identity(self):
        self._identity_tries += 1
        me = self._fetch_player() or {}
        self._me = _name_variants(me)
        if not self._me:
            if self._identity_tries in (1, 10):
                log.warning("active player not resolvable yet (try %d)", self._identity_tries)
            return
        players = self._fetch_players() or []
        my_team = None
        for p in players:
            if _name_variants(p) & self._me:
                my_team = p.get("team")
                break
        if my_team is not None:
            for p in players:
                if p.get("team") == my_team:
                    self._team |= _name_variants(p)
            self._team -= self._me
        log.info("active player: %s team=%s (%d teammates)",
                 sorted(self._me)[0], my_team, len(self._team))


# --------------------------------------------------------------------------
# Windows + cutting (after the recording)
# --------------------------------------------------------------------------

@dataclass
class Window:
    start: float
    end: float
    markers: List[Marker] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def weight(self) -> float:
        return sum(m.weight for m in self.markers)

    def label(self) -> str:
        seen, out = set(), []
        for m in self.markers:
            if m.label not in seen:
                seen.add(m.label)
                out.append(m.label)
        return "+".join(out)[:40] or "clip"


def build_windows(
    markers: Iterable[Marker],
    pre: float = DEFAULT_PRE_S,
    post: float = DEFAULT_POST_S,
    merge_gap: float = DEFAULT_MERGE_GAP_S,
    duration: Optional[float] = None,
) -> List[Window]:
    ms = sorted(markers, key=lambda m: m.t)
    out: List[Window] = []
    for m in ms:
        s, e = max(0.0, m.t - pre), m.t + post
        if duration is not None:
            e = min(e, duration)
            if s >= duration:
                continue
        if out and s <= out[-1].end + merge_gap:
            out[-1].end = max(out[-1].end, e)
            out[-1].markers.append(m)
        else:
            out.append(Window(s, e, [m]))
    return [w for w in out if w.duration > 0.5]


def _fmt_t(t: float) -> str:
    t = int(t)
    return f"{t // 60}m{t % 60:02d}s"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9+_-]+", "_", s).strip("_") or "clip"


def probe_duration(video: str) -> Optional[float]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = ledger.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except (subprocess.SubprocessError, ValueError):
        return None


def default_out_dir(video: str) -> str:
    root, _ = os.path.splitext(os.path.abspath(video))
    return root + "_highlights"


@dataclass
class CutResult:
    windows: List[Window]
    clips: List[str]
    reel: Optional[str]
    out_dir: str
    encoder: str


def cut_highlights(
    video: str,
    sidecar: Optional[Sidecar] = None,
    out_dir: Optional[str] = None,
    pre: float = DEFAULT_PRE_S,
    post: float = DEFAULT_POST_S,
    merge_gap: float = DEFAULT_MERGE_GAP_S,
    mode: str = "reencode",          # "reencode" (frame-accurate) | "copy" (keyframe-snapped, instant)
    encoder: str = "auto",
    reel: bool = True,
    progress: Optional[Callable[[int, int, Window], None]] = None,
) -> CutResult:
    """Cut every marker window out of `video`. Raises RuntimeError on failure."""
    if sidecar is None:
        sidecar = Sidecar.load(video)
    if not sidecar.markers:
        raise RuntimeError(f"no highlight markers in {sidecar.path}")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH")

    duration = probe_duration(video)
    windows = build_windows(sidecar.markers, pre, post, merge_gap, duration)
    if not windows:
        raise RuntimeError("markers fall outside the video")

    out_dir = out_dir or default_out_dir(video)
    os.makedirs(out_dir, exist_ok=True)

    enc_name = "copy"
    enc_args: List[str] = ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    if mode == "reencode":
        # Imported lazily: preflight pulls in dxcam, which only exists on Windows.
        import preflight
        resolved = preflight.resolve_encoder(encoder)
        if resolved is None:
            raise RuntimeError(f"no working video encoder for '{encoder}'")
        enc_name = resolved
        enc_args = preflight.encoder_args(resolved) + ["-c:a", "aac", "-b:a", "192k"]

    clips: List[str] = []
    for i, w in enumerate(windows, 1):
        name = f"{i:02d}_{_fmt_t(w.start)}_{_safe(w.label())}.mp4"
        path = os.path.join(out_dir, name)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{w.start:.3f}", "-i", video, "-t", f"{w.duration:.3f}",
               *enc_args, "-movflags", "+faststart", path]
        log.info("cut %d/%d: %s", i, len(windows), subprocess.list2cmdline(cmd))
        r = ledger.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(path):
            raise RuntimeError(f"ffmpeg failed on clip {i}: {r.stderr.strip()[-400:]}")
        clips.append(path)
        if progress:
            progress(i, len(windows), w)

    reel_path = None
    if reel and clips:
        reel_path = os.path.join(out_dir, "highlights.mp4")
        list_path = os.path.join(out_dir, ".concat.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for c in clips:
                f.write("file '" + os.path.basename(c).replace("'", r"'\''") + "'\n")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", list_path,
               "-c", "copy", "-movflags", "+faststart", reel_path]
        r = ledger.run(cmd, capture_output=True, text=True, cwd=out_dir)
        try:
            os.unlink(list_path)
        except OSError:
            pass
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed on reel: {r.stderr.strip()[-400:]}")

    return CutResult(windows=windows, clips=clips, reel=reel_path,
                     out_dir=out_dir, encoder=enc_name)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Cut highlight clips from a screecap recording using its "
                    "<video>.highlights.json sidecar.")
    p.add_argument("video", help="the recorded MP4")
    p.add_argument("--sidecar", help="marker file (default: next to the video)")
    p.add_argument("--out-dir", help="clip folder (default: <video>_highlights/)")
    p.add_argument("--pre", type=float, default=DEFAULT_PRE_S, help="seconds before each marker")
    p.add_argument("--post", type=float, default=DEFAULT_POST_S, help="seconds after each marker")
    p.add_argument("--merge-gap", type=float, default=DEFAULT_MERGE_GAP_S)
    p.add_argument("--copy", action="store_true",
                   help="stream-copy instead of re-encoding: instant, but cuts snap "
                        "to keyframes (2 s apart in screecap recordings)")
    p.add_argument("--encoder", default="auto", help="video encoder for re-encode mode")
    p.add_argument("--no-reel", action="store_true", help="skip the concatenated highlights.mp4")
    p.add_argument("--list", action="store_true", help="print markers and windows, cut nothing")
    p.add_argument("--mark", nargs="+", metavar=("SECONDS", "LABEL"),
                   help="append a manual marker to the sidecar (and exit)")
    a = p.parse_args(argv)

    if a.mark:
        try:
            t = float(a.mark[0])
        except ValueError:
            p.error("--mark SECONDS must be a number")
        label = " ".join(a.mark[1:]) or "mark"
        sc = Sidecar.load_if_present(a.video) or Sidecar(a.video, a.sidecar)
        sc.add(Marker(t=t, kind="manual", label=label, weight=2.5))
        print(f"added marker '{label}' at {_fmt_t(t)} -> {sc.path}")
        return 0

    try:
        sc = Sidecar.load(a.video, a.sidecar)
    except FileNotFoundError:
        print(f"no sidecar at {a.sidecar or sidecar_path(a.video)}\n"
              f"  remedy: record with screecap (markers are written during the "
              f"recording), or add one: python highlights.py {a.video} --mark 83 'nice play'")
        return 1
    if not sc.markers:
        print(f"sidecar has no markers: {sc.path}")
        return 1

    duration = probe_duration(a.video)
    windows = build_windows(sc.markers, a.pre, a.post, a.merge_gap, duration)
    print(f"{len(sc.markers)} marker(s), {len(windows)} window(s)"
          + (f", video {_fmt_t(duration)}" if duration else ""))
    for w in windows:
        print(f"  {_fmt_t(w.start):>7} - {_fmt_t(w.end):<7}  {w.label()}")
    if a.list:
        return 0

    try:
        res = cut_highlights(a.video, sc, a.out_dir, a.pre, a.post, a.merge_gap,
                             mode="copy" if a.copy else "reencode",
                             encoder=a.encoder, reel=not a.no_reel,
                             progress=lambda i, n, w: print(f"  [{i}/{n}] {w.label()}"))
    except RuntimeError as e:
        print(f"error: {e}")
        return 2
    print(f"wrote {len(res.clips)} clip(s) via {res.encoder} -> {res.out_dir}")
    if res.reel:
        print(f"reel: {res.reel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
