"""Enrichment: origin / destination for recorded passes, from the OpenSky Network — earned, good-citizen.

Why OpenSky, and why this shape:
  * Access is EARNED by feeding: an active feeder (receiver online > 30% of the month) gets 8,000 credits a
    day. The server only spends them on aircraft its own receiver saw.
  * OpenSky's flight data (`/flights/aircraft`: estDepartureAirport / estArrivalAirport) is batch-processed
    overnight, so it can't answer about a flight in progress. The job enriches YESTERDAY's passes, after
    `READY_AFTER_S` past midnight UTC, and learns: the next time that callsign passes, the history says
    where it usually goes (`usual_route`).
  * At most ONE call per airframe per UTC day, however many passes it made (`opensky_lookups` remembers).
    An empty answer is retried a few times (the batch may lag), then given up. A daily credit cap
    (`ADSB_OPENSKY_DAILY_CREDITS`), a reserve read from `X-Rate-Limit-Remaining` (`ADSB_OPENSKY_RESERVE`), and
    HTTP 429's retry-after all stop the job for the day.
  * Only this background job talks to OpenSky. The MCP tools read the local store, so they stay
    openWorldHint=false; the README says plainly that the server contacts OpenSky when configured.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings
from .store import get_meta, set_meta

log = logging.getLogger("adsb_mcp.enrich")

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API = "https://opensky-network.org/api"
CALL_COST = 30               # a past-day /flights/* query (1–2 day partitions), per OpenSky's credit table
# The query spans the PREVIOUS UTC day too (2 partitions — still 30 credits, within the 2-day limit). Measured:
# evening arrivals in a UTC-5 country land after 00:00 UTC, but OpenSky files the flight under the day it
# departed, so a one-day query never returned it (AAL833 MIA→UIO, every night: no_match).
WINDOW_BEFORE_S = 86400
READY_AFTER_S = 4 * 3600     # look day D up only after D+1 04:00 UTC: the nightly batch should be done
LOOKBACK_DAYS = 7            # older un-enriched days are left alone
EMPTY_RETRY_S = 6 * 3600     # an empty answer is retried after this...
MAX_ATTEMPTS = 3             # ...this many times in total, then given up
MATCH_SLACK_S = 900          # OpenSky's first/last seen vs ours: different receivers, different edges
# ...but the SAME airframe flying the SAME callsign may sit further off: OpenSky often first sees a domestic
# departure 30+ min after takeoff, far from our receiver (measured: LNE1397 / HC-CTX no_match every day).
SAME_CALLSIGN_MAX_GAP_S = 3 * 3600
ENRICH_EVERY_S = 3600


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d")


def _day_start(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()


class OpenSkyError(RuntimeError):
    pass


class OpenSky:
    """Minimal OAuth2 (client credentials) client for one endpoint. Tokens last ~30 min; refreshed early."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=30)
        self._token: str | None = None
        self._expires = 0.0

    async def _bearer(self, force: bool = False) -> str:
        if force or not self._token or time.time() > self._expires:
            try:
                r = await self.http.post(TOKEN_URL, data={"grant_type": "client_credentials",
                                                          "client_id": self.s.opensky_client_id,
                                                          "client_secret": self.s.opensky_client_secret})
                r.raise_for_status()
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise OpenSkyError(f"token request failed: {e}") from e
            self._token = body["access_token"]
            self._expires = time.time() + float(body.get("expires_in", 1800)) - 60
        return self._token

    async def flights_for(self, hex_: str, begin: int, end: int) -> httpx.Response:
        params = {"icao24": hex_, "begin": begin, "end": end}
        for attempt in (0, 1):
            token = await self._bearer(force=attempt == 1)
            try:
                r = await self.http.get(f"{API}/flights/aircraft", params=params,
                                        headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise OpenSkyError(f"flights request failed: {e}") from e
            if r.status_code != 401:
                return r
        return r


def _match(p: sqlite3.Row, flights: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float | None]:
    """The OpenSky flight that is our pass, and the time gap to it (s; 0 = overlapping).

    Any flight within `MATCH_SLACK_S` qualifies; a flight with the SAME callsign (it's the same airframe —
    one lookup per airframe) may be up to `SAME_CALLSIGN_MAX_GAP_S` away. Preference: same callsign, then
    the smallest gap, then the largest overlap — so a round trip flown under one callsign still gets the
    right leg. With no match, the gap returned is the NEAREST flight's (None: OpenSky had no flights),
    kept as `route_gap_s` so the matching can be measured, not guessed."""
    best, best_key, best_gap, nearest = None, None, None, None
    for f in flights:
        fs, ls = f.get("firstSeen"), f.get("lastSeen")
        if fs is None or ls is None:
            continue
        gap = max(0.0, fs - p["last_seen"], p["first_seen"] - ls)
        nearest = gap if nearest is None else min(nearest, gap)
        same = bool(p["callsign"]) and (f.get("callsign") or "").strip().upper() == p["callsign"].upper()
        if gap > (SAME_CALLSIGN_MAX_GAP_S if same else MATCH_SLACK_S):
            continue
        key = (same, -gap, min(p["last_seen"], ls) - max(p["first_seen"], fs))
        if best_key is None or key > best_key:
            best, best_key, best_gap = f, key, gap
    return best, (best_gap if best is not None else nearest)


def _budget_left(conn: sqlite3.Connection, s: Settings, now: float) -> tuple[bool, str]:
    paused = get_meta(conn, "opensky_paused_until")
    if paused and now < float(paused):
        return False, "paused by OpenSky (429) until " + datetime.fromtimestamp(float(paused), UTC).isoformat()
    if get_meta(conn, "opensky_day") != _day(now):
        set_meta(conn, "opensky_day", _day(now))
        set_meta(conn, "opensky_credits_today", 0)
    used = int(get_meta(conn, "opensky_credits_today") or 0)
    if used + CALL_COST > s.opensky_daily_credits:
        return False, f"daily credit cap reached ({used}/{s.opensky_daily_credits})"
    remaining = get_meta(conn, "opensky_remaining")
    if remaining is not None and get_meta(conn, "opensky_remaining_day") == _day(now) \
            and int(remaining) < s.opensky_reserve:
        return False, f"OpenSky reports {remaining} credits left (< reserve {s.opensky_reserve})"
    return True, "ok"


def _settle(conn: sqlite3.Connection, hex_: str, day: str, flights: list[dict[str, Any]] | None) -> int:
    """Write routes onto that airframe's passes for the day. flights=None: given up, mark as looked-up."""
    start = _day_start(day)
    rows = conn.execute("SELECT * FROM passes WHERE hex = ? AND open = 0 AND route_source IS NULL "
                        "AND first_seen >= ? AND first_seen < ?", (hex_, start, start + 86400)).fetchall()
    matched = 0
    for p in rows:
        f, gap = _match(p, flights) if flights else (None, None)
        if f:
            matched += 1
            conn.execute("UPDATE passes SET origin = ?, destination = ?, route_source = 'opensky', route_gap_s = ? "
                         "WHERE id = ?", (f.get("estDepartureAirport"), f.get("estArrivalAirport"), gap, p["id"]))
        else:
            conn.execute("UPDATE passes SET route_source = 'opensky:none', route_gap_s = ? WHERE id = ?",
                         (gap, p["id"]))
    return matched


async def enrich_once(settings: Settings, conn: sqlite3.Connection, client: OpenSky,
                      now: float | None = None) -> dict[str, Any]:
    """One enrichment run. Returns a summary; never raises for OpenSky trouble (it's logged + recorded)."""
    now = time.time() if now is None else now
    summary = {"calls": 0, "matched": 0, "stopped": None}
    candidates = conn.execute(
        "SELECT hex, strftime('%Y-%m-%d', first_seen, 'unixepoch') AS day FROM passes "
        "WHERE open = 0 AND route_source IS NULL AND first_seen >= ? GROUP BY hex, day ORDER BY day, hex",
        (now - LOOKBACK_DAYS * 86400,)).fetchall()
    for c in candidates:
        hex_, day = c["hex"], c["day"]
        start = _day_start(day)
        if now < start + 86400 + READY_AFTER_S:
            continue                                    # OpenSky hasn't processed that day yet
        prev = conn.execute("SELECT * FROM opensky_lookups WHERE hex = ? AND day = ?", (hex_, day)).fetchone()
        if prev is not None:
            if prev["status"] == "ok" or prev["attempts"] >= MAX_ATTEMPTS:
                _settle(conn, hex_, day, None)          # passes that closed after the lookup / gave up
                continue
            if now - prev["checked_at"] < EMPTY_RETRY_S:
                continue
        ok, why = _budget_left(conn, settings, now)
        if not ok:
            summary["stopped"] = why
            break
        try:
            r = await client.flights_for(hex_, int(start - WINDOW_BEFORE_S), int(start + 86399))
        except OpenSkyError as e:
            summary["stopped"] = f"error: {e}"
            set_meta(conn, "opensky_last_status", summary["stopped"][:300])
            break
        summary["calls"] += 1
        set_meta(conn, "opensky_credits_today", int(get_meta(conn, "opensky_credits_today") or 0) + CALL_COST)
        if (rem := r.headers.get("X-Rate-Limit-Remaining")) is not None:
            set_meta(conn, "opensky_remaining", rem)
            set_meta(conn, "opensky_remaining_day", _day(now))
        if r.status_code == 429:
            wait = float(r.headers.get("X-Rate-Limit-Retry-After-Seconds") or 3600)
            set_meta(conn, "opensky_paused_until", now + wait)
            summary["stopped"] = f"rate limited by OpenSky for {round(wait)} s"
            break
        attempts = (prev["attempts"] if prev else 0) + 1
        if r.status_code == 200 and isinstance(data := r.json(), list) and data:
            status, n = "ok", len(data)
            summary["matched"] += _settle(conn, hex_, day, data)
        elif r.status_code in (200, 404):               # OpenSky answers 404 when it has no flights
            status, n = "empty", 0
            if attempts >= MAX_ATTEMPTS:
                _settle(conn, hex_, day, None)
        else:
            status, n = "error", None
            log.warning("OpenSky /flights/aircraft %s %s -> HTTP %s", hex_, day, r.status_code)
        conn.execute("INSERT INTO opensky_lookups (hex, day, status, flights, attempts, checked_at) "
                     "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(hex, day) DO UPDATE SET status = excluded.status, "
                     "flights = excluded.flights, attempts = excluded.attempts, checked_at = excluded.checked_at",
                     (hex_, day, status, n, attempts, now))
        conn.commit()
    set_meta(conn, "opensky_last_run_utc", datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    set_meta(conn, "opensky_last_status", summary["stopped"] or "ok")
    conn.commit()
    if summary["calls"] or summary["stopped"]:
        log.info("enrichment: %d call(s), %d pass(es) matched%s", summary["calls"], summary["matched"],
                 f"; stopped: {summary['stopped']}" if summary["stopped"] else "")
    return summary


# --- reading routes: OpenSky's value is stored verbatim; aliases are applied here ------------------------

def airport(code: str | None, settings: Settings | None) -> str | None:
    """OpenSky's airport code, with a configured alias of our airport mapped to it."""
    if code and settings and settings.airport_icao and code.upper() in settings.airport_aliases:
        return settings.airport_icao
    return code


def airport_sql(col: str, settings: Settings | None) -> str:
    """SQL for `airport()` (codes are validated alphanumerics in config, so inlining them is safe)."""
    if not (settings and settings.airport_icao and settings.airport_aliases):
        return col
    codes = ", ".join(f"'{c}'" for c in settings.airport_aliases)
    return f"(CASE WHEN {col} IN ({codes}) THEN '{settings.airport_icao}' ELSE {col} END)"


def lookup_state(route_source: str | None, open_: int) -> str | None:
    """matched | no_match (looked up, OpenSky had no flight overlapping the pass) | pending | None (open)."""
    if route_source == "opensky":
        return "matched"
    if route_source == "opensky:none":
        return "no_match"
    return None if open_ else "pending"


def usual_route(conn: sqlite3.Connection, callsign: str | None, settings: Settings | None = None,
                days: int = 90) -> dict[str, Any] | None:
    """Where this callsign has usually flown, learned from enriched passes. None if nothing's known."""
    if not callsign:
        return None
    o, d = airport_sql("origin", settings), airport_sql("destination", settings)
    rows = conn.execute(
        f"SELECT {o} AS origin, {d} AS destination, COUNT(*) AS n FROM passes WHERE callsign = ? "
        "AND route_source = 'opensky' AND (origin IS NOT NULL OR destination IS NOT NULL) AND first_seen >= ? "
        f"GROUP BY {o}, {d} ORDER BY n DESC", (callsign, time.time() - days * 86400)).fetchall()
    if not rows:
        return None
    total = sum(r["n"] for r in rows)
    top = rows[0]
    return {"origin": top["origin"], "destination": top["destination"], "seen": top["n"], "of": total,
            "basis": "usual route of this callsign, from past passes (OpenSky)"}


def enrichment_status(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    week = conn.execute(
        "SELECT SUM(route_source = 'opensky') AS matched, SUM(route_source = 'opensky:none') AS no_match, "
        "SUM(route_source IS NULL) AS pending FROM passes WHERE open = 0 AND first_seen >= ?",
        (time.time() - 7 * 86400,)).fetchone()
    return {"enabled": settings.enrichment_enabled, "source": "opensky",
            "last_7_days": {k: week[k] or 0 for k in ("matched", "no_match", "pending")},
            "airport_aliases": list(settings.airport_aliases),
            "last_run_utc": get_meta(conn, "opensky_last_run_utc"),
            "last_status": get_meta(conn, "opensky_last_status"),
            "credits_today": int(get_meta(conn, "opensky_credits_today") or 0),
            "remaining_reported": get_meta(conn, "opensky_remaining")}
