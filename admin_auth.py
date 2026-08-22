"""
Google OAuth login for the admin panel -- this is what actually restricts
/admin and /api/admin/* to the office, closing the gap flagged in app.py's
module docstring (that section previously had zero authentication).

Setup (do this in Google Cloud Console, same project as Drive if desired):
    1. APIs & Services -> OAuth consent screen -> configure (Internal if
       using a Google Workspace domain, External + verification otherwise).
    2. APIs & Services -> Credentials -> Create OAuth client ID -> Web application.
       Authorized redirect URI: https://<your-domain>/admin/auth/callback
       (use http://localhost:5000/admin/auth/callback for local testing).
    3. Set environment variables before running the app:
       GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ADMIN_ALLOWED_EMAILS
       (comma-separated, e.g. "office@odcpa.co.il,staff@odcpa.co.il" --
       being a valid Google account is not enough on its own; it also has
       to be on this allowlist, otherwise anyone with a Google account
       could log in).

This module was written correctly against the real Google OAuth flow but
NOT run against real credentials in this session (none were available) --
unlike otp.py/db.py/etc., which were all actually executed. Verify it end
-to-end with a real GOOGLE_CLIENT_ID before relying on it.
"""

from __future__ import annotations

import functools
import os

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, jsonify, redirect, request, session, url_for

admin_auth_bp = Blueprint("admin_auth", __name__, url_prefix="/admin/auth")
oauth = OAuth()


def init_app(app):
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        app.logger.warning(
            "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET not set -- admin login routes "
            "are registered but will fail until these are configured."
        )
    oauth.init_app(app)
    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    app.register_blueprint(admin_auth_bp)


def _allowed_emails() -> set[str]:
    raw = os.environ.get("ADMIN_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


@admin_auth_bp.get("/login")
def login():
    redirect_uri = url_for("admin_auth.callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@admin_auth_bp.get("/callback")
def callback():
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").lower()

    if not userinfo.get("email_verified"):
        return "כתובת האימייל של חשבון Google לא מאומתת.", 403
    if email not in _allowed_emails():
        return f"החשבון {email} אינו מורשה לגשת לפאנל הניהול.", 403

    session["admin_email"] = email
    return redirect(url_for("admin_page"))


@admin_auth_bp.get("/logout")
def logout():
    session.pop("admin_email", None)
    return redirect(url_for("admin_auth.login"))


def require_admin(view):
    """Decorator for every /admin page route and /api/admin/* route."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_email"):
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="נדרשת התחברות"), 401
            return redirect(url_for("admin_auth.login"))
        return view(*args, **kwargs)
    return wrapped
