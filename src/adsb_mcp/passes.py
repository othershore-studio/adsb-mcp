"""The recorder: turn a stream of aircraft.json snapshots into PASSES (one row per visit, not per poll).

A pass opens when an airborne aircraft is inside the zone — within `pass_radius_km` of the reference point
(the site, else the airport), or on approach to the airport. While it's open, every snapshot the aircraft
appears in updates it (altitudes, closest approach, RSSI), so the exit is captured too. It closes once the
aircraft has been gone, or out of the zone, for `pass_grace_s`. If it comes back within `pass_merge_s` of
closing, that pass is reopened (a coverage gap must not split one visit) — unless it ended with a confirmed
landing.

Arrival tracking (needs the airport position + field elevation). ADS-B coverage usually ends before the
runway, so an aircraft lost while ON APPROACH is kept open for `arrival_grace_s`: it's presumably landing.
The heuristics are deliberately simple and documented here:
  * on approach   : within `approach_km` of the airport, below 10,000 ft above the field, descending ≥ 300 fpm.
  * landed        : on the ground within 5 km of the airport after an approach (confirmed).
  * go-around     : after getting below 2,000 ft above the field, climbing ≥ 700 ft back up at ≥ 500 fpm
                    within 15 km of the airport. Counted, and the approach must re-arm to count another.
  * departure     : in its first few snapshots (`DEPARTURE_SAMPLES`), airborne within 25 km of the airport,
                    below 5,000 ft above it, climbing ≥ 300 fpm. (Widened 2026-10-23 from "first sample, 10 km,
                    3,000 ft": the same daily departure, LNE1397, was a departure 3 days and an overflight 4 —
                    terrain hides the takeoff and the receiver first sees it ~20 km out, turning south.)
  * loitering     : circled — heading changed ≥ 360° in total (`LOITER_TURN_DEG`) while the pass ranged
                    ≤ 15 km (`LOITER_SPREAD_KM`, from the distances to the reference: no coordinates kept),
                    or ≤ 40 km for an aircraft that flew ≥ 150 kt (`LOITER_FAST_*`): a 737's flypast laps over
                    the city (FAE-640, 26–27 Oct: up to 4,485° of turning) range far wider than a helicopter.
                    Patrols, training, survey flights. Once loitering, climbs and descents no longer count
                    as go-arounds (a Grob G 120TP's training session was recorded as 4 go-arounds).
Outcome on close: arrival (landed seen) > arrival_presumed (lost on approach) > loitering > go_around (went
around, then left) > departure > overflight. Without arrival tracking only `loitering` is set (else null).

Feeder-down is NOT absence: a failed poll touches no pass (see `poll_once`), so an outage never closes or
splits the passes that were open.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

from .config import Settings
from .feeder import FeederError, Snapshots, haversine_km
from .store import connect, set_meta

log = logging.getLogger("adsb_mcp.passes")

MAX_POS_AGE_S = 60          # an older position is stale: treat the aircraft as present but position-less
APPROACH_CEILING_AGL = 10_000
APPROACH_DESCENT_FPM = -300
LANDED_KM = 5
GA_LOW_AGL = 2_000
GA_CLIMB_FT = 700
GA_RATE_FPM = 500
GA_KM = 15
DEPARTURE_KM = 25
DEPARTURE_AGL = 5_000
DEPARTURE_SAMPLES = 4       # snapshots at the start of a pass in which the departure rule may fire
LOITER_TURN_DEG = 360
LOITER_SPREAD_KM = 15
LOITER_FAST_KT = 150        # at or above this ground speed (max seen in the pass) the wider spread applies
LOITER_FAST_SPREAD_KM = 40
TURN_MIN_STEP_DEG = 2       # heading jitter below this between snapshots isn't a turn
TURN_MIN_GS_KT = 20         # a near-hovering helicopter's track is noise
DEPARTURE_CLIMB_FPM = 300


# --- airport geometry, shared by the recorder and the board ---------------------------------------------

def fresh_position(a: dict[str, Any]) -> bool:
    return (a["lat"] is not None and a["lon"] is not None
            and a["seen_pos_s"] is not None and a["seen_pos_s"] <= MAX_POS_AGE_S)


def airport_frame(a: dict[str, Any], s: Settings) -> tuple[float | None, float | None]:
    """(distance to the airport km, height above the field ft) — None where unknown or arrivals are off."""
    if not s.arrivals_enabled or not fresh_position(a):
        return None, None
    d_ap = haversine_km(s.airport_lat, s.airport_lon, a["lat"], a["lon"])
    alt = a["altitude_ft"]
    agl = alt - s.airport_elev_ft if alt is not None and not a["on_ground"] else None
    return d_ap, agl


def on_approach(d_ap: float | None, agl: float | None, vr: float | None, s: Settings) -> bool:
    return (agl is not None and d_ap <= s.approach_km and agl <= APPROACH_CEILING_AGL
            and vr is not None and vr <= APPROACH_DESCENT_FPM)


def climbing_out(d_ap: float | None, agl: float | None, vr: float | None, s: Settings) -> bool:
    """Climbing away from the airport area (the board's departures; not the recorder's first-seen rule)."""
    return (agl is not None and d_ap <= s.approach_km and agl <= APPROACH_CEILING_AGL
            and vr is not None and vr >= DEPARTURE_CLIMB_FPM)


@dataclass
class Pass:
    hex: str
    first_seen: float
    last_seen: float
    last_in_zone: float
    reference: str | None
    id: int | None = None
    callsign: str | None = None
    reg: str | None = None
    type: str | None = None
    category: str | None = None
    min_alt_ft: int | None = None
    max_alt_ft: int | None = None
    min_dist_km: float | None = None
    max_rssi: float | None = None
    points: int = 0
    emergency: str | None = None
    departure: int = 0
    on_approach: int = 0
    approach_low_agl_ft: float | None = None
    go_arounds: int = 0
    landed_at: float | None = None
    outcome: str | None = None
    open: int = 1
    origin: str | None = None        # set by the enricher (enrich.py) on closed passes, carried on reopen
    destination: str | None = None
    route_source: str | None = None
    military: int = 0                # readsb's database flag; ADSB_MILITARY_PATTERNS are applied on read
    turn_deg: float = 0.0
    last_track: float | None = None
    max_dist_km: float | None = None
    max_gs_kt: float | None = None
    route_gap_s: float | None = None  # set by the enricher

    @property
    def loitering(self) -> bool:
        if self.turn_deg < LOITER_TURN_DEG or self.max_dist_km is None or self.min_dist_km is None:
            return False
        fast = self.max_gs_kt is not None and self.max_gs_kt >= LOITER_FAST_KT
        return self.max_dist_km - self.min_dist_km <= (LOITER_FAST_SPREAD_KM if fast else LOITER_SPREAD_KM)


_COLS = [f.name for f in fields(Pass) if f.name != "id"]


def _min(a, b):
    return b if a is None else (a if b is None else min(a, b))


def _max(a, b):
    return b if a is None else (a if b is None else max(a, b))


class Tracker:
    """Holds the open passes (mirrored in the DB) and folds each snapshot into them."""

    def __init__(self, settings: Settings, conn: sqlite3.Connection) -> None:
        ref = settings.reference
        if ref is None:
            raise ValueError("pass tracking needs a reference point: set ADSB_SITE_LAT/LON or ADSB_AIRPORT_LAT/LON")
        self.s = settings
        self.conn = conn
        self.ref_kind, self.ref = ref
        self.open: dict[str, Pass] = {}
        self._closed: list[Pass] = []
        for row in conn.execute("SELECT * FROM passes WHERE open = 1"):
            p = Pass(**{k: row[k] for k in row.keys()})
            self.open[p.hex] = p
        if self.open:
            log.info("resumed %d open pass(es)", len(self.open))

    # --- one snapshot -------------------------------------------------------------------------------
    def ingest(self, snap: dict[str, Any]) -> None:
        now = float(snap.get("now") or time.time())
        s = self.s
        for a in snap["aircraft"]:
            hx = a["hex"]
            if not hx:
                continue
            t = now - (a["seen_s"] or 0)
            has_pos = fresh_position(a)
            ground = a["on_ground"]
            alt = a["altitude_ft"]
            vr = a["vertical_rate_fpm"]
            d_ref = haversine_km(self.ref[0], self.ref[1], a["lat"], a["lon"]) if has_pos else None
            d_ap, agl = airport_frame(a, s)
            approaching = on_approach(d_ap, agl, vr, s)
            in_zone = has_pos and not ground and (d_ref <= s.pass_radius_km or approaching)

            p = self.open.get(hx)
            if p is None:
                if not in_zone:
                    continue  # parked / far / position-less: nothing to open
                p = self._reopen(hx, t)
                if p is None:
                    p = Pass(hex=hx, first_seen=t, last_seen=t, last_in_zone=t, reference=self.ref_kind)
                self.open[hx] = p

            if (not p.departure and p.points < DEPARTURE_SAMPLES and p.approach_low_agl_ft is None
                    and agl is not None and d_ap <= DEPARTURE_KM and agl <= DEPARTURE_AGL
                    and vr is not None and vr >= DEPARTURE_CLIMB_FPM):
                p.departure = 1

            p.last_seen = max(p.last_seen, t)
            p.points += 1
            p.callsign = p.callsign or a["callsign"]
            p.reg = p.reg or a["registration"]
            p.type = p.type or a["type"]
            p.category = p.category or a["category"]
            p.max_rssi = _max(p.max_rssi, a["rssi_dbfs"])
            p.emergency = a["emergency"] or p.emergency
            p.military = int(p.military or a.get("military", False))
            trk, gs = a["track_deg"], a["ground_speed_kt"]
            if not ground and gs is not None:
                p.max_gs_kt = _max(p.max_gs_kt, round(gs))
            if not ground and trk is not None and gs is not None and gs >= TURN_MIN_GS_KT:
                if p.last_track is not None:
                    step = abs((trk - p.last_track + 540) % 360 - 180)
                    if step >= TURN_MIN_STEP_DEG:
                        p.turn_deg = round(p.turn_deg + step, 1)
                p.last_track = trk
            if has_pos and not ground and alt is not None:
                p.min_alt_ft = _min(p.min_alt_ft, alt)
                p.max_alt_ft = _max(p.max_alt_ft, alt)
            if d_ref is not None:
                p.min_dist_km = round(_min(p.min_dist_km, d_ref), 2)
                p.max_dist_km = round(_max(p.max_dist_km, d_ref), 2)
            if in_zone:
                p.last_in_zone = max(p.last_in_zone, t)
            if d_ap is not None:
                self._arrival(p, t, ground, d_ap, agl, vr, approaching)

        self._close_stale(now)
        self._write()

    def _reopen(self, hx: str, t: float) -> Pass | None:
        """The aircraft is back after a coverage gap: if its last pass closed recently, continue that pass
        instead of starting a new one (a hold or a turn behind terrain must not split one visit in two).
        A pass that ended on the ground (confirmed landing) is never reopened: the next flight is new."""
        row = self.conn.execute("SELECT * FROM passes WHERE hex = ? AND open = 0 AND landed_at IS NULL "
                                "ORDER BY last_seen DESC LIMIT 1", (hx,)).fetchone()
        if row is None or t - row["last_seen"] > self.s.pass_merge_s:
            return None
        p = Pass(**{k: row[k] for k in row.keys()})
        p.open, p.outcome = 1, None       # the outcome is decided again when it closes
        log.info("reopened pass %d for %s after a %.0f s gap", p.id, hx, t - p.last_seen)
        return p

    def _arrival(self, p: Pass, t: float, ground: bool, d_ap: float, agl: float | None,
                 vr: float | None, approaching: bool) -> None:
        if ground:
            if d_ap <= LANDED_KM and (p.on_approach or p.approach_low_agl_ft is not None) and p.landed_at is None:
                p.landed_at = t
                p.on_approach = 0
            return
        if agl is None:
            return
        low = p.approach_low_agl_ft
        if (not p.loitering and low is not None and low <= GA_LOW_AGL and agl >= low + GA_CLIMB_FT and vr is not None
                and vr >= GA_RATE_FPM and d_ap <= GA_KM and p.landed_at is None):
            p.go_arounds += 1
            p.approach_low_agl_ft = None   # re-arm: another approach is needed to count another go-around
            p.on_approach = 0
            return
        if approaching or (low is not None and d_ap <= GA_KM):
            p.approach_low_agl_ft = agl if low is None else min(low, agl)
        p.on_approach = int(approaching)

    def _close_stale(self, now: float) -> None:
        s = self.s
        for hx, p in list(self.open.items()):
            grace = s.arrival_grace_s if (p.on_approach and p.landed_at is None) else s.pass_grace_s
            if now - p.last_seen > grace or now - p.last_in_zone > grace:
                p.open = 0
                if p.loitering and p.landed_at is None:
                    p.go_arounds = 0       # climbs while working an area aren't go-arounds
                p.outcome = self._outcome(p) if s.arrivals_enabled else ("loitering" if p.loitering else None)
                del self.open[hx]
                self._closed.append(p)

    @staticmethod
    def _outcome(p: Pass) -> str:
        if p.landed_at is not None:
            return "arrival"
        if p.on_approach:
            return "arrival_presumed"
        if p.loitering:
            return "loitering"
        if p.go_arounds:
            return "go_around"
        if p.departure:
            return "departure"
        return "overflight"

    def _write(self) -> None:
        for p in [*self.open.values(), *self._closed]:
            vals = [getattr(p, c) for c in _COLS]
            if p.id is None:
                cur = self.conn.execute(
                    f"INSERT INTO passes ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})", vals)
                p.id = cur.lastrowid
            else:
                self.conn.execute(f"UPDATE passes SET {', '.join(c + '=?' for c in _COLS)} WHERE id=?", [*vals, p.id])
        self._closed = []
        self.conn.commit()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def poll_once(snapshots: Snapshots, tracker: Tracker) -> bool:
    """One recorder tick. A failed fetch is recorded and skipped — it never closes a pass."""
    now = time.time()
    try:
        snap = await snapshots.refresh()
    except FeederError as e:
        set_meta(tracker.conn, "last_poll_utc", _iso(now))
        set_meta(tracker.conn, "last_poll_status", f"error: {e}"[:300])
        tracker.conn.commit()
        log.warning("poll failed (passes left open): %s", e)
        return False
    tracker.ingest(snap)
    set_meta(tracker.conn, "last_poll_utc", _iso(now))
    set_meta(tracker.conn, "last_poll_ok_utc", _iso(now))
    set_meta(tracker.conn, "last_poll_status", "ok")
    tracker.conn.commit()
    return True


def open_tracker(settings: Settings) -> Tracker:
    return Tracker(settings, connect(settings.db_path))
