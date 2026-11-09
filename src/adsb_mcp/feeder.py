"""Thin async client over a local ADS-B receiver's `aircraft.json` (readsb / tar1090 / dump1090 shape).

Fetches the live snapshot and normalizes it. `Snapshots` lets the live tools reuse the background
recorder's latest snapshot while it's fresh, so a busy chat doesn't add load on a Pi-class feeder. Field names follow readsb: `hex`, `flight`, `r`, `t`, `alt_baro`, `gs`, `track`, `baro_rate`,
`squawk`, `category`, `lat`, `lon`, `rssi`, `seen`, `seen_pos`."""

from __future__ import annotations

import math
import time
from typing import Any

import httpx

from .config import Settings


class FeederError(RuntimeError):
    """The feeder is unreachable or returned a bad body (a genuine fault, not a policy decision)."""


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def normalize(ac: dict[str, Any]) -> dict[str, Any]:
    """One aircraft record from aircraft.json -> a stable, friendly shape. Units are named in the keys."""
    alt_raw = ac.get("alt_baro")
    on_ground = alt_raw == "ground"
    altitude = 0 if on_ground else (int(alt_raw) if isinstance(alt_raw, (int, float)) else None)
    callsign = (ac.get("flight") or "").strip() or None
    return {
        "hex": (ac.get("hex") or "").strip().lower() or None,
        "callsign": callsign,
        "registration": ac.get("r"),
        "type": ac.get("t"),
        "category": ac.get("category"),
        # From readsb's database with --db-file-lt: friendly model name, owner / operator, year built.
        "description": ac.get("desc"),
        "operator": ac.get("ownOp"),
        "year": ac.get("year"),
        "altitude_ft": altitude,
        "on_ground": on_ground,
        "ground_speed_kt": _f(ac.get("gs")),
        "track_deg": _f(ac.get("track")),
        "vertical_rate_fpm": _f(ac.get("baro_rate")),
        "lat": _f(ac.get("lat")),
        "lon": _f(ac.get("lon")),
        "squawk": ac.get("squawk"),
        "emergency": ac.get("emergency") if ac.get("emergency") not in (None, "none") else None,
        "rssi_dbfs": _f(ac.get("rssi")),
        "seen_s": _f(ac.get("seen")),
        "seen_pos_s": _f(ac.get("seen_pos")),
        # readsb dbFlags bit 0 = military (from its aircraft database; misses unregistered airframes).
        "military": bool(int(ac.get("dbFlags") or 0) & 1),
    }


class FeederClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._client = httpx.AsyncClient(timeout=settings.http_timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self) -> dict[str, Any]:
        """Return {"now": epoch, "aircraft": [normalized, ...]} from the live feeder snapshot."""
        try:
            r = await self._client.get(self.s.adsb_url)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            raise FeederError(f"feeder GET {self.s.adsb_url} failed (is the receiver up?): {e}") from e
        except ValueError as e:
            raise FeederError(f"feeder returned non-JSON from {self.s.adsb_url}: {e}") from e
        aircraft = [normalize(a) for a in (data.get("aircraft") or [])]
        return {"now": data.get("now"), "aircraft": aircraft}


class Snapshots:
    """The latest snapshot, shared by the recorder and the live tools.

    With the recorder on (`max_age_s` > 0) a tool call reuses the last snapshot if it's at most `max_age_s`
    old, else fetches. With it off every call fetches (the pure live tier). Callers get copies — the tools
    annotate aircraft dicts in place."""

    def __init__(self, feeder: FeederClient, max_age_s: float = 0) -> None:
        self.feeder = feeder
        self.max_age_s = max_age_s
        self._latest: dict[str, Any] | None = None
        self._at = 0.0

    async def refresh(self) -> dict[str, Any]:
        snap = await self.feeder.fetch()
        self._latest, self._at = snap, time.monotonic()
        return snap

    async def get(self, min_age_s: float = 0) -> tuple[dict[str, Any], float]:
        """Return (snapshot copy, age in seconds). `min_age_s` lets a caller that's polled hard (the board)
        share a snapshot for at least that long even when the recorder is off."""
        age = time.monotonic() - self._at
        max_age = max(self.max_age_s, min_age_s)
        if self._latest is None or max_age <= 0 or age > max_age:
            await self.refresh()
            age = 0.0
        snap = self._latest
        return {"now": snap["now"], "aircraft": [dict(a) for a in snap["aircraft"]]}, age
