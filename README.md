# adsb-mcp

Live local **ADS-B flight data** as MCP tools, over your own receiver's `aircraft.json` (readsb / tar1090 /
dump1090) — **keyless, live, your own data**. Answers "what's flying overhead right now?" and, with the
optional recorder on, "have we seen this one before?" / "what flies here most?".

## Tools

| Tool | What it does |
|---|---|
| **aircraft_overhead** | Aircraft in range of the receiver, nearest first. Distances from the site (`ADSB_SITE_LAT/LON`), else from the airport (approximate); without either, no distances. |
| **aircraft_near** | Aircraft within a radius of a point you pass (no receiver location needed). |
| **find_aircraft** | Match by callsign / ICAO hex / registration (partial, case-insensitive). |
| **traffic_stats** | Recorded passes over a window, grouped by airframe, callsign, type, outcome or local hour; most or least common. |
| **aircraft_history** | Past passes of an airframe / callsign / registration: "seen before?", when, how low, landed or went around. |

Data is structured (hex, callsign, registration, type, altitude_ft, ground_speed_kt, track_deg, lat/lon,
distance_km + bearing_deg from the reference point, seen_s). The site's coordinates are never echoed. Every result has a `status`: `ok` / `refused`
(caller-fixable, with `advice`) / `error` (receiver unreachable). ADS-B carries no route; the optional
OpenSky enrichment below adds one.

## Run

```bash
uv sync                      # (--system-certs behind a TLS-inspecting proxy)
# stdio:
uv run adsb-mcp serve
# streamable-http (behind mcpo / on the LAN):
ADSB_MCP_TOKEN=... ADSB_URL=http://<feeder-host>:8080/data/aircraft.json \
  ADSB_SITE_LAT=... ADSB_SITE_LON=... ADSB_POLL_SECONDS=15 \
  uv run adsb-mcp serve --transport http --host 0.0.0.0 --port 8000
uv run pytest                # no network: the feeder is faked
```

## Config (env)

| Var | Default | Notes |
|---|---|---|
| `ADSB_URL` | `http://localhost:8080/data/aircraft.json` | Your feeder's live snapshot (readsb / tar1090 path). |
| `ADSB_SITE_LAT` / `ADSB_SITE_LON` | — | Receiver location: the exact reference point. **Home-location PII — from a secret store, never committed, never echoed in results.** |
| `ADSB_EXACT_LOCATION` | `false` | Off = approximate location: when distances are from the site, `aircraft_overhead` rounds positions (~1 km), distance (whole km) and bearing (10°). `true` = exact values. |
| `ADSB_AIRPORT_ICAO` / `ADSB_AIRPORT_LAT` / `ADSB_AIRPORT_LON` | — | The nearby airport (e.g. `EGLL`). An approximate, public reference point when the site isn't set. |
| `ADSB_AIRPORT_ELEV_FT` | — | Field elevation. With the airport position, turns on arrival / go-around / departure tracking. |
| `ADSB_AIRPORT_ALIASES` | — | Nearby airfields OpenSky reports instead of your airport (e.g. `SEAS,SEHI`), counted as the airport when reading routes. The store keeps OpenSky's codes as received. |
| `ADSB_MILITARY_PATTERNS` | — | Callsign / registration patterns marking military or state aircraft (e.g. `FAE*,AEE*,HIERRO*,ARPIA*`), on top of readsb's own military flag. Catches unregistered airframes on tactical callsigns. Applied when reading, so it covers history too. |
| `ADSB_AIRPORT_NAME` | — | Display name for the board header (e.g. `Heathrow`). |
| `ADSB_BOARD` | `open` | The flight board at `/board`: `open` (no bearer: it carries no site data), `token` (behind the bearer), `off`. |
| `ADSB_MCP_TOKEN` | — | Bearer required on streamable-http when set. |
| `ADSB_HTTP_TIMEOUT` | `10` | Seconds. |
| `ADSB_POLL_SECONDS` | `0` | `>0` turns on the recorder: poll the feeder this often and record passes. `0` = live tools only. 10–15 is a good value. |
| `ADSB_DB` | `data/adsb.db` | SQLite store of passes (`/data/adsb.db` in the image). |
| `ADSB_PASS_RADIUS_KM` | `50` | A pass = an airborne aircraft within this radius of the reference point, or on approach to the airport. |
| `ADSB_PASS_GRACE_SECONDS` | `90` | Close a pass after the aircraft has been gone, or out of the zone, this long. |
| `ADSB_PASS_MERGE_SECONDS` | `600` | An aircraft back within this long of its pass closing continues that pass, so a coverage gap doesn't split one visit in two. |
| `ADSB_ARRIVAL_GRACE_SECONDS` | `900` | An aircraft lost **on approach** stays open this long (it's presumably landing below coverage). |
| `ADSB_APPROACH_KM` | `30` | How far from the airport a descent counts as an approach. |
| `ADSB_OPENSKY_CLIENT_ID` / `ADSB_OPENSKY_CLIENT_SECRET` | — | OpenSky API client (OAuth2). Both set + the recorder on = route enrichment. **Secret: from the environment only.** |
| `ADSB_OPENSKY_DAILY_CREDITS` | `3000` | Most OpenSky credits the server spends per UTC day (a past-day lookup costs ~30). |
| `ADSB_OPENSKY_RESERVE` | `1000` | Stop for the day when OpenSky reports fewer credits left than this. |
| `ADSB_TZ` | `UTC` | IANA zone for local times and hour-of-day stats (e.g. `Europe/London`). |

## History: passes

With `ADSB_POLL_SECONDS>0` and a reference point (the site, else the airport), the server polls the feeder
in the background, whether or not anyone is asking, and records one row per **pass**: a visit, not a poll.
A ~6-minute overflight is 1 row, not ~24. Each pass keeps first / last seen, min / max altitude, closest
approach, max RSSI and the number of snapshots. The live tools reuse the recorder's latest snapshot while
it's fresh, so the feeder isn't hit twice.

With the airport elevation set, each pass also gets an **outcome**: `arrival` (seen landing),
`arrival_presumed` (lost on approach: coverage usually ends before the runway, so the pass is held open
`ADSB_ARRIVAL_GRACE_SECONDS`), `go_around`, `departure` or `overflight`, plus a `go_arounds` count. The
heuristics are simple and documented in [`passes.py`](src/adsb_mcp/passes.py).

A pass that **circles** — heading changes of 360° or more while staying within about 15 km — is recorded
as `loitering` (patrols, training, survey flights), and its climbs and descents don't count as go-arounds.
Every pass also carries its ADS-B category (`rotorcraft: true` for helicopters) and a `military` flag
(readsb's database flag, or a match in `ADSB_MILITARY_PATTERNS`). `traffic_stats` can group by `category`
and narrow to `only="military"` or `only="rotorcraft"`.

A failed poll never closes a pass, and open passes are written through, so a restart resumes them. An
aircraft that drops out of coverage briefly (a hold, a turn behind terrain) continues its pass when it
comes back within `ADSB_PASS_MERGE_SECONDS`; a confirmed landing always ends the pass. With
neither a site nor an airport, passes can't be tracked and the recorder stays off.

### Claude Desktop / Claude Code (stdio)

```json
{
  "mcpServers": {
    "adsb": {
      "command": "uv",
      "args": ["--directory", "/path/to/adsb-mcp", "run", "adsb-mcp", "serve"],
      "env": { "ADSB_URL": "http://<feeder-host>:8080/data/aircraft.json" }
    }
  }
}
```

## Routes from OpenSky (optional)

ADS-B doesn't say where a flight is going. If you feed the [OpenSky Network](https://opensky-network.org),
your account gets API access (an active feeder gets 8,000 credits a day). Create an API client in your
OpenSky account and set `ADSB_OPENSKY_CLIENT_ID` / `ADSB_OPENSKY_CLIENT_SECRET`. A background job then adds
origin and destination airports to recorded passes.

It's built to be a good citizen with access you earned by feeding:
- It only looks up aircraft your own receiver saw.
- OpenSky processes flights overnight, so it enriches **yesterday's** passes, after 04:00 UTC.
- At most **one call per airframe per day**, however many times it passed.
- It stops for the day at `ADSB_OPENSKY_DAILY_CREDITS`, when OpenSky reports fewer than
  `ADSB_OPENSKY_RESERVE` credits left, or when OpenSky says to slow down (HTTP 429).

What you get: past passes show `origin` / `destination`, `traffic_stats` can group by `route`, and live
aircraft carry a `usual_route` — where that callsign has usually flown (e.g. 4 of its last 5 passes to
SPJC), so the assistant can say "usually flies to Lima". A past pass that broke from its usual route is
flagged `unusual_route` (a diversion, an unscheduled landing). The board shows usual routes too.

OpenSky estimates each airport from the last position its network saw, so when coverage ends before the
runway it can name a nearby airfield instead of yours. List those in `ADSB_AIRPORT_ALIASES`. Each pass's
`route_lookup` says whether it was `matched`, looked up with `no_match` (OpenSky had no overlapping
flight), or is still `pending`; `recorder.enrichment.last_7_days` counts each.

With enrichment on, the **server contacts OpenSky**; the MCP tools themselves still only read the local
store. Using OpenSky data is subject to [OpenSky's terms](https://opensky-network.org/about/terms-of-use).

## Flight board

An airport-style arrivals board (FIDS) for an old phone or tablet on the wall, or a Home Assistant card.
With the airport set and streamable-HTTP running, open `http://<server>:8000/board`.

- **Arrivals:** aircraft on approach (with ETA), ones lost on approach ("Landing"), and ones seen landing.
- **Departures:** aircraft climbing out.
- **Overhead:** everything within `ADSB_PASS_RADIUS_KM` of the airport, nearest first; emergency squawks
  (7500 / 7600 / 7700) flash red.
- **Recent:** passes that ended in the last 3 hours, with their outcome (needs the recorder).

The page is one self-contained file: no build, no dependencies, no external fonts. It polls
`/board.json` every 8 s and renders in the browser, so many displays cost the server almost nothing
(they share one snapshot for at least 5 s). It reflows for landscape, portrait and small embeds, dropping
less important columns as space shrinks. Options: `?lang=es`, `?interval=10`, `?sections=arr,ovh`.

Everything on the board is measured from the **airport**, a public place, and aircraft positions are left
out, so it carries nothing about the receiver's location. That's why it's served without the bearer by
default (a wall panel or an iframe can't send one); set `ADSB_BOARD=token` or `off` to change that.

**Full screen on a phone or tablet:** tap the *Full screen* button (Android Chrome, iPad), or use *Add to
Home Screen*. The board ships a manifest, so it then opens without browser bars; on iPhone that's the only
way. Android installs it as an app only over https, and keeping the screen awake (Wake Lock) also needs
https; over plain http, use the device's stay-awake setting or a kiosk browser app.

In Home Assistant, add a Webpage card with the `/board` URL. If the dashboard is served over https, serve
the board over https too, or the browser blocks the frame.

## Docker

```bash
docker build -t adsb-mcp .
docker run --rm -p 8000:8000 -e ADSB_MCP_TOKEN=... \
  -e ADSB_URL=http://<feeder-host>:8080/data/aircraft.json \
  -e ADSB_SITE_LAT=... -e ADSB_SITE_LON=... adsb-mcp
```

It needs to reach your feeder over the LAN. Inside the container `localhost` is the container itself, so
set `ADSB_URL` to the feeder's address. With the recorder on, mount `/data` on **local disk** (SQLite WAL
and network filesystems don't mix) and back it up: the history accumulates and can't be re-fetched.

```bash
docker run -d -p 8000:8000 -v /srv/adsb-mcp-data:/data -e ADSB_POLL_SECONDS=15 \
  -e ADSB_AIRPORT_ICAO=... -e ADSB_AIRPORT_LAT=... -e ADSB_AIRPORT_LON=... -e ADSB_AIRPORT_ELEV_FT=... \
  -e ADSB_URL=http://<feeder-host>:8080/data/aircraft.json -e ADSB_MCP_TOKEN=... adsb-mcp
```

## Auth and exposure

Auth is deliberately simple: stdio, or streamable-HTTP with a single bearer token (`ADSB_MCP_TOKEN`) on a
private network, for example behind [mcpo](https://github.com/open-webui/mcpo). `/healthz` stays open, and
so does the flight board (`/board`, `/board.json`) unless `ADSB_BOARD` says otherwise. There
is **no OAuth, no users and no audit log**, by design. To publish it more widely, put a management layer in
front of it: an MCP gateway or auth proxy that handles OAuth, users and audit for all your servers at once.
`ADSB_SITE_LAT/LON` is your home location. It is never echoed in a result, and the store holds distances,
not coordinates. An exact aircraft position next to an exact distance and bearing would give the site away
in a single result, so by default `aircraft_overhead` gives an **approximate location**, like choosing
"approximate" in a phone's location permission: still good for "about 12 km to the south". Set
`ADSB_EXACT_LOCATION=true` to get exact values. This makes the site hard to find, not impossible: someone
who collects many results can still narrow it down. If that matters, set only the airport, so results are
measured from a public place.

## AI assistance

adsb-mcp is developed openly with the help of Claude (Anthropic). Commits Claude helped write carry a
`Co-Authored-By: Claude` trailer. The code and design are open source so the work can be inspected, reused,
and given back.

## License

[MPL-2.0](LICENSE) © Daniel Núñez.
