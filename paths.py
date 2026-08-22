"""
Single source of truth for where persistent data lives on disk (SQLite DBs,
uploaded receipts, generated PDFs).

On Railway specifically: the container filesystem is ephemeral by default --
anything written next to the source code is wiped on every redeploy and on
every restart. Attach a Railway Volume (Project -> your service -> Volumes
-> New Volume) and set its mount path as the DATA_DIR environment variable
(e.g. /data). Without this, the app will appear to work in a demo/testing
sense but will silently lose every submission, uploaded receipt, rate edit,
and the FX rate cache on the next deploy.

Locally (no DATA_DIR set), this defaults to the project folder itself, which
is what db.py/storage.py/fx_sync.py/app.py already assumed before this file
existed -- local behavior is unchanged.
"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
