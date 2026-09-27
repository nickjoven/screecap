"""Per-champion loadout memory: SQLite store + LCU capture/apply.

The store keeps one row per (puuid, champion_id) — last spells used, last
rune page contents (snapshotted, not just an id), last seen time, and a play
counter. The tracker watches champ-select session events and writes a row on
session end. Apply pushes the row back to the client by writing to a single
dedicated `[screecap] auto` rune page so the user's hand-curated pages are
never overwritten.

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


@dataclass
class Loadout:
    puuid: str
    champion_id: int
    spell1_id: int
    spell2_id: int
    rune_page: Optional[dict]   # {name, primaryStyleId, subStyleId, selectedPerkIds}
    last_seen: datetime
    count: int


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
    return conn


def record_loadout(puuid: str, champion_id: int, spell1_id: int,
                   spell2_id: int, rune_page: Optional[dict]) -> None:
    """Insert or update the row for (puuid, champion_id).

    rune_page is preserved across writes if the new value is None — that way
    a session that ended without us being able to GET /currentpage doesn't
    wipe a previously-snapshotted page.
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
                spell1_id = excluded.spell1_id,
                spell2_id = excluded.spell2_id,
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
            SELECT spell1_id, spell2_id, rune_page_json, last_seen, count
            FROM loadout WHERE puuid = ? AND champion_id = ?
        """, (puuid, champion_id)).fetchone()
    if row is None:
        return None
    spell1_id, spell2_id, rune_json, last_seen, count = row
    return Loadout(
        puuid=puuid,
        champion_id=champion_id,
        spell1_id=spell1_id,
        spell2_id=spell2_id,
        rune_page=json.loads(rune_json) if rune_json else None,
        last_seen=datetime.fromisoformat(last_seen),
        count=count,
    )


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
            if self._on_session_changed:
                try:
                    self._on_session_changed(None)
                except Exception:
                    log.exception("session_changed(None) callback raised")
            return
        # Create or Update — `data` is the full session dict.
        if isinstance(data, dict):
            self._latest_session = data
            if self._on_session_changed:
                try:
                    self._on_session_changed(data)
                except Exception:
                    log.exception("session_changed callback raised")

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
        client.request("PATCH", "/lol-champ-select/v1/session/my-selection",
                       {"spell1Id": loadout.spell1_id,
                        "spell2Id": loadout.spell2_id})
        log.info("applied spells: champ=%d (%s, %s)",
                 loadout.champion_id,
                 spell_name(loadout.spell1_id), spell_name(loadout.spell2_id))
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
