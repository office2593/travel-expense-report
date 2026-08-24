"""
Daily off-platform backup of app.db to Google Drive, so historical trip and
traveler data survives even if the Railway Volume it normally lives on is
ever lost or corrupted. Reuses the same Drive service account already set
up for receipt storage (drive_storage.py) -- if Drive isn't configured
(RECEIPTS_FOLDER_ID unset), this module is a no-op.

Complementary to Railway's own Volume Backups feature (Settings -> Backups
in the Railway dashboard, if available on the plan) -- that protects
against the same failure at the platform level; this gives a second,
independent copy outside Railway entirely.

Uses sqlite3's own online backup API (Connection.backup()), which is safe
to run against a live database being written to concurrently -- it does
not require stopping the app or locking out writers for the whole copy.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import db
import drive_storage

logger = logging.getLogger("db_backup")

BACKUP_FOLDER_NAME = "db-backups"
_folder_id_cache: str | None = None


def _backup_folder_id() -> str:
    global _folder_id_cache
    if not _folder_id_cache:
        _folder_id_cache = drive_storage.ensure_folder(BACKUP_FOLDER_NAME)
    return _folder_id_cache


def run_backup() -> dict:
    """Snapshots app.db and uploads it to Drive. Returns a small summary
    dict; raises on failure (caller decides how to handle/alert)."""
    if not drive_storage.ROOT_FOLDER_ID:
        return {"status": "skipped", "reason": "Drive not configured"}

    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "app.db"
        source = sqlite3.connect(db.DB_PATH)
        snapshot = sqlite3.connect(snapshot_path)
        try:
            # sqlite3's online backup API -- safe against a live DB still
            # being written to, no need to stop the app or lock writers.
            source.backup(snapshot)
        finally:
            snapshot.close()
            source.close()
        db_bytes = snapshot_path.read_bytes()

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"app-{timestamp}.db"
    result = drive_storage.upload_receipt(db_bytes, filename, "application/x-sqlite3", _backup_folder_id())
    logger.info("DB backup uploaded: %s (%d bytes)", filename, len(db_bytes))
    return {"status": "ok", "filename": filename, "bytes": len(db_bytes), "file_id": result["file_id"]}
