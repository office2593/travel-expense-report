"""
Receipt storage on the office's Google Drive instead of app-server disk.

Architecture:
    - Files are stored under one root folder in a real Google Drive the
      office already uses (a regular "My Drive" folder, shared with the
      service account as an Editor -- NOT a formal "Shared Drive", which
      needs a Workspace tier that supports it and its own separate setup).
      When a service account creates a file inside a folder shared to it
      this way, the file is owned by (and counts against the quota of)
      whoever owns the folder -- the human, not the robot -- so a plain
      shared folder works fine for this without any Shared Drive machinery.
    - One subfolder per submission (named "{trip_id} - {client_name}"),
      created under the root folder on first upload.
    - The app's own database stores only the Drive `file_id` (and
      `web_view_link` for a human-clickable link in the admin panel) per
      uploaded receipt -- never the file bytes.
    - At PDF-generation time, the app streams each receipt's bytes down
      from Drive into memory, merges into the final PDF (see
      compress_pdf.py / the merge step in the report pipeline), and
      discards the temporary bytes. Nothing is cached to local disk.

One-time setup (do this before this module can run):
    1. In Google Cloud Console, create a project and enable the Drive API.
    2. Create a Service Account and generate a JSON key for it. On a PaaS
       host like Railway with no durable local filesystem to keep the key
       file on, set GOOGLE_SERVICE_ACCOUNT_JSON to the *entire contents* of
       that key file as one environment variable (this is the pattern this
       module prefers). For local development, GOOGLE_SERVICE_ACCOUNT_FILE
       (a path to the key file, kept out of the repo / .gitignore'd) also
       works and is checked as a fallback.
    3. In Google Drive, create (or pick) a regular folder for receipts,
       and share it with the service account's email (looks like
       xxx@yyy.iam.gserviceaccount.com) with "Editor" access. Set
       RECEIPTS_FOLDER_ID to that folder's id (the long string in its URL,
       after .../folders/).

This module is written against the real Drive API v3 client library.
"""

from __future__ import annotations

import io
import os
from typing import BinaryIO, Optional

ROOT_FOLDER_ID = os.environ.get("RECEIPTS_FOLDER_ID", "")
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_service():
    import json

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if SERVICE_ACCOUNT_JSON:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(SERVICE_ACCOUNT_JSON), scopes=SCOPES
        )
    elif SERVICE_ACCOUNT_FILE:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    else:
        raise RuntimeError(
            "Set GOOGLE_SERVICE_ACCOUNT_JSON (the key file's full contents -- "
            "preferred on Railway) or GOOGLE_SERVICE_ACCOUNT_FILE (a local path)."
        )
    return build("drive", "v3", credentials=creds)


def ensure_trip_folder(trip_id: str, client_name: str) -> str:
    """Returns the Drive folder id for this trip, creating it under
    ROOT_FOLDER_ID if needed."""
    service = _get_service()
    folder_name = f"{trip_id} - {client_name}"
    query = (
        f"'{ROOT_FOLDER_ID}' in parents and name = '{folder_name}' "
        f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    results = service.files().list(
        q=query,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        fields="files(id, name)",
    ).execute()
    existing = results.get("files", [])
    if existing:
        return existing[0]["id"]

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [ROOT_FOLDER_ID],
    }
    folder = service.files().create(
        body=metadata, supportsAllDrives=True, fields="id"
    ).execute()
    return folder["id"]


def upload_receipt(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    folder_id: str,
) -> dict:
    """Uploads one receipt file into `folder_id`. Returns {file_id, web_view_link}
    -- store only these in the app DB, never the bytes."""
    from googleapiclient.http import MediaIoBaseUpload

    service = _get_service()
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
    metadata = {"name": filename, "parents": [folder_id]}
    created = service.files().create(
        body=metadata,
        media_body=media,
        supportsAllDrives=True,
        fields="id, webViewLink",
    ).execute()
    return {"file_id": created["id"], "web_view_link": created.get("webViewLink")}


def download_receipt(file_id: str) -> bytes:
    """Streams a receipt's bytes down from Drive for merging into the
    final PDF. Called at report-generation time, not stored to disk."""
    from googleapiclient.http import MediaIoBaseDownload

    service = _get_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def delete_receipt(file_id: str) -> None:
    """For retention-policy cleanup once a report has been finalized and
    archived, if the office wants receipts removed from Drive afterward."""
    service = _get_service()
    service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
