"""MCP server: local ADS-B flight data as tools, over your own receiver's aircraft.json.

Design contract:
  * Live tier (aircraft_overhead / aircraft_near / find_aircraft): the current snapshot — fetched, or the
    background recorder's latest while fresh. Inputs pass through verbatim; resolved parameters are echoed.
  * History tier (traffic_stats / aircraft_history): read the recorder's local store of PASSES.
  * ok / refused / error status on every result — branch on `status`, never on prose.
  * The receiver location is home-location PII: it comes from env, never the repo, and is NEVER echoed in a
    result (distances and bearings only). The airport, a public place, may be.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent, ToolAnnotations

from .config import Settings
from .feeder import FeederClient, FeederError, Snapshots, bearing_deg, haversine_km
from .enrich import airport, airport_sql, enrichment_status, lookup_state, usual_route
from .store import get_meta

INSTRUCTIONS = """\
Aircraft seen by a local ADS-B receiver (its keyless aircraft.json — your OWN data). Live tools:
aircraft_overhead (what's in range now, nearest first), aircraft_near (within a radius of a given point),
find_aircraft (by callsign / hex / registration). The star query is "¿qué avión es ese?" — what's flying
overhead right now. History tools (when the server records): traffic_stats (most / least common airframes,
callsigns, types, outcomes, busiest hours over a window) and aircraft_history ("have we seen this one
before?" — the past passes of an airframe / callsign / registration).

Results are structured DATA (hex, callsign, registration, type, altitude_ft, ground_speed_kt, track_deg,
lat/lon, distance_km + bearing_deg from the reference point, seen_s). Answer in the user's language; cite
that it's local ADS-B. Altitudes are feet, speed knots, track/bearing degrees.

A PASS is one visit of an aircraft through the receiver's area (one row, not one per poll). With an airport
configured each pass has an outcome: arrival (seen landing), arrival_presumed (lost on approach — coverage
usually ends before the runway), go_around, departure or overflight; go_arounds counts missed approaches.
Three different "most common" questions: airframe (hex/registration — the physical aircraft), callsign
(the route/service), type (the model).

ADS-B carries no route — only what the aircraft transmits. When the operator enables enrichment, past
passes get origin/destination airports (ICAO codes, from OpenSky, learned the day after), and live aircraft
carry `usual_route`: where that callsign has usually flown (`seen` of `of` past passes) — a strong hint,
not a flight plan; say so ("usually flies to SPJC, Lima"). `unusual_route` on a past pass marks one that
differed from its callsign's usual route (a diversion, an unscheduled landing).

Every result has a `status`: "ok", "refused" (caller-fixable input or a tier not enabled, with `advice`),
or "error" (the receiver is unreachable — `kind`+`detail`; not something a reworded query fixes).
"""

GROUP_BY = ("airframe", "callsign", "type", "category", "outcome", "route", "hour")
ONLY = ("military", "rotorcraft")
ROTORCRAFT = "A7"            # ADS-B emitter category for helicopters

# "Approximate location" for aircraft_overhead (ADSB_EXACT_LOCATION off): enough for "about 12 km to the
# south", too coarse for one result to pinpoint the site. 2 decimals ≈ 1.1 km.
APPROX_DEC = 2
APPROX_BEARING = 10

# One SELECT per grouping (all GROUP BY the key; `first`/`last` are reshaped into local times).
_GROUP_SQL = {
    "airframe": ("hex", "SELECT hex, MAX(reg) AS registration, MAX(type) AS type, COUNT(*) AS passes, "
                        "GROUP_CONCAT(DISTINCT callsign) AS callsigns, MIN(first_seen) AS first, MAX(last_seen) AS last"),
    "callsign": ("callsign", "SELECT callsign, COUNT(*) AS passes, COUNT(DISTINCT hex) AS airframes, "
                             "GROUP_CONCAT(DISTINCT type) AS types, MIN(first_seen) AS first, MAX(last_seen) AS last"),
    "type": ("type", "SELECT type, COUNT(*) AS passes, COUNT(DISTINCT hex) AS airframes, "
                     "MIN(first_seen) AS first, MAX(last_seen) AS last"),
    "category": ("category", "SELECT category, COUNT(*) AS passes, COUNT(DISTINCT hex) AS airframes, "
                             "GROUP_CONCAT(DISTINCT type) AS types, MIN(first_seen) AS first, MAX(last_seen) AS last"),
    "outcome": ("outcome", "SELECT outcome, COUNT(*) AS passes, COUNT(DISTINCT hex) AS airframes, "
                           "MIN(first_seen) AS first, MAX(last_seen) AS last"),
    "route": ("route", "SELECT COALESCE({o}, '?') || '-' || COALESCE({d}, '?') AS route, {o} AS origin, "
                       "{d} AS destination, COUNT(*) AS passes, COUNT(DISTINCT hex) AS airframes, "
                       "GROUP_CONCAT(DISTINCT callsign) AS callsigns, MIN(first_seen) AS first, MAX(last_seen) AS last"),
}
# A usual route is "reliable" enough to flag a pass that differs from it.
RELIABLE_SEEN, RELIABLE_SHARE = 3, 0.6


def _read_hints(title: str) -> ToolAnnotations:
    # Pure reads: non-destructive, idempotent (same snapshot / store -> same answer). openWorld = FALSE: it
    # talks only to the lab's OWN LAN receiver or its own local store — a closed dependency, not the public
    # web or a third party. OpenSky enrichment is a background job writing to that store; the tools never
    # call out (a tool that queried OpenSky itself WOULD be openWorld=true).
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                           openWorldHint=False)


def _text(payload: dict[str, Any]) -> list[Any]:
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def _refused(reason: str, **extra: Any) -> list[Any]:
    """A caller-fixable input decision (not an error): adjust the request, don't retry unchanged."""
    return _text({"status": "refused", "reason": reason, **extra})


def _error(kind: str, detail: str) -> list[Any]:
    """A genuine fault (receiver down / bad body) — infra, not policy; a prompt change won't fix it."""
    return _text({"status": "error", "kind": kind, "detail": detail})


def build_server(settings: Settings, snapshots: Snapshots | None = None) -> MCPServer:
    mcp = MCPServer(name="adsb", instructions=INSTRUCTIONS)
    if snapshots is None:
        snapshots = Snapshots(FeederClient(settings))
    ref = settings.reference
    try:
        tz = ZoneInfo(settings.tz)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")

    def _positioned(aircraft: list[dict], include_grounded: bool) -> list[dict]:
        return [a for a in aircraft if a["lat"] is not None and a["lon"] is not None
                and (include_grounded or not a["on_ground"])]

    def _reference() -> dict[str, Any] | None:
        if ref is None:
            return None
        if ref[0] == "site":
            return {"kind": "site"}           # the site's coordinates are PII: never echoed
        return {"kind": "airport", "icao": settings.airport_icao}

    def _military(flag: Any, callsign: str | None, registration: str | None) -> dict[str, Any]:
        if flag:
            return {"military": True, "military_basis": "readsb"}
        if settings.military_match(callsign, registration):
            return {"military": True, "military_basis": "pattern"}
        return {"military": False}

    def _military_sql() -> str:
        # Patterns are validated [A-Z0-9*?-] in config, so inlining them as GLOB literals is safe.
        terms = ["military = 1"] + [f"upper({c}) GLOB '{p}'" for p in settings.military_patterns
                                     for c in ("callsign", "reg")]
        return "(" + " OR ".join(terms) + ")"

    def _with_routes(acs: list[dict]) -> list[dict]:
        """Mark military / state aircraft, and add `usual_route` (learned from enriched past passes)."""
        for a in acs:
            a.update(_military(a.pop("military", False), a.get("callsign"), a.get("registration")))
        conn = _store()
        if conn is None:
            return acs
        try:
            for a in acs:
                if r := usual_route(conn, a.get("callsign"), settings):
                    a["usual_route"] = r
        finally:
            conn.close()
        return acs

    @mcp.tool(title="Aircraft overhead", annotations=_read_hints("Aircraft overhead"), structured_output=False)
    async def aircraft_overhead(max_results: int = 10, include_grounded: bool = False) -> list[Any]:
        """Aircraft currently in range of the receiver, nearest first. The "what's that plane overhead
        right now" query. Distances/bearings are from the receiver location if configured, else from the
        nearby airport (approximate); with neither, results are listed newest-position-first, no distance.
        `precision` says whether positions/distances are exact or rounded (~1 km, 10°) to protect the site.

        Args:
            max_results: cap on aircraft returned (default 10).
            include_grounded: include aircraft on the ground (default False — only airborne).
        """
        try:
            snap, age = await snapshots.get()
        except FeederError as e:
            return _error("feeder", str(e))
        acs = _positioned(snap["aircraft"], include_grounded)
        # Approximate unless the operator opted in: only the site needs hiding (the airport is public).
        approximate = bool(ref) and ref[0] == "site" and not settings.exact_location
        if ref:
            lat, lon = ref[1]
            for a in acs:
                a["distance_km"] = haversine_km(lat, lon, a["lat"], a["lon"])
                a["bearing_deg"] = bearing_deg(lat, lon, a["lat"], a["lon"])
            acs.sort(key=lambda a: a["distance_km"])
            for a in acs:
                if approximate:
                    a["lat"], a["lon"] = round(a["lat"], APPROX_DEC), round(a["lon"], APPROX_DEC)
                    a["distance_km"] = max(1, round(a["distance_km"]))
                    a["bearing_deg"] = round(a["bearing_deg"] / APPROX_BEARING) * APPROX_BEARING % 360
                else:
                    a["distance_km"] = round(a["distance_km"], 1)
                    a["bearing_deg"] = round(a["bearing_deg"])
        else:
            acs.sort(key=lambda a: (a["seen_pos_s"] is None, a["seen_pos_s"] or 0))
        out = {"status": "ok", "source": "adsb", "snapshot_age_s": round(age, 1), "reference": _reference(),
               "site_configured": settings.site is not None,
               "precision": "approximate" if approximate else "exact", "count": len(acs),
               "aircraft": _with_routes(acs[:max(1, max_results)])}
        if approximate:
            out["precision_note"] = ("approximate location: positions rounded to ~1 km, distance to whole km, "
                                     f"bearing to {APPROX_BEARING}°")
        if ref and ref[0] == "airport":
            out["note"] = f"distances are from the airport {settings.airport_icao or ''} (approximate), not the receiver."
        elif not ref:
            out["note"] = ("no reference point set (ADSB_SITE_LAT/LON or ADSB_AIRPORT_LAT/LON) — no distances; "
                           "listed newest-position-first.")
        return _text(out)

    @mcp.tool(title="Aircraft near a point", annotations=_read_hints("Aircraft near a point"), structured_output=False)
    async def aircraft_near(latitude: float, longitude: float, radius_km: float = 50.0,
                            max_results: int = 10, include_grounded: bool = False) -> list[Any]:
        """Aircraft currently within `radius_km` of a given point (lat/lon you supply — e.g. a city), nearest
        first. Unlike aircraft_overhead this needs no receiver location: distance is measured from the point
        you pass.

        Args:
            latitude/longitude: the centre point to search around.
            radius_km: search radius in kilometres (default 50).
            max_results: cap on aircraft returned (default 10).
            include_grounded: include aircraft on the ground (default False).
        """
        try:
            snap, age = await snapshots.get()
        except FeederError as e:
            return _error("feeder", str(e))
        acs = []
        for a in _positioned(snap["aircraft"], include_grounded):
            dist = round(haversine_km(latitude, longitude, a["lat"], a["lon"]), 1)
            if dist <= radius_km:
                a = {**a, "distance_km": dist, "bearing_deg": round(bearing_deg(latitude, longitude, a["lat"], a["lon"]))}
                acs.append(a)
        acs.sort(key=lambda a: a["distance_km"])
        return _text({"status": "ok", "source": "adsb", "snapshot_age_s": round(age, 1),
                      "center": {"lat": latitude, "lon": longitude}, "radius_km": radius_km, "count": len(acs),
                      "aircraft": _with_routes(acs[:max(1, max_results)])})

    @mcp.tool(title="Find aircraft", annotations=_read_hints("Find aircraft"), structured_output=False)
    async def find_aircraft(query: str, max_results: int = 10) -> list[Any]:
        """Find aircraft currently in range by a partial match on callsign, ICAO hex, or registration
        (case-insensitive). Returns matches even if they have no position yet.

        Args:
            query: text to match against callsign / hex / registration (e.g. "LAN", "e80", "CC-B").
            max_results: cap on matches returned (default 10).
        """
        q = query.strip().lower()
        if not q:
            return _refused("empty query", advice="pass a callsign, hex, or registration fragment")
        try:
            snap, age = await snapshots.get()
        except FeederError as e:
            return _error("feeder", str(e))
        hits = [a for a in snap["aircraft"]
                if q in (a["callsign"] or "").lower() or q in (a["hex"] or "") or q in (a["registration"] or "").lower()]
        return _text({"status": "ok", "source": "adsb", "snapshot_age_s": round(age, 1), "query": query,
                      "count": len(hits), "aircraft": _with_routes(hits[:max(1, max_results)])})

    # --- history tier ---------------------------------------------------------------------------------

    def _store() -> sqlite3.Connection | None:
        if not settings.db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{settings.db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _not_recording() -> list[Any]:
        return _refused("history is not recorded on this server",
                        advice="the operator enables it with ADSB_POLL_SECONDS>0 plus a reference point "
                               "(ADSB_SITE_LAT/LON or ADSB_AIRPORT_LAT/LON); use the live tools meanwhile")

    def _local(epoch: float | None) -> str | None:
        return None if epoch is None else datetime.fromtimestamp(epoch, tz).isoformat(timespec="seconds")

    def _recorder(conn: sqlite3.Connection) -> dict[str, Any]:
        info: dict[str, Any] = {"enabled": settings.poll_seconds > 0, "poll_seconds": settings.poll_seconds,
                                "last_poll_utc": get_meta(conn, "last_poll_utc"),
                                "last_poll_ok_utc": get_meta(conn, "last_poll_ok_utc"),
                                "last_poll_status": get_meta(conn, "last_poll_status")}
        if settings.enrichment_enabled or get_meta(conn, "opensky_last_run_utc"):
            info["enrichment"] = enrichment_status(conn, settings)
        ok = info["last_poll_ok_utc"]
        if info["enabled"] and ok:
            lag = time.time() - datetime.fromisoformat(ok.replace("Z", "+00:00")).timestamp()
            if lag > max(300, 10 * settings.poll_seconds):
                info["warning"] = f"recorder stale: last good poll {round(lag)} s ago — recent passes may be missing"
        return info

    def _pass_row(r: sqlite3.Row) -> dict[str, Any]:
        return {"hex": r["hex"], "callsign": r["callsign"], "registration": r["reg"], "type": r["type"],
                "first_seen": _local(r["first_seen"]), "last_seen": _local(r["last_seen"]),
                "duration_s": round(r["last_seen"] - r["first_seen"]),
                "min_alt_ft": r["min_alt_ft"], "max_alt_ft": r["max_alt_ft"],
                "min_dist_km": r["min_dist_km"], "distance_from": r["reference"],
                "max_rssi_dbfs": r["max_rssi"], "points": r["points"], "outcome": r["outcome"],
                "go_arounds": r["go_arounds"], "landed_at": _local(r["landed_at"]),
                "emergency": r["emergency"], "in_progress": bool(r["open"]),
                "category": r["category"], "rotorcraft": r["category"] == ROTORCRAFT,
                **_military(r["military"], r["callsign"], r["reg"]),
                "turn_deg": round(r["turn_deg"] or 0), **_route_fields(r)}

    def _route_fields(r: sqlite3.Row) -> dict[str, Any]:
        # Aliases applied for reading; OpenSky's own code kept alongside whenever it differs.
        out: dict[str, Any] = {"origin": airport(r["origin"], settings),
                               "destination": airport(r["destination"], settings),
                               "route_source": "opensky" if r["route_source"] == "opensky" else None,
                               "route_lookup": lookup_state(r["route_source"], r["open"])}
        for k in ("origin", "destination"):
            if r[k] and out[k] != r[k]:
                out[f"{k}_reported"] = r[k]
        return out

    @mcp.tool(title="Traffic stats", annotations=_read_hints("Traffic stats"), structured_output=False)
    async def traffic_stats(group_by: str = "callsign", since_days: float = 30, order: str = "most",
                            limit: int = 10, only: str = "") -> list[Any]:
        """Counts of recorded PASSES (visits) over a window, grouped one way. Answers "most common flights",
        "which aircraft come most / rarest visitors", "what models fly here", "how many landed / went
        around", "busiest hours".

        Args:
            group_by: airframe (hex/registration — the physical aircraft), callsign (the route/service),
                type (the model), category (ADS-B emitter category; A7 = helicopter), outcome (arrival /
                arrival_presumed / loitering / go_around / departure / overflight), route (origin-destination
                airports, ICAO; needs enrichment), or hour (local hour of day).
            only: "" (all, default), "military" (military / state aircraft) or "rotorcraft" (helicopters).
            since_days: window, in days back from now (default 30).
            order: "most" (default) or "least" — least = the rarest.
            limit: rows returned (default 10, max 100).
        """
        if group_by not in GROUP_BY:
            return _refused(f"unknown group_by {group_by!r}", advice=f"use one of {', '.join(GROUP_BY)}")
        if order not in ("most", "least"):
            return _refused(f"unknown order {order!r}", advice='use "most" or "least"')
        if not 0 < since_days <= 3650:
            return _refused("since_days out of range", advice="pass a window between 0 and 3650 days")
        if not 1 <= limit <= 100:
            return _refused("limit out of range", advice="pass 1..100")
        if only and only not in ONLY:
            return _refused(f"unknown only {only!r}", advice=f'use "", {", ".join(repr(o) for o in ONLY)}')
        conn = _store()
        if conn is None:
            return _not_recording()
        try:
            cutoff = time.time() - since_days * 86400
            subset = {"": "", "military": f" AND {_military_sql()}", "rotorcraft": f" AND category = '{ROTORCRAFT}'"}[only]
            if group_by == "hour":
                counts = [0] * 24
                for (fs,) in conn.execute(f"SELECT first_seen FROM passes WHERE first_seen >= ?{subset}", (cutoff,)):
                    counts[datetime.fromtimestamp(fs, tz).hour] += 1
                sign = -1 if order == "most" else 1
                rows = sorted(({"hour": h, "passes": n} for h, n in enumerate(counts)),
                              key=lambda r: (sign * r["passes"], r["hour"]))[:limit]
            else:
                key, select = _GROUP_SQL[group_by]
                if group_by == "route":
                    select = select.format(o=airport_sql("origin", settings), d=airport_sql("destination", settings))
                not_null = ("" if group_by == "outcome"
                            else " AND route_source = 'opensky' AND (origin IS NOT NULL OR destination IS NOT NULL)"
                            if group_by == "route" else f" AND {key} IS NOT NULL")
                direction = "DESC" if order == "most" else "ASC"
                cur = conn.execute(f"{select} FROM passes WHERE first_seen >= ?{subset}{not_null} GROUP BY {key} "
                                   f"ORDER BY passes {direction}, last DESC LIMIT ?", (cutoff, limit))
                rows = []
                for r in cur:
                    d = {k: r[k] for k in r.keys() if k not in ("first", "last")}
                    for k in ("callsigns", "types"):
                        if k in d:
                            d[k] = sorted(d[k].split(","))[:10] if d[k] else []
                    d["first_seen"], d["last_seen"] = _local(r["first"]), _local(r["last"])
                    rows.append(d)
            total = conn.execute(f"SELECT COUNT(*) AS n, COUNT(DISTINCT hex) AS a FROM passes WHERE first_seen >= ?{subset}",
                                 (cutoff,)).fetchone()
            out = {"status": "ok", "source": "adsb-history", "group_by": group_by, "since_days": since_days,
                   "order": order, "limit": limit, "only": only or None, "tz": tz.key, "total_passes": total["n"],
                   "total_airframes": total["a"], "rows": rows, "recorder": _recorder(conn)}
            if group_by == "route" and not rows:
                out["note"] = ("no routes yet: enrichment (ADSB_OPENSKY_CLIENT_ID/SECRET) fills them in the day "
                               "after a pass")
            if group_by == "outcome" and not settings.arrivals_enabled:
                out["note"] = "outcomes need ADSB_AIRPORT_LAT/LON + ADSB_AIRPORT_ELEV_FT; without them they are null."
            if not total["n"]:
                out["note"] = "no passes recorded in this window yet."
            return _text(out)
        finally:
            conn.close()

    @mcp.tool(title="Aircraft history", annotations=_read_hints("Aircraft history"), structured_output=False)
    async def aircraft_history(query: str, limit: int = 20) -> list[Any]:
        """Recorded past PASSES matching an ICAO hex, callsign or registration (partial, case-insensitive),
        newest first, with a summary. Answers "have we seen this aircraft / flight before?", "when?", "how
        low did it come?", "did it land or go around?".

        Args:
            query: hex, callsign or registration fragment (e.g. "e80", "LAN1454", "CC-B").
            limit: passes returned (default 20, max 200).
        """
        q = query.strip().lower()
        if not q:
            return _refused("empty query", advice="pass a callsign, hex, or registration fragment")
        if not 1 <= limit <= 200:
            return _refused("limit out of range", advice="pass 1..200")
        conn = _store()
        if conn is None:
            return _not_recording()
        try:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where = " OR ".join(f"lower({c}) LIKE ? ESCAPE '\\'" for c in ("hex", "callsign", "reg"))
            args = (like, like, like)
            summary = conn.execute(f"SELECT COUNT(*) AS n, MIN(first_seen) AS f, MAX(last_seen) AS l "
                                   f"FROM passes WHERE {where}", args).fetchone()
            airframes = [{"hex": r["hex"], "registration": r["reg"], "type": r["type"], "passes": r["n"]}
                         for r in conn.execute(f"SELECT hex, MAX(reg) AS reg, MAX(type) AS type, COUNT(*) AS n "
                                               f"FROM passes WHERE {where} GROUP BY hex ORDER BY n DESC LIMIT 10", args)]
            passes = [_pass_row(r) for r in conn.execute(
                f"SELECT * FROM passes WHERE {where} ORDER BY first_seen DESC LIMIT ?", (*args, limit))]
            usual: dict[str, Any] = {}
            for p in passes:
                cs = p["callsign"]
                if cs and cs not in usual:
                    usual[cs] = usual_route(conn, cs, settings)
                u = usual.get(cs)
                if (u and p["route_source"] and u["seen"] >= RELIABLE_SEEN and u["seen"] / u["of"] >= RELIABLE_SHARE
                        and (p["origin"], p["destination"]) != (u["origin"], u["destination"])):
                    p["unusual_route"] = True
            return _text({"status": "ok", "source": "adsb-history", "query": query, "limit": limit, "tz": tz.key,
                          "seen_before": summary["n"] > 0, "total_passes": summary["n"],
                          "first_seen": _local(summary["f"]), "last_seen": _local(summary["l"]),
                          "airframes": airframes, "usual_routes": {k: v for k, v in usual.items() if v},
                          "passes": passes, "recorder": _recorder(conn)})
        finally:
            conn.close()

    return mcp


# --- streamable-http bearer gate -------------------------

import hmac  # noqa: E402

from .board import PATHS as BOARD_PATHS  # noqa: E402


class BearerAuth:
    """ASGI gate for streamable-http:
      * /healthz        — always open (Docker healthcheck).
      * /board, /board.json — the FIDS board (`board`), open when ADSB_BOARD=open (it's airport-referenced:
                          no site PII), else behind the bearer like everything else.
      * everything else — the MCP app, requiring `Authorization: Bearer <token>` when a token is set.
    """

    def __init__(self, app, settings: Settings, board=None) -> None:
        self.app = app
        self.settings = settings
        self.board = board
        self.expected = f"Bearer {settings.http_token}".encode() if settings.http_token else None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope["path"]
            if path == "/healthz":
                await _respond(send, 200, b"ok")
                return
            is_board = self.board is not None and path in BOARD_PATHS
            if is_board and self.board.public:
                await self.board(scope, receive, send)
                return
            if self.expected is not None:
                got = dict(scope["headers"]).get(b"authorization", b"")
                if not hmac.compare_digest(got, self.expected):
                    await _respond(send, 401, b"unauthorized")
                    return
            if is_board:
                await self.board(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _respond(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
