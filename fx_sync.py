"""
Daily sync of Bank of Israel representative exchange rates into a local
SQLite cache, so the calc engine never depends on BOI's API being reachable
at report-generation time.

Source: BOI SDMX "Edge" API (verified working; the simple GetExchangeRate
endpoint does NOT support historical dates -- it always returns today's rate).

Schedule this to run once a day (e.g. via APScheduler inside the Flask app,
or a cron / Windows Task Scheduler entry) -- see run_daily_sync().
"""

from __future__ import annotations

import csv
import io
import sqlite3
import urllib.request
from datetime import date, datetime, timedelta

from paths import DATA_DIR

DB_PATH = DATA_DIR / "fx_rates.db"
CURRENCIES = ["USD", "EUR", "GBP"]
BASE_URL = (
    "https://edge.boi.gov.il/FusionEdgeServer/sdmx/v2/data/dataflow/"
    "BOI.STATISTICS/EXR/1.0/RER_{ccy}_ILS"
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fx_rates (
            currency   TEXT NOT NULL,
            rate_date  TEXT NOT NULL,
            rate       REAL NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (currency, rate_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fx_sync_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at         TEXT NOT NULL,
            currency       TEXT NOT NULL,
            rows_upserted  INTEGER NOT NULL,
            status         TEXT NOT NULL,
            message        TEXT
        )
        """
    )
    conn.commit()
    return conn


def fetch_currency(ccy: str, start: date, end: date) -> list[tuple[str, float]]:
    url = (
        BASE_URL.format(ccy=ccy)
        + f"?startPeriod={start.isoformat()}&endPeriod={end.isoformat()}&format=csv"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        try:
            rows.append((r["TIME_PERIOD"], float(r["OBS_VALUE"])))
        except (KeyError, ValueError):
            continue
    return rows


def sync(days_back: int = 366) -> dict:
    """Fetch `days_back` days of history for each currency and upsert into
    fx_rates.db. Returns a summary dict; also writes to fx_sync_log so the
    admin panel can show 'last sync' / row counts without recomputing."""
    end = date.today()
    start = end - timedelta(days=days_back)
    now = datetime.now().isoformat(timespec="seconds")

    conn = _connect()
    summary = {}
    for ccy in CURRENCIES:
        try:
            rows = fetch_currency(ccy, start, end)
            conn.executemany(
                """
                INSERT INTO fx_rates (currency, rate_date, rate, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(currency, rate_date)
                DO UPDATE SET rate = excluded.rate, fetched_at = excluded.fetched_at
                """,
                [(ccy, d, r, now) for d, r in rows],
            )
            conn.execute(
                "INSERT INTO fx_sync_log (run_at, currency, rows_upserted, status, message) "
                "VALUES (?, ?, ?, 'ok', NULL)",
                (now, ccy, len(rows)),
            )
            conn.commit()
            summary[ccy] = {"status": "ok", "rows": len(rows)}
        except Exception as e:  # noqa: BLE001 -- log and continue with other currencies
            conn.execute(
                "INSERT INTO fx_sync_log (run_at, currency, rows_upserted, status, message) "
                "VALUES (?, ?, 0, 'error', ?)",
                (now, ccy, str(e)),
            )
            conn.commit()
            summary[ccy] = {"status": "error", "message": str(e)}
    conn.close()
    return summary


def get_rate(currency: str, on_date: date) -> float:
    """Representative rate for `on_date`, falling back to the most recent
    prior business day if `on_date` itself has no published rate (BOI
    doesn't publish on weekends/holidays)."""
    conn = _connect()
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE currency = ? AND rate_date <= ? "
        "ORDER BY rate_date DESC LIMIT 1",
        (currency, on_date.isoformat()),
    ).fetchone()
    conn.close()
    if row is None:
        raise LookupError(
            f"No cached rate for {currency} on or before {on_date}. Run sync()."
        )
    return row[0]


def sync_status() -> dict:
    """Summary used by the admin panel's FX card: last sync time, next
    scheduled sync, and row counts per currency."""
    conn = _connect()
    last = conn.execute("SELECT MAX(run_at) FROM fx_sync_log WHERE status='ok'").fetchone()[0]
    counts = dict(
        conn.execute("SELECT currency, COUNT(*) FROM fx_rates GROUP BY currency").fetchall()
    )
    date_range = conn.execute(
        "SELECT MIN(rate_date), MAX(rate_date) FROM fx_rates"
    ).fetchone()
    conn.close()

    next_sync = None
    if last:
        last_dt = datetime.fromisoformat(last)
        next_sync = (last_dt + timedelta(days=1)).replace(
            hour=6, minute=0, second=0, microsecond=0
        )
        if next_sync <= datetime.now():
            next_sync = next_sync + timedelta(days=1)

    return {
        "last_sync": last,
        "next_sync": next_sync.isoformat(timespec="minutes") if next_sync else None,
        "row_counts": counts,
        "date_range": date_range,
    }


def run_daily_sync():
    """Entry point for a scheduler (APScheduler / cron / Task Scheduler).
    Only needs a short history each day since sync() re-upserts on conflict --
    30 days is enough to backfill any days missed while the job was down."""
    return sync(days_back=30)


if __name__ == "__main__":
    result = sync(days_back=366)
    for ccy, info in result.items():
        print(ccy, info)
    print(sync_status())
