"""
SQLite schema and connection helper for the travel-expense-report app.
Small-office scale (per the architecture doc's storage recommendation) --
one file, no server process to run.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from paths import DATA_DIR

DB_PATH = DATA_DIR / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id                    TEXT PRIMARY KEY,
    contact_name          TEXT NOT NULL,
    contact_email         TEXT NOT NULL,
    destination           TEXT NOT NULL,
    country               TEXT NOT NULL,
    start_date            TEXT NOT NULL,
    end_date              TEXT NOT NULL,
    purpose               TEXT NOT NULL,
    purpose_explanation   TEXT NOT NULL,
    proof_file_ref        TEXT,
    status                TEXT NOT NULL DEFAULT 'pending',
    rate_year             INTEGER,
    grand_total_claimed   REAL,
    grand_total_recognized REAL,
    pdf_file_ref          TEXT,
    legal_confirmed       INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS travelers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id             TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    role                TEXT NOT NULL,
    days_json           TEXT NOT NULL,
    flight_price        REAL,
    flight_currency     TEXT,
    flight_class        TEXT,
    flight_file_ref     TEXT,
    lodging_hotel       TEXT,
    lodging_cost        REAL,
    lodging_currency    TEXT,
    lodging_file_ref    TEXT,
    car_enabled         INTEGER NOT NULL DEFAULT 0,
    car_days            INTEGER,
    car_cost_per_day    REAL,
    car_currency        TEXT,
    car_file_ref        TEXT,
    recognized_json     TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id  TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    ts       TEXT NOT NULL DEFAULT (datetime('now')),
    event    TEXT NOT NULL,
    actor    TEXT,
    note     TEXT
);

CREATE TABLE IF NOT EXISTS otp_codes (
    email       TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS draft_tokens (
    token       TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_tables (
    year        INTEGER PRIMARY KEY,
    json_blob   TEXT NOT NULL,
    updated_by  TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rate_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    year        INTEGER NOT NULL,
    updated_by  TEXT,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT
);
"""


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_event(conn, trip_id: str, event: str, actor: str | None = None, note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (trip_id, event, actor, note) VALUES (?, ?, ?, ?)",
        (trip_id, event, actor, note),
    )


if __name__ == "__main__":
    init_db()
    print("Initialized", DB_PATH)
