"""
Persists the rate table the admin panel's "ניהול תעריפים" screen edits, and
syncs it into calc_engine.RATE_TABLE (a plain in-memory dict) so the pure
calculation functions never need to know the DB exists.

calc_engine.py's in-code RATE_TABLE stays as the bootstrap default -- on
first run, seed_if_empty() copies it into the DB so there's always a
starting point even before any admin edit.
"""

from __future__ import annotations

import json

import calc_engine
from db import get_conn


def seed_if_empty() -> None:
    with get_conn() as conn:
        existing = conn.execute("SELECT year FROM rate_tables").fetchall()
        if existing:
            return
        for year, table in calc_engine.RATE_TABLE.items():
            conn.execute(
                "INSERT INTO rate_tables (year, json_blob, updated_by) VALUES (?, ?, ?)",
                (year, json.dumps(_serializable(table)), "system:seed"),
            )


def _serializable(table: dict) -> dict:
    out = dict(table)
    out["uplift_countries"] = sorted(table["uplift_countries"])
    return out


def load_all_into_engine() -> None:
    """Call at app startup, and again after any admin save, so
    calc_engine.RATE_TABLE reflects what's in the DB."""
    with get_conn() as conn:
        rows = conn.execute("SELECT year, json_blob FROM rate_tables").fetchall()
    for row in rows:
        table = json.loads(row["json_blob"])
        table["uplift_countries"] = set(table["uplift_countries"])
        calc_engine.RATE_TABLE[row["year"]] = table


def get_rate_table(year: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT json_blob, updated_by, updated_at FROM rate_tables WHERE year = ?", (year,)
        ).fetchone()
    if not row:
        raise LookupError(f"No rate table stored for {year}")
    table = json.loads(row["json_blob"])
    return {"year": year, "rates": table, "updated_by": row["updated_by"], "updated_at": row["updated_at"]}


def save_rate_table(year: int, new_rates: dict, actor: str) -> None:
    """Overwrites the stored table for `year`, logs a per-field diff to
    rate_history, and re-syncs calc_engine.RATE_TABLE."""
    with get_conn() as conn:
        row = conn.execute("SELECT json_blob FROM rate_tables WHERE year = ?", (year,)).fetchone()
        old = json.loads(row["json_blob"]) if row else {}

        for field, new_val in new_rates.items():
            old_val = old.get(field)
            if old_val != new_val:
                conn.execute(
                    "INSERT INTO rate_history (year, updated_by, field, old_value, new_value) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (year, actor, field, json.dumps(old_val), json.dumps(new_val)),
                )

        conn.execute(
            "INSERT INTO rate_tables (year, json_blob, updated_by) VALUES (?, ?, ?) "
            "ON CONFLICT(year) DO UPDATE SET json_blob = excluded.json_blob, "
            "updated_by = excluded.updated_by, updated_at = datetime('now')",
            (year, json.dumps(new_rates), actor),
        )
    load_all_into_engine()


def get_history(year: int, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, updated_by, field, old_value, new_value FROM rate_history "
            "WHERE year = ? ORDER BY id DESC LIMIT ?",
            (year, limit),
        ).fetchall()
    return [dict(r) for r in rows]
