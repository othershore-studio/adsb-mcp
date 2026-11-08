# CLAUDE.md — adsb-mcp

Context for Claude Code sessions in this repo. Read `~/.claude/mcp-server-standard.md` first — this server
follows it; `C:\Code\forge-mcp` is the reference implementation and `C:\Code\igepn-mcp` is the closest twin
(a local structured source → FastMCP → mcpo → OWUI, with an accumulating SQLite store). This file records
the *why*.

> **▶ Current work queue / backlog: [`NEXT.md`](NEXT.md).** Read it before starting.

## What this is

A thin MCP server over a **local ADS-B receiver's `aircraft.json`** (readsb / tar1090 / dump1090 shape —
keyless, live, your OWN data). Two tiers:
- **Live** — `aircraft_overhead`, `aircraft_near`, `find_aircraft`: what's in the air right now.
- **History** (optional recorder) — `traffic_stats`, `aircraft_history`: one row per *pass*, kept over time.
- **Board** — `GET /board` (one self-contained HTML page) + `GET /board.json`: an ambient FIDS display for a
  wall panel or a Home Assistant iframe. Not an MCP tool; served by the same ASGI app beside `/healthz`.

`ADSB_URL` points at the feeder's `aircraft.json` (default `http://localhost:8080/data/aircraft.json`); the
real feeder host lives in the homelab repo's deploy config, never here (standard §6 — repo stays generic
and public). The star query: *"¿qué avión es ese?"*

## Deliberate choices (don't undo)

- **Keyless local source, with the grain.** It's your receiver; nothing to bypass, no API key, no scraping.
- **openWorldHint = FALSE** on all five tools (standard §1). Judged by behaviour: they talk only to the
  lab's **own LAN receiver** or the server's **own local store** — closed dependencies, not the public web
  or a third party. OpenSky enrichment doesn't change this: a **background job** writes routes into the
  store and the tools only read it. A tool that called OpenSky itself WOULD be `openWorld=true`.
- **Enrichment is earned and good-citizen** (Daniel's principle): the OpenSky access comes from feeding
  (active feeder = 8,000 credits/day), and it's spent only on aircraft our own receiver saw. OpenSky's
  `/flights/aircraft` is batch-processed overnight, so the job enriches **yesterday's** passes (after
  04:00 UTC), **one call per airframe per UTC day** (`opensky_lookups`), retries an empty answer at most 3×
  (batch lag), and stops for the day at the credit cap, at the reserve read from `X-Rate-Limit-Remaining`,
  or on 429's retry-after. Off unless `ADSB_OPENSKY_CLIENT_ID/SECRET` are set (OAuth2 client credentials —
  OpenSky no longer accepts username/password). Keyless route lookups (adsbdb etc.) were rejected: not
  earned, and their route data has redistribution limits. Note: the `/flights/*` bucket only shows the
  feeder boost after ~50 calls, so `X-Rate-Limit-Remaining` may read against 4,000 at first — fine, it
  only makes the reserve more conservative.
- **The value is learned**, not live: `usual_route` (most common enriched route of a callsign, with
  `seen`/`of`) lets the live tools and the board say "usually to SPJC" for a flight in progress, and
  `unusual_route` flags a past pass that differed (diversion / unscheduled landing — needs ≥ 3 enriched
  passes, ≥ 60 % share). The board shows a usual route only when it's consistent (≥ 2, ≥ 60 %).
- **OpenSky airport codes are stored verbatim; aliases apply on read** (standard §5). Measured in the first
  week: OpenSky names the aerodrome nearest the last position *it* saw, so Quito arrivals came back as SEHI
  (Cotacachi) and departures as SEAS (Ascázubi). `ADSB_AIRPORT_ALIASES` maps such codes to the airport in
  `enrich.airport` / `airport_sql` (routes, usual_route, stats, board); results show `*_reported` with
  OpenSky's own code when it differs. Changing the alias list re-reads history — nothing to migrate.
- **The OpenSky query spans the previous UTC day too** (`WINDOW_BEFORE_S`): 2 partitions still cost 30
  credits. Measured: evening arrivals here land after 00:00 UTC but OpenSky files the flight under its
  departure day, so a one-day query returned nothing (AAL833 MIA→UIO: `no_match` every night).
- **Matching tolerates a wide gap only for the same callsign.** Any OpenSky flight within 15 min of the
  pass qualifies; one with the SAME callsign (same airframe, by construction) may be up to 3 h away —
  measured: OpenSky first sees domestic departures 30+ min after takeoff, far from our receiver (LNE1397 /
  HC-CTX `no_match` daily). Nearest leg wins, so a round trip under one callsign gets the right one.
  `route_gap_s` keeps the gap (or the nearest flight's, on no_match) so this stays measurable.
- **Outcomes are corrected on read, never rewritten**: an observed `overflight` whose (aliased) OpenSky
  route began / ended at the airport reads as `departure` / `arrival_presumed` (`outcome_corrected`,
  `outcome_basis`; `traffic_stats(group_by="outcome")` counts the corrected value). The stored `outcome` is
  what the receiver saw.
- **Lookup state is explicit**: `route_lookup` = matched | no_match | pending, and `last_7_days` counts,
  so "OpenSky had nothing" never looks like "not asked yet".
- **Receiver location is home-location PII.** `ADSB_SITE_LAT/LON` come from the environment (secret store at
  deploy), **never** hardcoded, **never echoed** in a result (the old `observer` field was removed for this),
  and **never stored** (the DB holds distances, not coordinates). Results say `reference: {"kind": "site"}`.
- **Approximate location by default** (`ADSB_EXACT_LOCATION` off), Daniel's call, like a phone's "approximate
  location" permission: when measuring from the site, `aircraft_overhead` rounds aircraft lat/lon to 2
  decimals, distance to whole km and bearing to 10°, because an exact position plus an exact distance and
  bearing locates the site from ONE result. The operator opts in to exact. It makes the site hard, not
  impossible — many collected results could still narrow it down; that's accepted. Airport-referenced
  results stay exact (public place). `find_aircraft` / `aircraft_near` return exact positions but no
  site-relative numbers.
- **The airport is the fallback reference point.** Someone who won't give an exact site can set the nearby
  airport (`ADSB_AIRPORT_*`, public — fine to echo its ICAO): distances and passes are measured from it.
  With neither site nor airport, the live tools still work (no distances) but **passes can't be tracked**,
  so the recorder stays off.
- **The recorder is off by default** (`ADSB_POLL_SECONDS=0`, like igepn's `IGEPN_POLL_MINUTES`): a public repo
  shouldn't start writing a DB just because someone ran it. The deploy turns it on (10–15 s). It's
  in-process and runs regardless of tool traffic, so capture doesn't depend on anyone asking.
- **The store is a deliberate exception to "stateless"** (standard §5), the same as igepn's catalog: it's
  the server's own observations of your own receiver, not a copy of the caller's state. It ACCUMULATES and
  is not reproducible — back it up; local disk only (WAL + NFS don't mix).
- **One row per PASS, not per poll.** A pass opens when an airborne aircraft is inside the zone (within
  `ADSB_PASS_RADIUS_KM` of the reference, or on approach to the airport); while open, every snapshot it
  appears in updates it, so the exit is captured; it closes after `ADSB_PASS_GRACE_SECONDS` gone or out of
  the zone. An aircraft back within `ADSB_PASS_MERGE_SECONDS` (600) **reopens** its last pass — seen live:
  an A320 descending around the city lost coverage for ~170 s and was split into an "overflight" plus a new
  pass. A confirmed landing is never reopened (the next flight is new). Grounded aircraft never open a pass (a parked aircraft with its transponder on would otherwise
  be one endless pass).
- **Arrivals are tracked past the end of coverage.** A receiver usually loses arrivals
  before the runway. An aircraft lost **on approach** is held open `ADSB_ARRIVAL_GRACE_SECONDS` and closes
  as `arrival_presumed`; seen on the ground → `arrival`; go-arounds are counted. Heuristics are in the
  `passes.py` docstring — simple on purpose; tune the constants there, with a test, if real data disagrees.
  Needs the airport position **and** elevation (`ADSB_AIRPORT_ELEV_FT`); otherwise outcome is null.
- **A failed poll is not absence.** `poll_once` touches no pass when the fetch fails, so a feeder outage
  never closes or splits passes. Open passes are **written through** every tick (`open=1`), so a restart
  resumes them.
- **Timestamps come from the feeder's clock** (`now` minus each aircraft's `seen`), not the server's: a
  slow fetch doesn't skew them. (Clocks are synced in the lab anyway.)
- **The live tools reuse the recorder's snapshot** while it's ≤ 2 polls old (`Snapshots`): the point is to
  load the Pi less and do the work on the Docker host. Tools get copies (they annotate dicts in place).
- **Two history tools, not five.** Small local models pick tools from docstrings; `traffic_stats(group_by,
  order)` covers most-common / rarest / busiest-hours / outcomes, and `aircraft_history` covers
  seen-before. Dedicated tools are in the NEXT.md backlog if a caller (e.g. Claude, a trusted external
  model here) would benefit.
- **History tools when not recording → `refused`** with advice (a tier not enabled, not a fault). A stale
  recorder is flagged in every history result (`recorder.warning`) — never a silently thin answer.
- **The board is airport-referenced so it can be open.** `/board.json` measures everything from the public
  airport, omits aircraft positions and has nothing site-relative, so it carries no home-location PII and
  is served WITHOUT the bearer by default (`ADSB_BOARD=open`): wall panels and HA iframes can't send
  headers. `token` puts it behind the bearer, `off` removes it. If you add a field to the board, keep it
  free of site-relative data or this choice no longer holds (a test checks for lat/lon and the site).
- **Board = client-side render, no build, no deps.** The server only answers JSON + one static file
  (`board.html`, packaged with the module); displays share one snapshot for ≥ 5 s (`Snapshots.get(
  min_age_s)`), so N panels cost ~nothing. Responsive via container queries (each panel drops columns by
  its own width) with viewport media queries as the fallback for old tablets. Its arrivals / departures use
  the recorder's heuristics (`passes.airport_frame` / `on_approach` / `climbing_out`) — one definition.
- **Military / state aircraft = readsb's flag OR operator patterns, applied on read.** readsb's `dbFlags`
  military bit only knows registered airframes; Ecuador's FAE flies unregistered helicopters on tactical
  callsigns (ARPIA, HIERRO22 = `e847f9`), so `ADSB_MILITARY_PATTERNS` (validated `[A-Z0-9*?-]`, inlined as
  SQL GLOB) catches them. The flag is stored per pass (`military`); patterns are evaluated when reading,
  like the airport aliases, so a new pattern re-reads history. Results say `military_basis` (readsb |
  pattern).
- **Loitering is detected from heading change, without storing a track.** A pass accumulates
  `turn_deg` (sum of track changes ≥ 2°, ignored below 20 kt where a hovering helicopter's track is noise)
  and ranges `min_dist_km`..`max_dist_km` from the reference — distances only, no coordinates (the
  PII rule). ≥ 360° within 15 km — 40 km if the pass reached ≥ 150 kt (`max_gs_kt`; a 737's flypast laps
  over the city range far wider than a helicopter's) → `loitering`, which also stops the go-around rule (measured: a Grob
  G 120TP training session was recorded as 4 go-arounds; the test recreates the shape — 5 under the old rule).
- **All five: four hints explicit**, **ok/refused/error** contract (receiver unreachable = error, never a
  silent empty list).

## Layout

`src/adsb_mcp/`: `config.py` (env settings, `reference` = site → airport → none), `feeder.py` (fetch +
normalize aircraft.json, haversine/bearing, `Snapshots` cache — the MockTransport target), `passes.py`
(`Tracker`: snapshots → passes + arrival heuristics; `poll_once`), `store.py` (SQLite schema, WAL, meta),
`server.py` (tools + `BearerAuth`), `board.py` + `board.html` (the FIDS board), `enrich.py` (OpenSky client,
`enrich_once`, `usual_route`), `__init__.py` (`main()`, the recorder + enrichment loops). `store.py` also
migrates older files (`MIGRATIONS`: columns added after the first release).

## Tests

`uv run --system-certs pytest` — no network.
- `tests/test_tools.py` — live tools by name against a `FakeFeeder` (`httpx.MockTransport`): title + four
  hints on all five tools, nearest-first maths, site never echoed, airport fallback, radius filter, matching,
  no-reference graceful, feeder-down → error, snapshot reuse.
- `tests/test_passes.py` — the tracker on a synthetic feeder clock: one row per pass, parked aircraft,
  arrival / presumed / go-around (+ landing, + leaving) / departure, restart resume, outage never closes or
  splits, airport-only reference; the two history tools by name over the store it writes.
- `tests/test_board.py` — `/board.json` content (approach / departure / overhead / emergency, held arrivals
  and recent passes from the store, no positions or site), and open / token / off access through the real
  `BearerAuth` gate (`httpx.ASGITransport`).
- `tests/test_enrich.py` — against a fake OpenSky: one call per airframe-day + time matching, nothing before
  the nightly batch, empty → retry → give up, 429 pause, daily cap, reserve, token refresh, migration of a
  first-release store; usual / unusual route in the tools, `group_by=route`, the board.
