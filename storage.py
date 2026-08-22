"""
File storage for uploaded receipts. Default backend is local disk under
uploads/ (the "start simple" recommendation from the architecture doc).

To switch to Google Drive once drive_storage.py has been verified against
real credentials, change save_upload()/get_bytes() to call
drive_storage.upload_receipt()/download_receipt() instead, and store the
returned Drive file_id as the ref instead of a local path. Nothing else in
the app needs to change -- routes only ever deal with opaque `ref` strings.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from paths import DATA_DIR

UPLOAD_ROOT = DATA_DIR / "uploads"


def save_upload(trip_id: str, filename: str, file_bytes: bytes) -> str:
    trip_dir = UPLOAD_ROOT / trip_id
    trip_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(filename).name}"
    path = trip_dir / safe_name
    path.write_bytes(file_bytes)
    return str(path.relative_to(UPLOAD_ROOT))


def get_bytes(ref: str) -> bytes:
    return (UPLOAD_ROOT / ref).read_bytes()


def get_path(ref: str) -> Path:
    return UPLOAD_ROOT / ref


def original_name(ref: str) -> str:
    """Strips the uuid-prefix save_upload() adds, for display purposes."""
    name = Path(ref).name
    prefix, _, rest = name.partition("_")
    return rest if len(prefix) == 8 and rest else name
