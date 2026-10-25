"""Runtime settings, all from environment variables.

No API key anywhere — the source is your OWN ADS-B receiver's `aircraft.json` on the LAN (keyless). The
only real secret posture here is the **receiver's coordinates**, which are home-location PII: they come
from the environment (sourced from a secret store at deploy), never hardcoded in the repo. Leaving them
unset is fine — distance/overhead degrade gracefully, and a nearby airport can stand in as an approximate
(public, non-PII) reference point."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# The feeder's live aircraft.json. adsb.im / tar1090 / readsb serve it at this path by default; set
# ADSB_URL to point at your receiver (deployment-specific hosts live in the deploy config, not here).
DEFAULT_ADSB_URL = "http://localhost:8080/data/aircraft.json"


def _opt_float(name: str) -> float | None:
    v = os.environ.get(name)
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _float(name: str, default: float) -> float:
    v = _opt_float(name)
    return default if v is None else v


@dataclass(frozen=True)
class Settings:
    adsb_url: str = field(default_factory=lambda: os.environ.get("ADSB_URL", DEFAULT_ADSB_URL))
    http_timeout: float = field(default_factory=lambda: _float("ADSB_HTTP_TIMEOUT", 10))
    # Receiver location — HOME-LOCATION PII. Set via env (from a secret store) to enable nearest-first
    # "overhead" with real distances/bearings. Never echoed in tool results.
    site_lat: float | None = field(default_factory=lambda: _opt_float("ADSB_SITE_LAT"))
    site_lon: float | None = field(default_factory=lambda: _opt_float("ADSB_SITE_LON"))
    # Off (default) = "approximate location", like a phone's location permission: when distances are measured
    # from the site, aircraft_overhead rounds positions / distance / bearing so one result doesn't give the
    # site away. On = exact values. (Many intercepted results could still narrow it down — this makes it
    # hard, not impossible.)
    exact_location: bool = field(default_factory=lambda: os.environ.get("ADSB_EXACT_LOCATION", "").strip().lower()
                                 in ("1", "true", "yes", "on"))
    # The nearby airport: an approximate, public reference point when the site isn't given, and the target
    # for arrival / landing / go-around tracking (which also needs the field elevation).
    airport_icao: str | None = field(default_factory=lambda: (os.environ.get("ADSB_AIRPORT_ICAO") or "").strip().upper() or None)
    airport_lat: float | None = field(default_factory=lambda: _opt_float("ADSB_AIRPORT_LAT"))
    airport_lon: float | None = field(default_factory=lambda: _opt_float("ADSB_AIRPORT_LON"))
    airport_elev_ft: float | None = field(default_factory=lambda: _opt_float("ADSB_AIRPORT_ELEV_FT"))
    # Nearby airfields OpenSky reports in place of the airport (it names the aerodrome nearest the last
    # position it saw): normalized to ADSB_AIRPORT_ICAO when READ; the store keeps OpenSky's value verbatim.
    airport_aliases: tuple[str, ...] = field(default_factory=lambda: tuple(
        c for c in (x.strip().upper() for x in (os.environ.get("ADSB_AIRPORT_ALIASES") or "").split(","))
        if c.isalnum() and 3 <= len(c) <= 4))
    # Callsign / registration patterns that mark military / state aircraft (e.g. "FAE*,AEE*,HIERRO*,ARPIA*"):
    # catches unregistered airframes flying tactical callsigns, which readsb's database flag can't. Applied
    # when READING (like the airport aliases), so changing the list re-reads history.
    military_patterns: tuple[str, ...] = field(default_factory=lambda: tuple(
        p for p in (x.strip().upper() for x in (os.environ.get("ADSB_MILITARY_PATTERNS") or "").split(","))
        if p and re.fullmatch(r"[A-Z0-9*?\-]+", p)))
    airport_name: str | None = field(default_factory=lambda: (os.environ.get("ADSB_AIRPORT_NAME") or "").strip() or None)
    # The FIDS board (/board, /board.json; streamable-http only). Airport-referenced, so no site PII:
    # open = no bearer needed (wall panels / HA iframes can't send one), token = behind the bearer, off.
    board: str = field(default_factory=lambda: (os.environ.get("ADSB_BOARD") or "open").strip().lower())

    # --- historical tier (the recorder) ---
    # >0 makes `serve` poll the feeder in the background and record passes. 0 = off (pure live tier).
    poll_seconds: float = field(default_factory=lambda: _float("ADSB_POLL_SECONDS", 0))
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("ADSB_DB", "data/adsb.db")))
    # A pass = an airborne aircraft within this radius of the reference point (or on approach to the airport).
    pass_radius_km: float = field(default_factory=lambda: _float("ADSB_PASS_RADIUS_KM", 50))
    # Close a pass once the aircraft has been gone (or out of the zone) this long.
    pass_grace_s: float = field(default_factory=lambda: _float("ADSB_PASS_GRACE_SECONDS", 90))
    # An aircraft back within this long of its pass closing continues that pass (coverage gaps don't split).
    pass_merge_s: float = field(default_factory=lambda: _float("ADSB_PASS_MERGE_SECONDS", 600))
    # ...but an aircraft lost while on approach is kept open this long: it's presumably landing below coverage.
    arrival_grace_s: float = field(default_factory=lambda: _float("ADSB_ARRIVAL_GRACE_SECONDS", 900))
    approach_km: float = field(default_factory=lambda: _float("ADSB_APPROACH_KM", 30))
    # IANA zone for hour-of-day stats and the local times in history results.
    tz: str = field(default_factory=lambda: os.environ.get("ADSB_TZ") or "UTC")

    # --- enrichment (optional, keyed): OpenSky routes for recorded passes ---
    # OAuth2 client credentials from your OpenSky account (a secret: from the environment only). Both set +
    # the recorder on = a background job adds origin/destination to YESTERDAY's passes (OpenSky's flight
    # data is batch-processed overnight): at most one call per airframe per UTC day.
    opensky_client_id: str | None = field(default_factory=lambda: os.environ.get("ADSB_OPENSKY_CLIENT_ID") or None)
    opensky_client_secret: str | None = field(default_factory=lambda: os.environ.get("ADSB_OPENSKY_CLIENT_SECRET") or None)
    # Good-citizen budget: credits this server may spend per UTC day (a past-day /flights call costs ~30)...
    opensky_daily_credits: int = field(default_factory=lambda: int(_float("ADSB_OPENSKY_DAILY_CREDITS", 3000)))
    # ...and stop for the day when OpenSky says fewer than this remain (leaves room for your other use).
    opensky_reserve: int = field(default_factory=lambda: int(_float("ADSB_OPENSKY_RESERVE", 1000)))

    # Bearer token required on streamable-http when set (sent as an Authorization header).
    http_token: str | None = field(default_factory=lambda: os.environ.get("ADSB_MCP_TOKEN") or None)

    @property
    def site(self) -> tuple[float, float] | None:
        return (self.site_lat, self.site_lon) if self.site_lat is not None and self.site_lon is not None else None

    @property
    def airport(self) -> tuple[float, float] | None:
        return (self.airport_lat, self.airport_lon) if self.airport_lat is not None and self.airport_lon is not None else None

    @property
    def reference(self) -> tuple[str, tuple[float, float]] | None:
        """The point distances and passes are measured from: the exact site, else the airport, else none."""
        if self.site:
            return "site", self.site
        if self.airport:
            return "airport", self.airport
        return None

    def military_match(self, callsign: str | None, registration: str | None) -> bool:
        return any(fnmatch.fnmatchcase((v or "").upper(), p)
                   for p in self.military_patterns for v in (callsign, registration) if v)

    @property
    def enrichment_enabled(self) -> bool:
        return bool(self.opensky_client_id and self.opensky_client_secret)

    @property
    def arrivals_enabled(self) -> bool:
        return self.airport is not None and self.airport_elev_ft is not None
