"""SQLite store for the historical tier: one row per PASS (a visit), never one per poll.

ACCUMULATES and is not reproducible (the live feed is gone once it's gone) — back the file up. Open passes
are written through on every poll (`open=1`), so a restart resumes them instead of losing or splitting them.
No coordinates are stored: distances only, so the file never holds the receiver's location.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS passes (
    id                  INTEGER PRIMARY KEY,
    hex                 TEXT NOT NULL,     -- ICAO 24-bit address: the airframe
    callsign            TEXT,              -- the route / service
    reg                 TEXT,
    type                TEXT,              -- ICAO type designator: the model
    category            TEXT,
    first_seen          REAL NOT NULL,     -- epoch seconds, feeder clock
    last_seen           REAL NOT NULL,
    last_in_zone        REAL NOT NULL,     -- last airborne sample inside the pass zone
    min_alt_ft          INTEGER,
    max_alt_ft          INTEGER,
    min_dist_km         REAL,              -- closest approach to the reference point
    max_rssi            REAL,
    points              INTEGER NOT NULL,  -- snapshots the aircraft appeared in during the pass
    emergency           TEXT,
    reference           TEXT,              -- site | airport: what min_dist_km is measured from
    departure           INTEGER NOT NULL DEFAULT 0,
    on_approach         INTEGER NOT NULL DEFAULT 0,  -- last airborne sample was on approach to the airport
    approach_low_agl_ft REAL,              -- lowest height above the field since the approach began
    go_arounds          INTEGER NOT NULL DEFAULT 0,
    landed_at           REAL,              -- seen on the ground at the airport after an approach
    outcome             TEXT,              -- set on close: arrival | arrival_presumed | loitering | go_around | departure | overflight
    open                INTEGER NOT NULL DEFAULT 1,
    origin              TEXT,              -- departure airport (ICAO), from enrichment
    destination         TEXT,              -- arrival airport (ICAO), from enrichment
    route_source        TEXT,              -- who set origin/destination (opensky); NULL = not looked up yet
    military            INTEGER NOT NULL DEFAULT 0,  -- readsb's database flag (patterns are applied on read)
    turn_deg            REAL NOT NULL DEFAULT 0,     -- total heading change during the pass (circling)
    last_track          REAL,
    max_dist_km         REAL,              -- with min_dist_km: how far the pass ranged (no coordinates kept)
    max_gs_kt           REAL,              -- fastest ground speed seen (loiter spread depends on it)
    route_gap_s         REAL,              -- time to the matched OpenSky flight (or the nearest, if no match)
    description         TEXT,              -- readsb's long type name, e.g. "BOEING 737-800"
    operator            TEXT,              -- readsb's owner / operator
    year                TEXT
);
CREATE INDEX IF NOT EXISTS passes_hex ON passes (hex, first_seen DESC);
CREATE INDEX IF NOT EXISTS passes_callsign ON passes (callsign, first_seen DESC);
CREATE INDEX IF NOT EXISTS passes_first_seen ON passes (first_seen DESC);
CREATE INDEX IF NOT EXISTS passes_open ON passes (open) WHERE open = 1;

-- Enrichment bookkeeping: at most ONE OpenSky call per airframe per UTC day, ever (retries only on lag).
CREATE TABLE IF NOT EXISTS opensky_lookups (
    hex        TEXT NOT NULL,
    day        TEXT NOT NULL,              -- YYYY-MM-DD (UTC) the passes began
    status     TEXT NOT NULL,              -- ok | empty (retried while OpenSky's nightly batch may lag) | error
    flights    INTEGER,
    attempts   INTEGER NOT NULL DEFAULT 0,
    checked_at REAL NOT NULL,
    PRIMARY KEY (hex, day)
);

-- Recorder health: last poll time / status (so a stale recorder is visible, never silent).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# Columns added after the first release: CREATE TABLE IF NOT EXISTS won't add them to an existing file.
MIGRATIONS = {"passes": [("origin", "TEXT"), ("destination", "TEXT"), ("route_source", "TEXT"),
                         ("military", "INTEGER NOT NULL DEFAULT 0"), ("turn_deg", "REAL NOT NULL DEFAULT 0"),
                         ("last_track", "REAL"), ("max_dist_km", "REAL"), ("max_gs_kt", "REAL"),
                         ("route_gap_s", "REAL"), ("description", "TEXT"), ("operator", "TEXT"), ("year", "TEXT")]}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue               # a fresh file: SCHEMA creates the table with every column
        for name, kind in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # the poller writes while the tools read
    conn.execute("PRAGMA busy_timeout=30000")
    _migrate(conn)                 # before SCHEMA: its indexes may name new columns
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | float | None) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, None if value is None else str(value)),
    )
