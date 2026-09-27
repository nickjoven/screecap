"""Per-champion loadout memory: SQLite store + LCU capture/apply.

The store keeps one row per (puuid, champion_id) — last spells used, last
rune page contents (snapshotted, not just an id), last seen time, and a play
counter. The tracker watches champ-select session events and writes a row on
session end. Apply pushes the row back to the client by writing to a single
dedicated `[screecap] auto` rune page so the user's hand-curated pages are
never overwritten.

A row can be *locked*: its spell pair is pinned. A locked pair is not
overwritten by whatever was played last, and the tracker re-applies it
automatically (once per champ select) whenever that champion is picked.

UI-agnostic. The Qt-specific bridging lives in gui.py.
"""
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import logging_setup
from league.rest import Client

log = logging.getLogger(__name__)

# Single dedicated rune page name. apply_loadout writes the snapshot here so
# we never touch a page the user actually maintains.
SCRATCH_PAGE_NAME = "[screecap] auto"

# Hardcoded — Riot's summoner spell IDs have been stable for years and the LCU
# endpoint for these is patch-version-gated. Falling back to "Spell <id>" is
# fine for any unmapped id.
SUMMONER_SPELL_NAMES = {
    1: "Cleanse", 3: "Exhaust", 4: "Flash", 6: "Ghost", 7: "Heal",
    11: "Smite", 12: "Teleport", 13: "Clarity", 14: "Ignite",
    21: "Barrier", 32: "Snowball",
}


def spell_name(spell_id: int) -> str:
    return SUMMONER_SPELL_NAMES.get(spell_id, f"Spell {spell_id}")


def current_spells(cell: dict) -> tuple[int, int]:
    """(spell1Id, spell2Id) currently selected on a champ-select cell; 0 = unset."""
    return (int(cell.get("spell1Id") or 0), int(cell.get("spell2Id") or 0))


def pick_locked_in(session: dict) -> bool:
    """True once the local player's pick action is completed (champion locked in).

    `championId` on the cell is set as soon as a champion is *selected*, before
    the lock-in click; the completed pick action is the actual lock-in signal.
    """
    local_cell_id = session.get("localPlayerCellId")
    for group in session.get("actions") or []:
        for action in group or []:
            if (action.get("type") == "pick"
                    and action.get("actorCellId") == local_cell_id
                    and action.get("completed")):
                return True
    return False


@dataclass
class Loadout:
    puuid: str
    champion_id: int
    spell1_id: int
    spell2_id: int
    rune_page: Optional[dict]   # {name, primaryStyleId, subStyleId, selectedPerkIds}
    last_seen: datetime
    count: int
    locked: bool = False   # added in schema v2; default keeps v1 rows parsing


# ----------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------

def db_path() -> str:
    return os.path.join(logging_setup.log_dir(), "loadouts.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(logging_setup.log_dir(), exist_ok=True)
    conn = sqlite3.connect(db_path())
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loadout (
            puuid TEXT NOT NULL,
            champion_id INTEGER NOT NULL,
            spell1_id INTEGER NOT NULL,
            spell2_id INTEGER NOT NULL,
            rune_page_json TEXT,
            last_seen TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (puuid, champion_id)
        )
    """)
    _migrate(conn)
    return conn


# Schema versions (additive only so far):
#   1: puuid, champion_id, spell1_id, spell2_id, rune_page_json, last_seen, count
#   2: + locked INTEGER NOT NULL DEFAULT 0
SCHEMA_VERSION = 2


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(loadout)")}
    if "locked" not in cols:
        conn.execute(
            "ALTER TABLE loadout ADD COLUMN locked INTEGER NOT NULL DEFAULT 0"
        )
        log.info("loadouts.db migrated to schema v2 (locked column)")


def record_loadout(puuid: str, champion_id: int, spell1_id: int,
                   spell2_id: int, rune_page: Optional[dict]) -> None:
    """Insert or update the row for (puuid, champion_id).

    rune_page is preserved across writes if the new value is None — that way
    a session that ended without us being able to GET /currentpage doesn't
    wipe a previously-snapshotted page.

    If the row is locked, the stored spell pair is kept as-is: last_seen,
    count and runes still update, but the pin wins over what was played.
    """
    if not puuid or champion_id <= 0:
        return
    rune_json = json.dumps(rune_page) if rune_page else None
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute("""
            INSERT INTO loadout (puuid, champion_id, spell1_id, spell2_id,
                                 rune_page_json, last_seen, count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(puuid, champion_id) DO UPDATE SET
                spell1_id = CASE WHEN loadout.locked THEN loadout.spell1_id
                                 ELSE excluded.spell1_id END,
                spell2_id = CASE WHEN loadout.locked THEN loadout.spell2_id
                                 ELSE excluded.spell2_id END,
                rune_page_json = COALESCE(excluded.rune_page_json,
                                          loadout.rune_page_json),
                last_seen = excluded.last_seen,
                count = loadout.count + 1
        """, (puuid, champion_id, spell1_id, spell2_id, rune_json, now))
    log.info("loadout recorded: champ=%d spells=(%s,%s) runes=%s",
             champion_id, spell_name(spell1_id), spell_name(spell2_id),
             rune_page.get("name") if rune_page else None)


def get_loadout(puuid: str, champion_id: int) -> Optional[Loadout]:
    if not puuid or champion_id <= 0:
        return None
    with _connect() as conn:
        row = conn.execute("""
            SELECT spell1_id, spell2_id, rune_page_json, last_seen, count, locked
            FROM loadout WHERE puuid = ? AND champion_id = ?
        """, (puuid, champion_id)).fetchone()
    if row is None:
        return None
    spell1_id, spell2_id, rune_json, last_seen, count, locked = row
    return Loadout(
        puuid=puuid,
        champion_id=champion_id,
        spell1_id=spell1_id,
        spell2_id=spell2_id,
        rune_page=json.loads(rune_json) if rune_json else None,
        last_seen=datetime.fromisoformat(last_seen),
        count=count,
        locked=bool(locked),
    )


def set_locked(puuid: str, champion_id: int, locked: bool,
               spell1_id: int = 0, spell2_id: int = 0) -> Optional[Loadout]:
    """Pin (or unpin) the spell pair for (puuid, champion_id).

    When locking, a non-zero spell pair replaces the stored one — pass the
    spells currently selected in champ select so "lock" means "lock *these*".
    With zeros, the stored pair is pinned unchanged. Locking a champion with
    no history inserts a row with count=0 (never played, but pinned).
    Unlocking never touches the spells. Returns the resulting row.
    """
    if not puuid or champion_id <= 0:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        if locked and spell1_id > 0 and spell2_id > 0:
            conn.execute("""
                INSERT INTO loadout (puuid, champion_id, spell1_id, spell2_id,
                                     rune_page_json, last_seen, count, locked)
                VALUES (?, ?, ?, ?, NULL, ?, 0, 1)
                ON CONFLICT(puuid, champion_id) DO UPDATE SET
                    spell1_id = excluded.spell1_id,
                    spell2_id = excluded.spell2_id,
                    locked = 1
            """, (puuid, champion_id, spell1_id, spell2_id, now))
        else:
            conn.execute("""
                UPDATE loadout SET locked = ?
                WHERE puuid = ? AND champion_id = ?
            """, (1 if locked else 0, puuid, champion_id))
    result = get_loadout(puuid, champion_id)
    if result is not None:
        log.info("loadout %s: champ=%d spells=(%s,%s)",
                 "locked" if result.locked else "unlocked", champion_id,
                 spell_name(result.spell1_id), spell_name(result.spell2_id))
    return result


# ----------------------------------------------------------------------------
# Capture: tracker that observes champ-select session events
# ----------------------------------------------------------------------------

def _find_my_cell(session: dict) -> Optional[dict]:
    local_cell_id = session.get("localPlayerCellId")
    for cell in session.get("myTeam") or []:
        if cell.get("cellId") == local_cell_id:
            return cell
    return None


def _intended_champion_id(cell: dict) -> int:
    """Locked-in id if present, else the hover/intent. Either may be 0."""
    return cell.get("championId") or cell.get("championPickIntent") or 0


class LoadoutTracker:
    """Watches LCU events and persists a loadout when champ select ends.

    Wire it into an EventStream by passing tracker.on_event as the on_event
    callback and tracker.on_connect as on_connect. Optionally pass a
    session_changed callback to drive UI updates while champ select is live.

    All callbacks fire on the EventStream's worker thread — the GUI is
    responsible for marshalling to its own thread.
    """

    def __init__(
        self,
        on_session_changed: Optional[Callable[[Optional[dict]], None]] = None,
    ):
        self._on_session_changed = on_session_changed
        self._client: Optional[Client] = None
        self._puuid: Optional[str] = None
        self._latest_session: Optional[dict] = None
        self._champion_names: Optional[dict] = None
        # Champions already considered for auto-apply in the current champ
        # select. One attempt per champion per session: after that the user's
        # manual changes win, and we don't hit the DB on every websocket frame.
        self._auto_apply_seen: set[int] = set()

    @property
    def puuid(self) -> Optional[str]:
        return self._puuid

    @property
    def client(self) -> Optional[Client]:
        return self._client

    def on_connect(self, lockfile) -> None:
        self._client = Client(lockfile)
        try:
            s = self._client.current_summoner() or {}
            self._puuid = s.get("puuid") or None
            log.info("loadout tracker bound: puuid=%s", self._puuid)
        except Exception:
            log.exception("fetch current summoner failed")
        # Champion names rarely change between launcher restarts; fetch lazily
        # via champion_name() so a slow asset endpoint doesn't block on_connect.

    def champion_name(self, champion_id: int) -> str:
        """Look up a display name; falls back to 'Champion #<id>' if unknown."""
        if champion_id <= 0:
            return "(none)"
        if self._champion_names is None and self._client is not None:
            try:
                summary = self._client.get(
                    "/lol-game-data/assets/v1/champion-summary.json"
                ) or []
                self._champion_names = {
                    int(c.get("id")): c.get("name") or ""
                    for c in summary if c.get("id") and c.get("id") != -1
                }
            except Exception:
                log.exception("fetch champion-summary failed")
                self._champion_names = {}
        name = (self._champion_names or {}).get(champion_id)
        return name or f"Champion #{champion_id}"

    def on_disconnect(self) -> None:
        self._client = None
        self._latest_session = None
        self._auto_apply_seen.clear()
        if self._on_session_changed:
            try:
                self._on_session_changed(None)
            except Exception:
                log.exception("session_changed(None) callback raised")

    def on_event(self, uri: str, payload: dict) -> None:
        if uri != "/lol-champ-select/v1/session":
            return
        event_type = payload.get("eventType")
        data = payload.get("data")
        if event_type == "Delete":
            self._on_session_end()
            self._latest_session = None
            self._auto_apply_seen.clear()
            if self._on_session_changed:
                try:
                    self._on_session_changed(None)
                except Exception:
                    log.exception("session_changed(None) callback raised")
            return
        # Create or Update — `data` is the full session dict.
        if isinstance(data, dict):
            self._latest_session = data
            self._maybe_auto_apply(data)
            if self._on_session_changed:
                try:
                    self._on_session_changed(data)
                except Exception:
                    log.exception("session_changed callback raised")

    def forget_auto_apply(self, champion_id: int) -> None:
        """Let the next session frame re-evaluate auto-apply for this champion.

        Call after the lock state changes mid-select so a fresh lock takes
        effect without waiting for the next champ select.
        """
        self._auto_apply_seen.discard(champion_id)

    def _maybe_auto_apply(self, session: dict) -> None:
        """Push the locked spell pair when a locked champion is picked.

        Runs on the EventStream thread; the PATCH is one short local HTTP call,
        the same class of work _on_session_end already does here.
        """
        if self._client is None or not self._puuid:
            return
        cell = _find_my_cell(session)
        if cell is None:
            return
        champ_id = _intended_champion_id(cell)
        if champ_id <= 0 or champ_id in self._auto_apply_seen:
            return
        self._auto_apply_seen.add(champ_id)
        loadout = get_loadout(self._puuid, champ_id)
        if loadout is None or not loadout.locked:
            return
        if current_spells(cell) == (loadout.spell1_id, loadout.spell2_id):
            log.info("locked spells already selected: champ=%d", champ_id)
            return
        try:
            apply_spells(self._client, loadout)
            log.info("auto-applied locked spells: champ=%d", champ_id)
        except Exception:
            log.exception("auto-apply locked spells failed (champ=%d)", champ_id)

    def _on_session_end(self) -> None:
        if self._client is None or not self._puuid:
            return
        if self._latest_session is None:
            return
        cell = _find_my_cell(self._latest_session)
        if cell is None:
            return
        champ_id = _intended_champion_id(cell)
        spell1 = cell.get("spell1Id") or 0
        spell2 = cell.get("spell2Id") or 0
        if champ_id <= 0 or spell1 <= 0 or spell2 <= 0:
            log.info("skip loadout record: champ=%d spells=(%d,%d) — incomplete",
                     champ_id, spell1, spell2)
            return
        snapshot = self._snapshot_current_page()
        record_loadout(self._puuid, champ_id, spell1, spell2, snapshot)

    def _snapshot_current_page(self) -> Optional[dict]:
        if self._client is None:
            return None
        try:
            page = self._client.get("/lol-perks/v1/currentpage")
        except Exception:
            log.exception("fetch currentpage failed")
            return None
        if not isinstance(page, dict):
            return None
        # Only the writable fields — anything else is read-only and would be
        # rejected (or silently ignored) on a future PUT.
        return {
            "name": page.get("name"),
            "primaryStyleId": page.get("primaryStyleId"),
            "subStyleId": page.get("subStyleId"),
            "selectedPerkIds": list(page.get("selectedPerkIds") or []),
        }


# ----------------------------------------------------------------------------
# Apply: push a stored loadout back to the LCU
# ----------------------------------------------------------------------------

def apply_loadout(client: Client, loadout: Loadout) -> None:
    """Push the loadout to the running client.

    Spells go via PATCH /lol-champ-select/v1/session/my-selection — only valid
    while champ select is active. Runes are written to the SCRATCH_PAGE_NAME
    page (created on first apply), then made current. The two are independent;
    one failure is logged but does not abort the other.
    """
    spells_err: Optional[Exception] = None
    runes_err: Optional[Exception] = None

    try:
        apply_spells(client, loadout)
    except Exception as e:
        spells_err = e
        log.exception("apply spells failed (champ=%d)", loadout.champion_id)

    if loadout.rune_page is not None:
        try:
            page_id = _ensure_scratch_page(client)
            body = {
                "name": SCRATCH_PAGE_NAME,
                "primaryStyleId": loadout.rune_page.get("primaryStyleId"),
                "subStyleId": loadout.rune_page.get("subStyleId"),
                "selectedPerkIds": list(loadout.rune_page.get("selectedPerkIds") or []),
            }
            client.request("PUT", f"/lol-perks/v1/pages/{page_id}", body)
            client.request("PUT", "/lol-perks/v1/currentpage", page_id)
            log.info("applied runes to page %s (id=%d)", SCRATCH_PAGE_NAME, page_id)
        except Exception as e:
            runes_err = e
            log.exception("apply runes failed (champ=%d)", loadout.champion_id)

    # Surface the most actionable error. If both failed, the spells one usually
    # reflects the underlying state (not in champ select / wrong phase).
    if spells_err is not None:
        raise spells_err
    if runes_err is not None:
        raise runes_err


def apply_spells(client: Client, loadout: Loadout) -> None:
    """Set summoner spells only. Valid only while champ select is active."""
    client.request("PATCH", "/lol-champ-select/v1/session/my-selection",
                   {"spell1Id": loadout.spell1_id,
                    "spell2Id": loadout.spell2_id})
    log.info("applied spells: champ=%d (%s, %s)",
             loadout.champion_id,
             spell_name(loadout.spell1_id), spell_name(loadout.spell2_id))


def _ensure_scratch_page(client: Client) -> int:
    """Return the id of the [screecap] auto page, creating a placeholder if absent."""
    pages = client.get("/lol-perks/v1/pages") or []
    for p in pages:
        if p.get("name") == SCRATCH_PAGE_NAME:
            return p["id"]
    placeholder: dict[str, Any] = {
        "name": SCRATCH_PAGE_NAME,
        "primaryStyleId": 8000,   # Precision — arbitrary non-zero default
        "subStyleId": 8100,       # Domination
        "selectedPerkIds": [],
    }
    created = client.request("POST", "/lol-perks/v1/pages", placeholder)
    if not isinstance(created, dict) or "id" not in created:
        raise RuntimeError(f"unexpected response creating rune page: {created!r}")
    log.info("created scratch rune page id=%d", created["id"])
    return created["id"]
