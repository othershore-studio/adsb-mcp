# NEXT — adsb-mcp work queue / backlog

For the next Claude working in this repo. Read `CLAUDE.md` first (the *why*); this is the *what next*.
Follow `~/.claude/mcp-server-standard.md`. Deployment details (hosts, paths, secrets) live in the homelab
repo, never here.

## Done

- **Live tier** — `aircraft_overhead`, `aircraft_near`, `find_aircraft`. Validated against a real feeder.
- **Privacy** — `observer` (the exact site) removed from results; `reference: {"kind": ...}` instead.
- **Historical tier** — background recorder (`ADSB_POLL_SECONDS`, off by default), passes store (SQLite,
  write-through, restart-safe, outage-safe), airport reference fallback, arrival / presumed arrival /
  go-around / departure tracking, live tools reusing the recorder's snapshot, and two history tools:
  `traffic_stats`, `aircraft_history`.
- **Pass merge + approximate location** — coverage gaps reopen the pass; `ADSB_EXACT_LOCATION` opt-in.
- **Flight board** — `/board` + `/board.json` (see below).
- **OpenSky enrichment** — yesterday's passes get origin/destination (one call per airframe-day, budgeted);
  `usual_route` on live aircraft and the board, `unusual_route` on history, `traffic_stats(group_by="route")`.

## To validate on real data

- Run the recorder for a few days and check the arrival heuristics against what actually happened
  (thresholds are constants at the top of `passes.py`). Watch for: arrivals counted as `overflight`
  (approach not detected — descent threshold too strict?), `arrival_presumed` that were really
  overflights descending elsewhere, and go-arounds.
- Baro altitude is pressure altitude, not QNH: at a high airport the AGL estimate can be off by a few
  hundred feet. Landing is confirmed by `on_ground`, not altitude, for that reason.

### First week, measured (2026-10-23)

1,104 passes from 276 airframes in 6 days; peaks at 08:00 and 17–18:00 local. Outcomes: 448
`arrival_presumed`, 332 `overflight`, 323 `departure`, **0 confirmed `arrival` and 0 go-arounds** — the
receiver never sees an aircraft on the ground at SEQM, so `arrival_presumed` IS the arrival count here.
Enrichment: ~330 credits/day used, remaining reported ~4,900 — far under the cap, so the "fewer calls" idea
isn't needed at this volume yet. Roughly 40 % of eligible passes got at least one airport.

Fixed from this data: the departure rule (same daily flight split 3 departure / 4 overflight), airport
aliases (SEHI / SEAS), and distinct lookup states. Still open:
- **Correct history with enrichment**: `overflight` passes whose (aliased) origin is the airport were
  departures; destination = airport → arrival. A read-time `outcome_enriched` would fix the first week too
  (the widened rule only helps new passes). Measure after the new rule has run a week: how many remain.
- **Many routes have one end missing** (`SEQM-?` 92 passes): OpenSky didn't see the other end. Nothing to
  fix on our side; maybe show only the known end on the board (it does).
- **LNE1440 SEQM→SEQM** (21 Oct): a return to field, or a missing leg? Worth a look if it repeats.
- **N355GB** (BE30, 18 passes, always overflight): a local survey / calibration aircraft — a candidate for a
  "local / loitering" outcome if more show up.
- **One-off callsigns are the route gap**: 100+ callsigns were seen only once in the week — mostly cargo
  (ABX / LCO / LTG / GTI / LAE B763 / B744, a new flight number each trip) and GLG 16xx numbers. A
  per-callsign `usual_route` can never cover them.

### Military / state aircraft and helicopters (2026-10-25)

Seen 24–25 Oct, apparently practice for an event on **27 Oct** (expect more of it that day — a good
dataset to check the items below against):
- **FAE (Ecuadorian Air Force) helicopters fly tactical callsigns**, not FAE-xxx: ARPIA (two at once, both
  squawk 7377: `e847fc` = FAE-1254 H145, and `e847f9` — unregistered, almost certainly another FAE H145 by
  its address next to FAE's block `e847fc`–`e847ff`), HIERRO21 (FAE-1255 H145), HIERRO22 (`e847f9`).
  Long low loiters circling over the city centre (up to 43 min, ~9,000–9,500 ft pressure alt).
- Other FAE: FAE001 = FAE052 Falcon 7X (VIP), FAE-640 B735, FAE1103 Grob G 120TP (85-min training session
  overhead). AEE472 / AEE473 (`e84946` / `e84947`, unregistered, low) — likely Army aviation. PNE-220
  King Air 350 (likely national police). US military C-146A (Do 328, serial 11-3016, `ae68b5`, no callsign)
  SKBO → UIO 24 Oct, out 25 Oct.
- **Bug found:** the G 120TP's training was recorded as `go_around` with 4 go-arounds — the go-around rule
  fires on an aircraft working an area.

**Built 2026-10-25, ahead of the 27th** (items 1–3 below): readsb's military flag + `ADSB_MILITARY_PATTERNS`,
category in history + `traffic_stats(group_by="category", only="military"|"rotorcraft")`, and `loitering`
from accumulated heading change (stops the go-around misfire). After the 27th: check how many passes were
flagged, whether ARPIA / HIERRO's patrols came out `loitering`, and whether any real arrival with a holding
pattern was misread (`arrival_presumed` wins over `loitering`, so it shouldn't be).

To build (in this order; check against the 27 Oct data):
1. **Military / state flag** per pass: readsb's `dbFlags` military bit (not captured today) **plus**
   `ADSB_MILITARY_PATTERNS` (e.g. `FAE*,AEE*,HIERRO*,ARPIA*`) matched on callsign / registration — the
   patterns are what catch unregistered airframes on tactical callsigns. Expose in history; filterable.
2. **Category in history**: `category` (A7 = rotorcraft) is already stored per pass but not shown or
   groupable — add it to results and `traffic_stats(group_by="category")`, so "helicopters today" is one
   call. (Military airframes often have no `type`, so category is the reliable heli signal.)
3. **Loiter / circling detection**: accumulate absolute track change during a pass; ≥ 360° while staying
   within a small area → outcome `loitering` (patrols, training, survey flights like N355GB). Loitering
   passes must NOT feed the go-around rule. Needs a test from the G 120TP shape.

### Route coverage: plan (Daniel, 2026-10-23)

1. **Airframe fallback (local, free — build first):** when a live callsign has no usual route, use the
   *airframe's* usual route (`hex` → most common enriched route), labelled as such (`basis: "airframe"`).
   Cargo shuttles fly the same pairs under changing flight numbers.
2. **Feed adsb.lol** (Daniel, on the feeder — not this repo). Little or no Ecuador coverage today, so the
   feed helps them; and if their network covers the far-end airports (international origins), it may fill
   the `SEQM-?` / `?-SEHI` gaps. Their API is announced to require a key earned by feeding — fits the
   "earned" principle.
3. **Then measure** (1–2 weeks): share of live aircraft with a route from callsign / airframe / none, and
   of passes with one end missing. If "none" is still large, **evaluate adsb.lol's route lookup** for that
   remainder — it's hobbyist-run, so check before building: the actual endpoint and its plausibility check,
   the **licence of the route data itself** (may differ from the ODbL positions), accuracy on cargo /
   charter / multi-leg (LNE1423) against OpenSky's matched legs, and rate limits. Any tool that calls it
   live would be `openWorldHint=true`; a background job writing to the store would not change the hints.

### Field notes (Daniel, 2026-10-18 — after the first hours of the board)

- **Missed takeoffs look like overflights.** Terrain hides some departures; the receiver first sees them
  overhead as they turn south, so the outcome is `overflight`. Enrichment can correct this after the fact:
  if OpenSky's origin is our airport, it was a departure (and destination = our airport → an arrival).
  Idea: keep `outcome` as *observed* and add an `outcome_enriched` (or similar) set in `enrich._settle` —
  measure first how many overflights have origin/destination = `ADSB_AIRPORT_ICAO`.
- **Multi-leg flight numbers** (e.g. LNE1423: UIO→GYE, then GYE→Galápagos). OpenSky's `/flights/aircraft`
  reports each takeoff-to-landing **leg** as its own flight, and we match by time overlap, so a pass gets
  the leg it actually saw (UIO→GYE). `usual_route` per callsign is then "the leg seen here" — right for
  "where is it going now", but not the flight number's final destination. Check on real data that legs
  come back separately.
- **One airframe, several callsigns a day** (e.g. flights in Peru, then Ecuador, then elsewhere). Handled
  by design: passes are per airframe with their own callsign, and one OpenSky call per airframe-day
  returns all its legs, matched pass by pass. `aircraft_history` by hex shows the rotation.
- **Learning compounds**: ~80 flights/day here; the longer it runs, the better `usual_route` gets.
- **Board in Home Assistant works only on the LAN**: the iframe makes the *viewer's browser* fetch the board,
  and away from home it can't reach the LAN host (HA's own remote access only tunnels HA). Options:
  expose `/board` over https through the same proxy/tunnel as HA (it's PII-free and open, but it would then
  be a public view of the traffic around the airport), or let HA fetch `/board.json` itself with a RESTful
  sensor — then it works remotely through HA, and enables automations (747 on final, squawk 7700). If the
  sensor route is taken, consider a compact `/board/summary.json` sized for HA state attributes.

## Backlog

- **Dedicated analytics tools** (if callers benefit — `traffic_stats` covers them today):
  `most_common_flights`, `seen_before`, `busiest_times`, `rarest_visitors`, `aircraft_stats`.
- **Operator / owner** — OpenSky publishes an aircraft database as a downloadable file (hex → operator,
  owner, model). Importing it periodically would name the airline with ZERO API calls. Check it's still
  offered and its licence first.
- **Validate enrichment on real data** — field names (`estDepartureAirport` / `estArrivalAirport` /
  `firstSeen` / `lastSeen`) come from OpenSky's Python client; confirm against real responses, and check
  how often airports come back null around a high-altitude airport with sparse coverage.
- **Fewer OpenSky calls as history grows** (Daniel's idea; check after a few weeks of data) — skip the
  lookup for a callsign whose usual route is already reliable (e.g. ≥ 5 passes, ≥ 90 % one route) and only
  sample it now and then (say weekly) so changes and diversions are still caught. Measure first: credits
  per day vs. how many lookups just confirmed the usual route.
- **Live route hint on the board** is the callsign's usual route; a real-time source would need a
  different (earned) API — FlightAware AeroAPI for feeders is worth a look, FR24's new API is restrictive.
- **Retention / backup tooling** — the store accumulates; a `backup` subcommand (SQLite online backup) may
  be worth it. If you add one-shot subcommands run via `docker run --rm`, bind `/data` explicitly (the
  image declares `VOLUME /data`; an unbound `--rm` run writes to a throwaway anonymous volume).

## The ambient FIDS board — `board.json` + `board.html` (the "something interesting"; Daniel, 2026-10-17)

**Built** (2026-10-18): `/board` + `/board.json` as below, `ADSB_BOARD=open|token|off`, `ADSB_AIRPORT_NAME`,
`?lang=es`, `?interval=`, `?sections=`. Arrivals include held (`arrival_presumed`) and `landed` rows from the
store. Still open: the HA RESTful-sensor idea under *Later*, and checking it on the real wall device.

An **airport arrivals / FIDS board**: an ambient display of what's overhead and arriving, to run on an old
phone/tablet on a wall **and** embedded in Home Assistant. Minimal, **client-side render**, one data source.

**Two tiny additions, both served by the existing ASGI app (alongside `/healthz`):**

1. **`GET /board.json`** — a read-only snapshot for a display, **airport-referenced** (the public reference
   point), so it carries **no site/home PII** and can stay **open** (no bearer), like `/healthz`. Built from
   the live snapshot classified by the pass/airport logic. Shape (draft):
   - `now`, `airport: {icao, name?}`
   - `overhead`: in range now, nearest first — `{hex, callsign, type, registration, altitude_ft,
     ground_speed_kt, track_deg, distance_km, bearing_deg, vertical_rate_fpm, category}`
   - `arrivals`: on approach — `{callsign, type, distance_km, altitude_ft, eta_min?, status:
     approaching|arrival_presumed}`
   - `departures`: climbing out — `{callsign, type, altitude_ft}`
   - `recent`: last few **closed** passes (just landed / departed / overflew) — the board's "just happened" row.

2. **`GET /board` (board.html)** — one **self-contained** HTML file (HTML+CSS+JS, **no build, no deps**) that
   polls `/board.json` every ~5–10 s and renders a split-flap / FIDS board **client-side** (the old device
   does the work; the server only serves JSON + the static file). **Responsive by requirement:** works
   **landscape** (wall), **portrait** (phone), and a **small iframe** (HA card) — CSS grid/flex that reflows,
   font scales, drop low-priority columns at narrow widths. One file, one breakpoint set.

**That completes it — one `board.json`, one `board.html`, two homes:**
- **Standalone:** point an old phone/tablet at `…/board` → full-screen FIDS board (the Kurena wall-panel idea
  in the homelab `someday/todo.md`).
- **In HA:** an `iframe`/webpage Lovelace card embedding the same `…/board` URL — it fits because board.html is
  responsive. (If the HA dashboard is https, serve `/board` via an https name so the browser doesn't block an
  http iframe.)

**Design rules:** airport-referenced (keeps `/board.json` PII-free + openable) · client-side render (scales to
N displays at ~zero server cost) · no front-end build or deps (sovereign, runs on anything with a browser).
*Later:* the same `/board.json` can also feed HA **entities** via a RESTful sensor — for the proactive triggers
(747-on-final push, squawk-watch) that need aircraft as entities, not a picture.
