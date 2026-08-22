"""
Server-side OTP email verification gate (item 3 from the security review).

Real logic (generation, expiry, attempt limiting, verification) runs for
real against the DB. send_email() sends for real via SendGrid's REST API
once SENDGRID_API_KEY (and optionally SENDGRID_FROM_EMAIL) are set as
environment variables; without a key it falls back to logging the message
instead, so local development still works without credentials -- but that
fallback must never be what's running in production, since it leaves the
OTP code sitting in the server log instead of reaching only the recipient.

Setup: create a SendGrid account (free tier covers OTP-scale volume),
verify a sender identity for the "from" address (Settings -> Sender
Authentication -- single sender verification is enough to start), and
create an API key with "Mail Send" permission. This was written against
SendGrid's real v3 API but NOT executed against a real API key in this
session (none were available) -- verify with a real key before deploying.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
from datetime import datetime, timedelta

import requests

from db import get_conn

logger = logging.getLogger("otp")

CODE_TTL_MINUTES = 10
MAX_ATTEMPTS = 5

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
SENDGRID_FROM_EMAIL = os.environ.get("SENDGRID_FROM_EMAIL", "office@odcpa.co.il")
SENDGRID_FROM_NAME = os.environ.get("SENDGRID_FROM_NAME", "אורן דולב, רואה חשבון")


def _hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{email.lower()}:{code}".encode()).hexdigest()


def send_email(to: str, subject: str, body: str) -> None:
    if not SENDGRID_API_KEY:
        logger.warning(
            "SENDGRID_API_KEY not set -- logging instead of emailing (fine for local "
            "dev, must not happen in production).\nTo: %s\nSubject: %s\nBody: %s",
            to, subject, body,
        )
        return

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}"},
        json={
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": SENDGRID_FROM_EMAIL, "name": SENDGRID_FROM_NAME},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
        timeout=10,
    )
    if resp.status_code >= 300:
        # Fail loudly rather than silently -- a client who never gets their
        # code has no way to know whether to wait, retry, or contact the office.
        logger.error("SendGrid send failed (%s): %s", resp.status_code, resp.text)
        raise RuntimeError(f"Email send failed: {resp.status_code}")


def request_otp(email: str) -> None:
    code = f"{random.randint(0, 999999):06d}"
    expires_at = (datetime.now() + timedelta(minutes=CODE_TTL_MINUTES)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO otp_codes (email, code, expires_at, verified, attempts) "
            "VALUES (?, ?, ?, 0, 0) "
            "ON CONFLICT(email) DO UPDATE SET code = excluded.code, "
            "expires_at = excluded.expires_at, verified = 0, attempts = 0",
            (email.lower(), _hash(email, code), expires_at),
        )
    send_email(
        email,
        "קוד אימות לטופס דיווח נסיעה",
        f"קוד האימות שלך: {code}\nהקוד בתוקף ל-{CODE_TTL_MINUTES} דקות.",
    )


def verify_otp(email: str, code: str) -> tuple[bool, str]:
    """Returns (ok, error_message)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT code, expires_at, verified, attempts FROM otp_codes WHERE email = ?",
            (email.lower(),),
        ).fetchone()
        if not row:
            return False, "לא נשלח קוד לכתובת זו. יש לבקש קוד חדש."
        if row["verified"]:
            return True, ""
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return False, "הקוד פג תוקף. יש לבקש קוד חדש."
        if row["attempts"] >= MAX_ATTEMPTS:
            return False, "יותר מדי ניסיונות שגויים. יש לבקש קוד חדש."

        if _hash(email, code) != row["code"]:
            conn.execute(
                "UPDATE otp_codes SET attempts = attempts + 1 WHERE email = ?",
                (email.lower(),),
            )
            remaining = MAX_ATTEMPTS - (row["attempts"] + 1)
            return False, f"קוד שגוי. נותרו {max(remaining, 0)} ניסיונות."

        conn.execute("UPDATE otp_codes SET verified = 1 WHERE email = ?", (email.lower(),))
    return True, ""


def is_verified(email: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT verified FROM otp_codes WHERE email = ?", (email.lower(),)
        ).fetchone()
    return bool(row and row["verified"])
