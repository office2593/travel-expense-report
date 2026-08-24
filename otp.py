"""
Server-side OTP email verification gate (item 3 from the security review).

Real logic (generation, expiry, attempt limiting, verification) runs for
real against the DB. Email sending goes through mailer.send_email() (shared
with report-copy and FX-alert emails elsewhere in the app) -- see that
module for the SendGrid setup steps and the local-dev logging fallback.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

from db import get_conn
from email_templates import otp_email_html
from mailer import send_email

CODE_TTL_MINUTES = 10
MAX_ATTEMPTS = 5


def _hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{email.lower()}:{code}".encode()).hexdigest()


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
        html_body=otp_email_html(code),
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
