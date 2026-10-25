"""OpenSky enrichment against a fake OpenSky (httpx.MockTransport — no network): one call per airframe per
UTC day, never before the nightly batch, empty-answer retries then give-up, 429 pause, daily cap, reserve,
token refresh; the migration for older stores; and the routes surfacing in the tools and the board."""

import asyncio
import json
import time

import httpx
import pytest

from adsb_mcp import feeder as feeder_mod
from adsb_mcp.board import build_board
from adsb_mcp.config import Settings
from adsb_mcp.enrich import OpenSky, enrich_once, usual_route
from adsb_mcp.feeder import FeederClient, Snapshots
from adsb_mcp.server import build_server
from adsb_mcp.store import connect

from test_passes import snap

REAL_CLIENT = httpx.AsyncClient
DAY0 = (int(time.time()) // 86400 - 3) * 86400          # a UTC midnight three days ago
DAY = time.strftime("%Y-%m-%d", time.gmtime(DAY0))
READY = DAY0 + 86400 + 5 * 3600                          # next day, 05:00 UTC: OpenSky's batch is done


@pytest.fixture
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ADSB_DB", str(tmp_path / "adsb.db"))
    monkeypatch.setenv("ADSB_AIRPORT_ICAO", "SEQM")
    monkeypatch.setenv("ADSB_AIRPORT_LAT", "-0.129")
    monkeypatch.setenv("ADSB_AIRPORT_LON", "-78.358")
    monkeypatch.setenv("ADSB_AIRPORT_ELEV_FT", "7900")
    monkeypatch.setenv("ADSB_OPENSKY_CLIENT_ID", "test-client")
    monkeypatch.setenv("ADSB_OPENSKY_CLIENT_SECRET", "test-secret")
    for k in ("ADSB_SITE_LAT", "ADSB_SITE_LON", "ADSB_OPENSKY_DAILY_CREDITS", "ADSB_OPENSKY_RESERVE"):
        monkeypatch.delenv(k, raising=False)
    return Settings()


def add_pass(conn, hx, callsign, start, end, outcome="overflight"):
    conn.execute("INSERT INTO passes (hex, callsign, type, first_seen, last_seen, last_in_zone, points, open, "
                 "reference, outcome) VALUES (?, ?, 'A320', ?, ?, ?, 5, 0, 'airport', ?)",
                 (hx, callsign, start, end, end, outcome))
    conn.commit()


class FakeOpenSky:
    """Token endpoint + /flights/aircraft. `flights[hex]` = list of flights, or an int status to answer."""

    def __init__(self, flights=None, remaining="7000"):
        self.flights = flights or {}
        self.remaining = remaining
        self.calls: list[str] = []
        self.tokens = 0
        self.reject_next_token = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/token"):
            self.tokens += 1
            assert b"client_credentials" in req.content and b"test-secret" in req.content
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 1800})
        if self.reject_next_token:
            self.reject_next_token = False
            return httpx.Response(401)
        hx = req.url.params["icao24"]
        self.calls.append(hx)
        assert int(req.url.params["end"]) - int(req.url.params["begin"]) < 86400   # one calendar day
        got = self.flights.get(hx, 404)
        headers = {"X-Rate-Limit-Remaining": self.remaining}
        if got == 429:
            headers["X-Rate-Limit-Retry-After-Seconds"] = "7200"
        if isinstance(got, int):
            return httpx.Response(got, headers=headers)
        return httpx.Response(200, json=got, headers=headers)


def client(settings, fake):
    return OpenSky(settings, http=REAL_CLIENT(transport=httpx.MockTransport(fake)))


def run(settings, fake, now=READY):
    conn = connect(settings.db_path)
    return asyncio.run(enrich_once(settings, conn, client(settings, fake), now=now))


def flight(cs, dep, arr, first, last):
    return {"icao24": "x", "firstSeen": first, "estDepartureAirport": dep, "lastSeen": last,
            "estArrivalAirport": arr, "callsign": cs + "  "}


def routes(settings):
    return [(r["callsign"], r["origin"], r["destination"], r["route_source"])
            for r in connect(settings.db_path).execute("SELECT * FROM passes ORDER BY id")]


def test_one_call_per_airframe_per_day_and_matching(settings):
    conn = connect(settings.db_path)
    add_pass(conn, "e80001", "LNE1454", DAY0 + 3600, DAY0 + 4000)          # out...
    add_pass(conn, "e80001", "LNE1455", DAY0 + 30000, DAY0 + 30400)        # ...and back, same airframe
    add_pass(conn, "e80002", "AVA8371", DAY0 + 50000, DAY0 + 50300)
    fake = FakeOpenSky({
        "e80001": [flight("LNE1454", "SEQM", "SPJC", DAY0 + 3000, DAY0 + 12000),
                   flight("LNE1455", "SPJC", "SEQM", DAY0 + 22000, DAY0 + 31000)],
        "e80002": [flight("AVA8371", "SKBO", "SEQM", DAY0 + 45000, DAY0 + 51000)]})
    s = run(settings, fake)
    assert sorted(fake.calls) == ["e80001", "e80002"] and fake.tokens == 1 and s["matched"] == 3
    assert routes(settings) == [("LNE1454", "SEQM", "SPJC", "opensky"), ("LNE1455", "SPJC", "SEQM", "opensky"),
                                ("AVA8371", "SKBO", "SEQM", "opensky")]
    run(settings, fake, now=READY + 7200)
    assert len(fake.calls) == 2                    # never asked twice


def test_nothing_before_the_nightly_batch(settings):
    add_pass(connect(settings.db_path), "e80001", "LNE1454", DAY0 + 3600, DAY0 + 4000)
    fake = FakeOpenSky()
    run(settings, fake, now=DAY0 + 86400 + 3600)   # 01:00 the next day: too early
    assert fake.calls == []


def test_empty_is_retried_then_given_up(settings):
    add_pass(connect(settings.db_path), "e80003", "XYZ1", DAY0 + 3600, DAY0 + 4000)
    fake = FakeOpenSky()                           # 404: OpenSky has nothing
    run(settings, fake)
    run(settings, fake, now=READY + 3600)          # too soon to retry
    assert fake.calls == ["e80003"]
    run(settings, fake, now=READY + 7 * 3600)
    run(settings, fake, now=READY + 14 * 3600)
    run(settings, fake, now=READY + 21 * 3600)
    assert len(fake.calls) == 3 and routes(settings) == [("XYZ1", None, None, "opensky:none")]


def test_429_pauses(settings):
    conn = connect(settings.db_path)
    add_pass(conn, "e80004", "A1", DAY0 + 3600, DAY0 + 4000)
    add_pass(conn, "e80005", "A2", DAY0 + 3600, DAY0 + 4000)
    fake = FakeOpenSky({"e80004": 429, "e80005": 429})
    s = run(settings, fake)
    assert len(fake.calls) == 1 and "rate limited" in s["stopped"]
    run(settings, fake, now=READY + 3600)          # still inside the retry-after window
    assert len(fake.calls) == 1


def test_daily_cap_and_reserve(settings, monkeypatch):
    conn = connect(settings.db_path)
    for i in range(4):
        add_pass(conn, f"e8100{i}", f"C{i}", DAY0 + 3600, DAY0 + 4000)
    monkeypatch.setenv("ADSB_OPENSKY_DAILY_CREDITS", "60")         # two calls' worth
    s = run(Settings(), FakeOpenSky())
    assert s["calls"] == 2 and "cap" in s["stopped"]
    monkeypatch.setenv("ADSB_OPENSKY_DAILY_CREDITS", "3000")
    fake = FakeOpenSky(remaining="400")                             # below the 1000 reserve
    s = run(Settings(), fake, now=READY + 86400)                    # a new UTC day
    assert len(fake.calls) == 1 and "reserve" in s["stopped"]


def test_expired_token_is_refreshed_once(settings):
    add_pass(connect(settings.db_path), "e80001", "LNE1454", DAY0 + 3600, DAY0 + 4000)
    fake = FakeOpenSky({"e80001": [flight("LNE1454", "SEQM", "SPJC", DAY0 + 3000, DAY0 + 12000)]})
    fake.reject_next_token = True
    run(settings, fake)
    assert fake.tokens == 2 and routes(settings)[0][1:3] == ("SEQM", "SPJC")


def test_migration_adds_route_columns_to_an_old_store(tmp_path):
    import re
    import sqlite3

    from adsb_mcp.store import SCHEMA
    first_release = re.sub(r",\n    origin .*?\n\);", "\n);", SCHEMA, count=1, flags=re.S)   # every later column
    assert "origin" not in first_release.split("CREATE INDEX")[0]
    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript(first_release)
    old.execute("INSERT INTO passes (hex, first_seen, last_seen, last_in_zone, points) VALUES ('e8', 1, 2, 2, 1)")
    old.commit()
    old.close()
    cols = {r[1] for r in connect(tmp_path / "old.db").execute("PRAGMA table_info(passes)")}
    assert {"origin", "destination", "route_source", "military", "turn_deg", "last_track", "max_dist_km"} <= cols


# --- the routes, surfaced -----------------------------------------------------------------------------

@pytest.fixture
def enriched(settings):
    conn = connect(settings.db_path)
    for i in range(4):                                    # LNE1454 usually SEQM -> SPJC...
        add_pass(conn, f"e8200{i}", "LNE1454", DAY0 + 3600 * (i + 1), DAY0 + 3600 * (i + 1) + 300)
    add_pass(conn, "e82009", "LNE1454", DAY0 + 40000, DAY0 + 40300, outcome="arrival")   # ...once it landed here
    fake = FakeOpenSky({**{f"e8200{i}": [flight("LNE1454", "SEQM", "SPJC", DAY0, DAY0 + 80000)] for i in range(4)},
                        "e82009": [flight("LNE1454", "SKBO", "SEQM", DAY0 + 35000, DAY0 + 41000)]})
    run(settings, fake)
    return settings


def _call(s, name, args):
    res = asyncio.run(build_server(s).call_tool(name, args))
    return json.loads([c for c in res.content if c.type == "text"][-1].text)


def test_usual_route_and_unusual_flag(enriched):
    u = usual_route(connect(enriched.db_path), "LNE1454")
    assert (u["origin"], u["destination"], u["seen"], u["of"]) == ("SEQM", "SPJC", 4, 5)
    d = _call(enriched, "aircraft_history", {"query": "LNE1454"})
    odd = [p for p in d["passes"] if p.get("unusual_route")]
    assert len(odd) == 1 and odd[0]["origin"] == "SKBO" and odd[0]["outcome"] == "arrival"
    assert d["usual_routes"]["LNE1454"]["destination"] == "SPJC"
    assert d["recorder"]["enrichment"]["credits_today"] == 150


def test_traffic_stats_by_route(enriched):
    rows = _call(enriched, "traffic_stats", {"group_by": "route", "since_days": 30})["rows"]
    assert rows[0]["route"] == "SEQM-SPJC" and rows[0]["passes"] == 4 and rows[1]["route"] == "SKBO-SEQM"


def test_live_tool_and_board_carry_the_usual_route(enriched, monkeypatch):
    raw = {"hex": "e8ffff", "flight": "LNE1454 ", "t": "A20N", "alt_baro": 30000, "lat": -0.10, "lon": -78.30,
           "seen": 0.5, "seen_pos": 0.5}
    monkeypatch.setattr(feeder_mod.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"now": time.time(), "aircraft": [raw]})), **kw))
    a = _call(enriched, "find_aircraft", {"query": "lne"})["aircraft"][0]
    assert a["usual_route"]["destination"] == "SPJC" and a["usual_route"]["seen"] == 4
    snaps = Snapshots(FeederClient(enriched))
    board = build_board(enriched, asyncio.run(snaps.get())[0], connect(enriched.db_path))
    assert board["overhead"][0]["usual_route"] == {"origin": "SEQM", "destination": "SPJC", "seen": 4}


def test_airport_aliases_apply_when_reading_and_the_store_stays_raw(settings, monkeypatch):
    monkeypatch.setenv("ADSB_AIRPORT_ALIASES", "seas, SEHI, bad-code")
    s = Settings()
    assert s.airport_aliases == ("SEAS", "SEHI")            # junk dropped
    conn = connect(s.db_path)
    for i, dest in enumerate(("SEHI", "SEQM", "SEHI")):
        add_pass(conn, f"e8300{i}", "KLM755", DAY0 + 3600 * (i + 1), DAY0 + 3600 * (i + 1) + 300, "arrival_presumed")
    fake = FakeOpenSky({f"e8300{i}": [flight("KLM755", "EHAM", d, DAY0, DAY0 + 80000)]
                        for i, d in enumerate(("SEHI", "SEQM", "SEHI"))})
    run(s, fake)
    assert [r[2] for r in routes(s)] == ["SEHI", "SEQM", "SEHI"]          # OpenSky's values, verbatim
    u = usual_route(connect(s.db_path), "KLM755", s)
    assert (u["origin"], u["destination"], u["seen"], u["of"]) == ("EHAM", "SEQM", 3, 3)
    rows_ = _call(s, "traffic_stats", {"group_by": "route"})["rows"]
    assert [(r["route"], r["passes"]) for r in rows_] == [("EHAM-SEQM", 3)]
    p = _call(s, "aircraft_history", {"query": "KLM755"})["passes"]
    assert {(x["destination"], x.get("destination_reported")) for x in p} == {("SEQM", "SEHI"), ("SEQM", None)}
    assert not any(x.get("unusual_route") for x in p)


def test_lookup_states_are_distinct(settings):
    conn = connect(settings.db_path)
    add_pass(conn, "e84001", "MATCH1", DAY0 + 3600, DAY0 + 4000)
    add_pass(conn, "e84002", "NOPE1", DAY0 + 3600, DAY0 + 4000)
    add_pass(conn, "e84003", "LATER1", int(time.time()) - 600, int(time.time()) - 300)   # today: not yet
    fake = FakeOpenSky({"e84001": [flight("MATCH1", "SKBO", "SEQM", DAY0 + 3000, DAY0 + 5000)],
                        "e84002": [flight("NOPE1", "SKBO", "SEQM", DAY0 + 40000, DAY0 + 45000)]})   # no overlap
    run(settings, fake)
    state = {cs: _call(settings, "aircraft_history", {"query": cs})["passes"][0]["route_lookup"]
             for cs in ("MATCH1", "NOPE1", "LATER1")}
    assert state == {"MATCH1": "matched", "NOPE1": "no_match", "LATER1": "pending"}
    week = _call(settings, "aircraft_history", {"query": "MATCH1"})["recorder"]["enrichment"]["last_7_days"]
    assert week == {"matched": 1, "no_match": 1, "pending": 1}
