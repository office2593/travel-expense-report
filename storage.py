"""
File storage for uploaded receipts. Local disk under uploads/ is the
default backend; automatically switches to Google Drive (drive_storage.py)
when RECEIPTS_SHARED_DRIVE_ID is set, so nothing else in the app needs to
change -- routes only ever deal with opaque `ref` strings. Drive-backed
refs are prefixed "drive:<file_id>:<original_filename>" so original_name()
never needs a network call just to display a filename; local refs are
unprefixed relative paths ("<trip_id>/<8hex>_<filename>"), same as before.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from paths import DATA_DIR

UPLOAD_ROOT = DATA_DIR / "uploads"

_DRIVE_PREFIX = "drive:"


def _drive_enabled() -> bool:
    import drive_storage
    return bool(drive_storage.SHARED_DRIVE_ID)


def save_upload(trip_id: str, filename: str, file_bytes: bytes, mime_type: str = "") -> str:
    if _drive_enabled():
        import drive_storage
        folder_id = drive_storage.ensure_trip_folder(trip_id, trip_id)
        result = drive_storage.upload_receipt(file_bytes, filename, mime_type, folder_id)
        return f"{_DRIVE_PREFIX}{result['file_id']}:{filename}"

    trip_dir = UPLOAD_ROOT / trip_id
    trip_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(filename).name}"
    path = trip_dir / safe_name
    path.write_bytes(file_bytes)
    return str(path.relative_to(UPLOAD_ROOT))


def get_bytes(ref: str) -> bytes:
    if ref.startswith(_DRIVE_PREFIX):
        import drive_storage
        file_id = ref[len(_DRIVE_PREFIX):].split(":", 1)[0]
        return drive_storage.download_receipt(file_id)
    return (UPLOAD_ROOT / ref).read_bytes()


def get_path(ref: str) -> Path:
    """Local-disk only -- raises for a Drive-backed ref. Nothing in the app
    calls this for attachment merging any more (see get_bytes() + suffix()
    in report_builder.py); kept only in case something local-only needs a
    real filesystem path."""
    if ref.startswith(_DRIVE_PREFIX):
        raise ValueError("get_path() doesn't apply to Drive-backed refs; use get_bytes() instead.")
    return UPLOAD_ROOT / ref


def original_name(ref: str) -> str:
    """Strips the uuid-prefix save_upload() adds, for display purposes."""
    if ref.startswith(_DRIVE_PREFIX):
        return ref[len(_DRIVE_PREFIX):].split(":", 1)[1]
    name = Path(ref).name
    prefix, _, rest = name.partition("_")
    return rest if len(prefix) == 8 and rest else name


def suffix(ref: str) -> str:
    return Path(original_name(ref)).suffix.lower()
