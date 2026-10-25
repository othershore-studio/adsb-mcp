"""The recorder: snapshots -> PASSES (one row per visit), arrival / go-around / departure outcomes, outage and
restart safety — and the two history tools called by name through the server over the store it writes.

No network: the tracker is fed hand-built snapshots on a synthetic feeder clock; `poll_once` runs against
an `httpx.MockTransport` feeder that can be switched off."""

import asyncio
import json
import math

import httpx
import pytest

from adsb_mcp import feeder as feeder_mod
from adsb_mcp.config import Settings
from adsb_mcp.feeder import FeederClient, Snapshots, normalize
from adsb_mcp.passes import Tracker, poll_once
from adsb_mcp.server import build_server
from adsb_mcp.store import connect

AP_LAT, AP_LON, AP_ELEV = -0.129, -78.358, 7900   # test-only airport (public place, not a site)


def east_of_airport(km: float) -> tuple[float, float]:
    return AP_LAT, AP_LON + km / (111.32 * math.cos(math.radians(AP_LAT)))


def ac(hx, km, alt, vr=0, flight="LNE1454", reg="CC-BHD", typ="A20N", seen=0.5):
    lat, lon = east_of_airport(km)
    return {"hex": hx, "flight": flight, "r": reg, "t": typ, "alt_baro": alt, "baro_rate": vr,
            "lat": lat, "lon": lon, "rssi": -10.0, "seen": seen, "seen_pos": seen}


def snap(now, *aircraft):
    return {"now": now, "aircraft": [normalize(a) for a in aircraft]}


@pytest.fixture
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ADSB_SITE_LAT", "-0.18")      # test-only "receiver", ~13 km from the airport
    monkeypatch.setenv("ADSB_SITE_LON", "-78.47")
    monkeypatch.setenv("ADSB_AIRPORT_ICAO", "SEQM")
    monkeypatch.setenv("ADSB_AIRPORT_LAT", str(AP_LAT))
    monkeypatch.setenv("ADSB_AIRPORT_LON", str(AP_LON))
    monkeypatch.setenv("ADSB_AIRPORT_ELEV_FT", str(AP_ELEV))
    monkeypatch.setenv("ADSB_DB", str(tmp_path / "adsb.db"))
    monkeypatch.setenv("ADSB_POLL_SECONDS", "15")
    return Settings()


def rows(s: Settings) -> list[dict]:
    return [dict(r) for r in connect(s.db_path).execute("SELECT * FROM passes ORDER BY id")]


def run(tracker: Tracker, *snaps):
    for sn in snaps:
        tracker.ingest(sn)


def test_overflight_is_one_row_not_one_per_poll(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, *(snap(1000 + 15 * i, ac("e80001", 5 + i, 35000, flight="AAL100")) for i in range(6)))
    assert len(rows(settings)) == 1 and rows(settings)[0]["open"] == 1
    run(tr, snap(1200))                               # gone for > grace (90 s)
    (r,) = rows(settings)
    assert r["open"] == 0 and r["outcome"] == "overflight" and r["points"] == 6
    assert r["min_alt_ft"] == r["max_alt_ft"] == 35000 and r["callsign"] == "AAL100" and r["reference"] == "site"


def test_parked_aircraft_never_opens_a_pass(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, *(snap(1000 + 15 * i, ac("e80002", 0.5, "ground")) for i in range(5)))
    assert rows(settings) == []


def test_arrival_seen_landing(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80003", 25, 15000, -1200)),
        snap(1060, ac("e80003", 15, 12000, -1000)),
        snap(1120, ac("e80003", 6, 9500, -800)),
        snap(1180, ac("e80003", 0.5, "ground")),
        snap(1400))
    (r,) = rows(settings)
    assert r["outcome"] == "arrival" and r["landed_at"] == pytest.approx(1179.5) and r["go_arounds"] == 0


def test_lost_on_approach_is_kept_open_then_presumed_landed(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80004", 25, 15000, -1200)), snap(1060, ac("e80004", 12, 11000, -900)))
    run(tr, snap(1300))                               # past the normal grace, inside the arrival grace
    assert rows(settings)[0]["open"] == 1
    run(tr, snap(2100))                               # past the arrival grace (900 s)
    (r,) = rows(settings)
    assert r["open"] == 0 and r["outcome"] == "arrival_presumed"


def test_go_around_then_landing(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80005", 20, 13000, -1000)),
        snap(1060, ac("e80005", 3, 8400, -700)),          # ~500 ft above the field
        snap(1100, ac("e80005", 4, 9400, 1800)),          # climbing away: go-around
        snap(1300, ac("e80005", 18, 12000, -900)),        # second approach
        snap(1400, ac("e80005", 3, 8500, -700)),
        snap(1460, ac("e80005", 0.3, "ground")),
        snap(1700))
    (r,) = rows(settings)
    assert r["go_arounds"] == 1 and r["outcome"] == "arrival"


def test_go_around_then_leaving(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80006", 20, 13000, -1000)),
        snap(1060, ac("e80006", 3, 8400, -700)),
        snap(1100, ac("e80006", 6, 10000, 2000)),
        snap(1160, ac("e80006", 70, 16000, 1500)),        # out of the zone
        snap(1400))
    (r,) = rows(settings)
    assert r["go_arounds"] == 1 and r["outcome"] == "go_around"


def test_departure(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80007", 3, 9000, 2000)), snap(1060, ac("e80007", 20, 14000, 2500)),
        snap(1120, ac("e80007", 80, 20000, 2000)), snap(1400))
    (r,) = rows(settings)
    assert r["outcome"] == "departure" and r["departure"] == 1


def test_restart_resumes_the_open_pass(settings):
    run(Tracker(settings, connect(settings.db_path)),
        snap(1000, ac("e80008", 5, 30000)), snap(1015, ac("e80008", 6, 30000)))
    tr2 = Tracker(settings, connect(settings.db_path))   # a new process over the same file
    run(tr2, snap(1030, ac("e80008", 7, 30000)))
    (r,) = rows(settings)
    assert r["points"] == 3 and r["open"] == 1


def test_no_reference_point_means_no_tracking(monkeypatch, tmp_path):
    monkeypatch.setenv("ADSB_DB", str(tmp_path / "x.db"))
    for k in ("ADSB_SITE_LAT", "ADSB_SITE_LON", "ADSB_AIRPORT_LAT", "ADSB_AIRPORT_LON"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError):
        Tracker(Settings(), connect(Settings().db_path))


def test_airport_alone_is_enough_for_passes(settings, monkeypatch):
    monkeypatch.delenv("ADSB_SITE_LAT")
    monkeypatch.delenv("ADSB_SITE_LON")
    s = Settings()
    run(Tracker(s, connect(s.db_path)), snap(1000, ac("e80009", 10, 20000)))
    assert rows(s)[0]["reference"] == "airport"


class SwitchableFeeder:
    def __init__(self) -> None:
        self.down = False
        self.body, self.raw = {"now": 0}, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=req)
        return httpx.Response(200, json={"now": self.body["now"], "aircraft": self.raw})


def test_feeder_outage_never_closes_or_splits_a_pass(settings, monkeypatch):
    fake = SwitchableFeeder()
    real = httpx.AsyncClient
    monkeypatch.setattr(feeder_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(fake), **kw))
    snaps = Snapshots(FeederClient(settings), max_age_s=30)
    tr = Tracker(settings, connect(settings.db_path))

    async def tick(now, *aircraft):
        fake.body, fake.raw = {"now": now}, list(aircraft)
        return await poll_once(snaps, tr)

    async def scenario():
        assert await tick(1000, ac("e80010", 5, 30000))
        fake.down = True
        assert not await tick(1100)                      # 5 failed polls spanning > grace...
        assert not await tick(1300)
        fake.down = False
        assert await tick(1310, ac("e80010", 9, 30000))  # ...and it's still the same pass
    asyncio.run(scenario())
    (r,) = rows(settings)
    assert r["open"] == 1 and r["points"] == 2


# --- the history tools, by name through the server ---------------------------------------------------

def _call(s: Settings, name: str, args: dict) -> dict:
    res = asyncio.run(build_server(s).call_tool(name, args))
    return json.loads([c for c in res.content if c.type == "text"][-1].text)


@pytest.fixture
def history(settings):
    import time
    t0 = time.time() - 3600
    tr = Tracker(settings, connect(settings.db_path))
    # LNE1454 twice (same airframe), one arrival; AAL100 once; an E295 once.
    run(tr, snap(t0, ac("e80003", 25, 15000, -1200)), snap(t0 + 60, ac("e80003", 6, 9500, -800)),
        snap(t0 + 120, ac("e80003", 0.5, "ground")), snap(t0 + 400))
    run(tr, snap(t0 + 1000, ac("e80003", 10, 30000)), snap(t0 + 1300))
    run(tr, snap(t0 + 1500, ac("a12345", 10, 35000, flight="AAL100", reg="N1AA", typ="B738")), snap(t0 + 1800))
    run(tr, snap(t0 + 2000, ac("e8bbbb", 10, 28000, flight="AZU999", reg="PR-AXA", typ="E295")))  # still open
    return settings


def test_traffic_stats_by_callsign_airframe_type(history):
    d = _call(history, "traffic_stats", {"group_by": "callsign"})
    assert d["status"] == "ok" and d["total_passes"] == 4 and d["total_airframes"] == 3
    assert d["rows"][0]["callsign"] == "LNE1454" and d["rows"][0]["passes"] == 2
    a = _call(history, "traffic_stats", {"group_by": "airframe"})["rows"][0]
    assert a["hex"] == "e80003" and a["registration"] == "CC-BHD" and a["callsigns"] == ["LNE1454"]
    t = _call(history, "traffic_stats", {"group_by": "type", "order": "least", "limit": 1})["rows"]
    assert len(t) == 1 and t[0]["passes"] == 1
    o = {r["outcome"]: r["passes"] for r in _call(history, "traffic_stats", {"group_by": "outcome"})["rows"]}
    assert o["arrival"] == 1 and o["overflight"] == 2 and o[None] == 1   # the open pass has no outcome yet
    h = _call(history, "traffic_stats", {"group_by": "hour"})
    assert sum(r["passes"] for r in h["rows"]) <= 4 and h["recorder"]["enabled"] is True


def test_traffic_stats_refuses_bad_input(history):
    assert _call(history, "traffic_stats", {"group_by": "airline"})["status"] == "refused"
    assert _call(history, "traffic_stats", {"order": "sideways"})["status"] == "refused"
    assert _call(history, "traffic_stats", {"since_days": 0})["status"] == "refused"


def test_aircraft_history_seen_before(history):
    d = _call(history, "aircraft_history", {"query": "cc-bhd"})
    assert d["status"] == "ok" and d["seen_before"] is True and d["total_passes"] == 2
    assert d["passes"][0]["outcome"] == "overflight" and d["passes"][1]["outcome"] == "arrival"
    assert d["passes"][1]["landed_at"] is not None and d["airframes"][0]["passes"] == 2
    assert _call(history, "aircraft_history", {"query": "azu"})["passes"][0]["in_progress"] is True
    assert _call(history, "aircraft_history", {"query": "zzz"})["seen_before"] is False
    assert _call(history, "aircraft_history", {"query": "%"})["total_passes"] == 0   # LIKE wildcards are literal
    assert _call(history, "aircraft_history", {"query": " "})["status"] == "refused"


def test_history_tools_refuse_when_not_recording(monkeypatch, tmp_path):
    monkeypatch.setenv("ADSB_DB", str(tmp_path / "none.db"))
    for name, args in (("traffic_stats", {}), ("aircraft_history", {"query": "lan"})):
        d = _call(Settings(), name, args)
        assert d["status"] == "refused" and "ADSB_POLL_SECONDS" in d["advice"]


def test_coverage_gap_reopens_the_same_pass(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80011", 10, 17500)), snap(1015, ac("e80011", 9, 17500)),
        snap(1200))                                       # gone > grace: closed (overflight, for now)
    assert rows(settings)[0]["open"] == 0
    run(tr, snap(1300, ac("e80011", 20, 14000, -1300)))   # back after a 285 s gap (< merge window)
    (r,) = rows(settings)
    assert r["open"] == 1 and r["outcome"] is None and r["points"] == 3


def test_a_late_return_is_a_new_pass(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80012", 10, 35000)), snap(1200), snap(2000, ac("e80012", 10, 35000)))
    assert [r["open"] for r in rows(settings)] == [0, 1]   # 1000 s later (> 600 s merge window): new visit


def test_a_landed_pass_is_never_reopened(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80013", 15, 12000, -1000)), snap(1060, ac("e80013", 0.5, "ground")),
        snap(1300), snap(1400, ac("e80013", 3, 9000, 2000)))   # quick turnaround: a new (departure) pass
    r = rows(settings)
    assert len(r) == 2 and r[0]["outcome"] == "arrival" and r[1]["departure"] == 1


def test_departure_first_seen_well_out_while_climbing(settings):
    # The LNE1397 case: takeoff hidden by terrain, first seen ~20 km out, ~1,700 ft above the field, climbing.
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80020", 20, 9600, 2200)), snap(1015, ac("e80020", 24, 10200, 2400)),
        snap(1030, ac("e80020", 70, 19000, 2000)), snap(1300))
    (r,) = rows(settings)
    assert r["departure"] == 1 and r["outcome"] == "departure"


def test_departure_rule_fires_in_the_first_few_snapshots_only(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80021", 15, 11000, 0)), snap(1015, ac("e80021", 16, 11000, 1500)))   # 2nd sample
    run(tr, snap(1000, ac("e80022", 15, 11000, 0)), *(snap(1015 + 15 * i, ac("e80022", 15, 11000, 0)) for i in range(5)),
        snap(1100, ac("e80022", 16, 11500, 1500)))                                                # too late
    flags = {r["hex"]: r["departure"] for r in rows(settings)}
    assert flags == {"e80021": 1, "e80022": 0}


def test_high_climbing_overflight_is_not_a_departure(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, snap(1000, ac("e80023", 20, 16000, 1500)), snap(1300))      # 8,100 ft above the field
    assert rows(settings)[0]["outcome"] == "overflight"


# --- circling / loitering, military, category --------------------------------------------------------

def circle(hx, n, t0, centre_km=8, radius_km=3, deg_per_step=30, alts=(8400, 10000), **kw):
    """n snapshots flying counter-clockwise around a point east of the airport, climbing and descending —
    the shape of the Grob G 120TP training session that was misread as 4 go-arounds."""
    snaps = []
    clat, clon = east_of_airport(centre_km)
    for i in range(n):
        th = math.radians(i * deg_per_step)
        lat = clat + radius_km * math.sin(th) / 111.32
        lon = clon + radius_km * math.cos(th) / (111.32 * math.cos(math.radians(clat)))
        down = (i // 3) % 2 == 0                     # 3 snapshots down, 3 up, ...
        alt = alts[0] + (alts[1] - alts[0]) * ((i % 3) / 2 if not down else 1 - (i % 3) / 2)
        raw = {"hex": hx, "flight": kw.get("flight", "FAE1103"), "r": kw.get("reg"), "t": kw.get("typ", "G12T"),
               "alt_baro": int(alt), "baro_rate": -1500 if down else 1500, "gs": 140,
               "track": (-math.degrees(th)) % 360, "lat": lat, "lon": lon, "seen": 0.5, "seen_pos": 0.5,
               "category": kw.get("category", "A1"), "dbFlags": kw.get("dbFlags", 0)}
        snaps.append(snap(t0 + 15 * i, raw))
    return snaps


def test_training_circles_are_loitering_not_go_arounds(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, *circle("e847ff", 30, 1000))             # 2.5 turns
    run(tr, snap(1000 + 15 * 30 + 200))
    (r,) = rows(settings)
    assert r["turn_deg"] >= 720 and r["outcome"] == "loitering" and r["go_arounds"] == 0


def test_straight_flight_with_track_jitter_does_not_turn(settings):
    tr = Tracker(settings, connect(settings.db_path))
    run(tr, *(snap(1000 + 15 * i, {**ac("e80030", 5 + i, 35000), "gs": 450, "track": 90 + (i % 2)})
              for i in range(20)))
    assert rows(settings)[0]["turn_deg"] == 0


def test_loitering_without_arrival_tracking(settings, monkeypatch):
    monkeypatch.delenv("ADSB_AIRPORT_ELEV_FT")
    s = Settings()
    tr = Tracker(s, connect(s.db_path))
    run(tr, *circle("e847f9", 30, 1000, flight="ARPIA", typ=None, category="A7"), snap(1000 + 15 * 30 + 200))
    assert rows(s)[0]["outcome"] == "loitering"


def test_military_flag_patterns_and_category(settings, monkeypatch):
    monkeypatch.setenv("ADSB_MILITARY_PATTERNS", "fae*, HIERRO*, ARPIA*, bad pattern!")
    s = Settings()
    assert s.military_patterns == ("FAE*", "HIERRO*", "ARPIA*")
    import time
    t0 = time.time() - 3600                          # inside traffic_stats' window
    tr = Tracker(s, connect(s.db_path))
    run(tr, *circle("e847f9", 6, t0, flight="ARPIA", typ=None, category="A7"))                    # pattern
    run(tr, *circle("ae68b5", 3, t0, flight=None, reg="11-3016", typ="D328", dbFlags=1))          # readsb flag
    run(tr, snap(t0, ac("e80040", 10, 35000, flight="AAL100")))                                    # neither
    run(tr, snap(t0 + 1000))
    d = _call(s, "aircraft_history", {"query": "ARPIA"})["passes"][0]
    assert d["military"] is True and d["military_basis"] == "pattern" and d["rotorcraft"] is True
    d = _call(s, "aircraft_history", {"query": "11-3016"})["passes"][0]
    assert d["military"] is True and d["military_basis"] == "readsb"
    assert _call(s, "aircraft_history", {"query": "AAL100"})["passes"][0]["military"] is False
    mil = _call(s, "traffic_stats", {"group_by": "airframe", "only": "military"})
    assert mil["total_passes"] == 2 and {r["hex"] for r in mil["rows"]} == {"e847f9", "ae68b5"}
    heli = _call(s, "traffic_stats", {"group_by": "callsign", "only": "rotorcraft"})
    assert [r["callsign"] for r in heli["rows"]] == ["ARPIA"]
    cats = {r["category"]: r["passes"] for r in _call(s, "traffic_stats", {"group_by": "category"})["rows"]}
    assert cats["A7"] == 1
    assert _call(s, "traffic_stats", {"only": "boats"})["status"] == "refused"


def test_live_tools_mark_military(settings, monkeypatch):
    monkeypatch.setenv("ADSB_MILITARY_PATTERNS", "ARPIA*")
    raws = [{"hex": "e847f9", "flight": "ARPIA ", "alt_baro": 9100, "lat": -0.18, "lon": -78.48, "seen": 0.1,
             "seen_pos": 0.2, "category": "A7"},
            {"hex": "ae68b5", "r": "11-3016", "t": "D328", "alt_baro": 15000, "lat": -0.1, "lon": -78.4,
             "seen": 0.1, "seen_pos": 0.2, "dbFlags": 1},
            {"hex": "e80001", "flight": "LNE1454 ", "alt_baro": 30000, "lat": -0.2, "lon": -78.3, "seen": 0.1,
             "seen_pos": 0.2}]
    real = httpx.AsyncClient
    monkeypatch.setattr(feeder_mod.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"now": 1, "aircraft": raws})), **kw))
    got = {a["hex"]: (a["military"], a.get("military_basis"))
           for a in _call(Settings(), "aircraft_overhead", {"include_grounded": True})["aircraft"]}
    assert got == {"e847f9": (True, "pattern"), "ae68b5": (True, "readsb"), "e80001": (False, None)}
