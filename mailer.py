"""
Shared email sending via SendGrid's REST API, used by otp.py (verification
codes) and app.py (report copies to the client/office, FX-sync failure
alerts). Falls back to logging instead of sending when SENDGRID_API_KEY is
unset, so local development still works without credentials.
"""

from __future__ import annotations

import base64
import logging
import os

import requests

logger = logging.getLogger("mailer")

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
SENDGRID_FROM_EMAIL = os.environ.get("SENDGRID_FROM_EMAIL", "office@odcpa.co.il")
SENDGRID_FROM_NAME = os.environ.get("SENDGRID_FROM_NAME", "אורן דולב, רואה חשבון")


def send_email(to: str, subject: str, body: str, attachments: list[dict] | None = None) -> None:
    """attachments: list of {"filename": str, "content_bytes": bytes, "mime_type": str}."""
    if not SENDGRID_API_KEY:
        logger.warning(
            "SENDGRID_API_KEY not set -- logging instead of emailing (fine for local "
            "dev, must not happen in production).\nTo: %s\nSubject: %s\nBody: %s\nAttachments: %s",
            to, subject, body, [a["filename"] for a in (attachments or [])],
        )
        return

    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": SENDGRID_FROM_EMAIL, "name": SENDGRID_FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    if attachments:
        payload["attachments"] = [
            {
                "content": base64.b64encode(a["content_bytes"]).decode(),
                "filename": a["filename"],
                "type": a.get("mime_type", "application/octet-stream"),
                "disposition": "attachment",
            }
            for a in attachments
        ]

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}"},
        json=payload,
        timeout=15,
    )
    if resp.status_code >= 300:
        # Fail loudly rather than silently -- callers decide whether a failed
        # send should block anything (OTP does; report-copy/alert emails don't).
        logger.error("SendGrid send failed (%s): %s", resp.status_code, resp.text)
        raise RuntimeError(f"Email send failed: {resp.status_code}")
