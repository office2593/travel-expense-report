"""
Production entry point, deployed on Railway via the Dockerfile in this
folder (installs Chromium via apt, which report_builder.py needs for PDF
rendering -- see report_builder.py's _find_browser() and CHROME_BIN).
app.py's own `if __name__ == "__main__"` block runs Flask's single-threaded
dev server instead; fine locally, not for Railway.

Before the first real deploy, in the Railway project settings:

1. Attach a Volume to this service and set its mount path as the DATA_DIR
   environment variable (e.g. /data). Without this, Railway's container
   filesystem is ephemeral -- the SQLite DB, uploaded receipts, generated
   PDFs, and the FX rate cache all vanish on every redeploy. See paths.py.

2. Set these environment variables (see the referenced modules' docstrings
   for what each is for and how to obtain it):
       FLASK_SECRET_KEY
       GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ADMIN_ALLOWED_EMAILS  (admin_auth.py)
       SENDGRID_API_KEY, SENDGRID_FROM_EMAIL                         (otp.py)
   Optional -- falls back to local disk storage under DATA_DIR if unset:
       GOOGLE_SERVICE_ACCOUNT_FILE, RECEIPTS_SHARED_DRIVE_ID          (drive_storage.py)

3. In Google Cloud Console, add this Railway deployment's real URL to the
   OAuth client's authorized redirect URIs:
   https://<your-railway-domain>/admin/auth/callback
"""

import threading

from app import _fx_scheduler_loop, app

# Started here rather than in app.py so pytest importing `app` directly never
# spins this up (it would otherwise make a real network call on a fresh,
# never-synced fx_rates.db during test collection).
threading.Thread(target=_fx_scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run()
