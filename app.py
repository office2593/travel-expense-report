"""
Flask app tying together calc_engine, db, storage, otp, rates_store,
fx_sync, and report_builder into one running service.

Run locally:
    python app.py
Then open http://localhost:5000/travel/new and http://localhost:5000/admin

Admin routes are protected by Google OAuth (admin_auth.py) -- set
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and ADMIN_ALLOWED_EMAILS (see that
module's docstring) before deploying, or every @require_admin route will
redirect to a login that always fails.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

import admin_auth
import calc_engine
import db
import fx_sync
import otp
import paths
import rates_store
import report_builder
import storage

BASE_DIR = Path(__file__).parent  # source code -- static/, templates/ (not user data)
REPORTS_DIR = paths.DATA_DIR / "generated_reports"
REPORTS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
# Railway (like Heroku, most PaaS platforms) terminates HTTPS at its own proxy
# and forwards plain HTTP to the container, setting X-Forwarded-Proto/-Host to
# say what the request really was. Without ProxyFix, Flask doesn't trust those
# headers and treats every request as plain http:// -- which makes
# url_for(..., _external=True) build "http://..." callback URLs, guaranteed to
# mismatch whatever "https://..." URI is registered in Google Cloud Console.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# SECRET_KEY signs the session cookie -- generated fresh per process start here,
# which invalidates sessions on every restart. Set a real, fixed FLASK_SECRET_KEY
# env var before deploying, or every server restart logs everyone out mid-flow.
import os
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
admin_auth.init_app(app)


def _init():
    db.init_db()
    rates_store.seed_if_empty()
    rates_store.load_all_into_engine()


_init()


# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------

@app.get("/")
@app.get("/travel/new")
def wizard_page():
    return send_from_directory(BASE_DIR / "static", "wizard.html")


@app.get("/admin")
@admin_auth.require_admin
def admin_page():
    return send_from_directory(BASE_DIR / "static", "admin.html")


# ---------------------------------------------------------------------------
# OTP
# ---------------------------------------------------------------------------

@app.post("/api/otp/send")
def api_otp_send():
    email = (request.json or {}).get("email", "").strip()
    if not email or "@" not in email:
        return jsonify(ok=False, error="כתובת אימייל לא תקינה"), 400
    try:
        otp.request_otp(email)
    except RuntimeError:
        return jsonify(ok=False, error="שליחת המייל נכשלה. נסה/י שוב בעוד רגע."), 502
    return jsonify(ok=True)


@app.post("/api/otp/verify")
def api_otp_verify():
    body = request.json or {}
    email = body.get("email", "").strip()
    code = body.get("code", "").strip()
    ok, error = otp.verify_otp(email, code)
    if ok:
        session.clear()
        session["verified_email"] = email.lower()
    return jsonify(ok=ok, error=error), (200 if ok else 400)


def _require_verified_email() -> tuple[str, None] | tuple[None, tuple]:
    """Trusts only the signed server-side session, never a client-supplied
    header/field -- otherwise anyone could claim any email without ever
    passing the OTP gate."""
    email = session.get("verified_email")
    if not email or not otp.is_verified(email):
        return None, (jsonify(ok=False, error="נדרש אימות אימייל"), 401)
    return email, None


# ---------------------------------------------------------------------------
# Trip lifecycle
# ---------------------------------------------------------------------------

@app.post("/api/trips/start")
def api_trips_start():
    email, err = _require_verified_email()
    if err:
        return err
    trip_id = "SUBM-" + secrets.token_hex(4).upper()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO trips (id, contact_name, contact_email, destination, country, "
            "start_date, end_date, purpose, purpose_explanation, status) "
            "VALUES (?, '', ?, '', '', '2000-01-01', '2000-01-01', '', '', 'draft')",
            (trip_id, email),
        )
        db.log_event(conn, trip_id, "trip_started", actor=email)
    return jsonify(trip_id=trip_id)


@app.post("/api/trips/<trip_id>/upload")
def api_upload(trip_id):
    email, err = _require_verified_email()
    if err:
        return err
    if "file" not in request.files:
        return jsonify(ok=False, error="לא צורף קובץ"), 400
    f = request.files["file"]
    allowed = {"application/pdf", "image/jpeg", "image/png"}
    if f.mimetype not in allowed:
        return jsonify(ok=False, error="רק PDF או תמונה (JPG/PNG)"), 400
    file_bytes = f.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        return jsonify(ok=False, error="הקובץ גדול מ-10MB"), 400
    ref = storage.save_upload(trip_id, f.filename, file_bytes)
    return jsonify(ok=True, ref=ref, filename=storage.original_name(ref))


@app.post("/api/trips/<trip_id>/draft")
def api_draft_save(trip_id):
    email, err = _require_verified_email()
    if err:
        return err
    payload = request.json or {}
    token = secrets.token_hex(16)
    expires = (datetime.now() + timedelta(days=7)).isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO draft_tokens (token, email, payload_json, expires_at) VALUES (?, ?, ?, ?)",
            (token, email, json.dumps(payload), expires),
        )
        db.log_event(conn, trip_id, "draft_saved", actor=email)
    return jsonify(ok=True, token=token)


@app.get("/api/drafts/<token>")
def api_draft_load(token):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT email, payload_json, expires_at FROM draft_tokens WHERE token = ?", (token,)
        ).fetchone()
    if not row:
        return jsonify(ok=False, error="טיוטה לא נמצאה"), 404
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        return jsonify(ok=False, error="הטיוטה פגה תוקף"), 410
    return jsonify(ok=True, email=row["email"], payload=json.loads(row["payload_json"]))


@app.post("/api/trips/<trip_id>/submit")
def api_submit(trip_id):
    email, err = _require_verified_email()
    if err:
        return err
    body = request.json or {}
    trip_data = body.get("trip", {})
    travelers_data = body.get("travelers", [])
    if not travelers_data:
        return jsonify(ok=False, error="חסרים נתוני נוסעים"), 400
    if not body.get("legal_confirmed"):
        return jsonify(ok=False, error="נדרש אישור ההצהרה המשפטית"), 400

    with db.get_conn() as conn:
        existing = conn.execute("SELECT status FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not existing:
            return jsonify(ok=False, error="נסיעה לא נמצאה"), 404
        if existing["status"] not in ("draft", "failed"):
            # idempotency: a trip that already succeeded (or is mid-flight) can't be
            # resubmitted -- but "failed" (e.g. a prior PDF-generation error) must stay
            # retryable, otherwise a transient failure locks the client out permanently.
            return jsonify(ok=False, error="הטופס הזה כבר נשלח"), 409

        conn.execute(
            "UPDATE trips SET contact_name=?, destination=?, country=?, start_date=?, end_date=?, "
            "purpose=?, purpose_explanation=?, proof_file_ref=?, legal_confirmed=1, status='processing' "
            "WHERE id=?",
            (
                trip_data.get("contact_name", ""), trip_data.get("destination", ""),
                trip_data.get("country", ""), trip_data.get("start_date", ""),
                trip_data.get("end_date", ""), trip_data.get("purpose", ""),
                trip_data.get("purpose_explanation", ""), trip_data.get("proof_file_ref"),
                trip_id,
            ),
        )
        # clear any traveler rows from a prior failed attempt before re-inserting,
        # so a retry doesn't duplicate them
        conn.execute("DELETE FROM travelers WHERE trip_id = ?", (trip_id,))
        for t in travelers_data:
            conn.execute(
                """INSERT INTO travelers
                (trip_id, name, role, days_json, flight_price, flight_currency, flight_class, flight_file_ref,
                 lodging_hotel, lodging_cost, lodging_currency, lodging_file_ref,
                 car_enabled, car_days, car_cost_per_day, car_currency, car_file_ref)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trip_id, t.get("name"), t.get("role"), json.dumps(t.get("days", [])),
                    t.get("flight_price"), t.get("flight_currency"), t.get("flight_class"), t.get("flight_file_ref"),
                    t.get("lodging_hotel"), t.get("lodging_cost"), t.get("lodging_currency"), t.get("lodging_file_ref"),
                    1 if t.get("car_enabled") else 0, t.get("car_days"), t.get("car_cost_per_day"),
                    t.get("car_currency"), t.get("car_file_ref"),
                ),
            )
        db.log_event(conn, trip_id, "submitted", actor=email)

    with db.get_conn() as conn:
        trip_row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        traveler_rows = conn.execute("SELECT * FROM travelers WHERE trip_id = ?", (trip_id,)).fetchall()

    try:
        pdf_path = REPORTS_DIR / f"{trip_id}.pdf"
        result = report_builder.build_report_pdf(trip_row, traveler_rows, pdf_path)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE trips SET pdf_file_ref=?, grand_total_recognized=?, rate_year=?, status='pending' WHERE id=?",
                (str(pdf_path.name), result["grand_total_recognized_ils"], result["rate_year"], trip_id),
            )
            db.log_event(conn, trip_id, "pdf_generated", actor="system",
                         note=f"{result['compressed_bytes']} bytes, {result['reduction_pct']}% smaller")
    except Exception as e:
        with db.get_conn() as conn:
            conn.execute("UPDATE trips SET status='failed' WHERE id=?", (trip_id,))
            db.log_event(conn, trip_id, "pdf_generation_failed", actor="system", note=str(e))
        return jsonify(ok=False, error="שגיאה בהפקת הדוח. ניתן לנסות לשלוח שוב, או לפנות למשרד."), 500

    return jsonify(ok=True, trip_id=trip_id, submission_id=trip_id,
                   grand_total_recognized_ils=result["grand_total_recognized_ils"])


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

@app.get("/api/admin/submissions")
@admin_auth.require_admin
def admin_submissions():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, contact_name, destination, country, start_date, status, "
            "grand_total_recognized, created_at, "
            "(SELECT COUNT(*) FROM travelers WHERE travelers.trip_id = trips.id) AS traveler_count "
            "FROM trips WHERE status != 'draft' ORDER BY created_at DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/admin/submissions/<trip_id>")
@admin_auth.require_admin
def admin_submission_detail(trip_id):
    with db.get_conn() as conn:
        trip_row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if not trip_row:
            return jsonify(ok=False, error="לא נמצא"), 404
        travelers = conn.execute("SELECT * FROM travelers WHERE trip_id = ?", (trip_id,)).fetchall()
        audit = conn.execute(
            "SELECT ts, event, actor, note FROM audit_log WHERE trip_id = ? ORDER BY id", (trip_id,)
        ).fetchall()
    return jsonify(
        trip=dict(trip_row),
        travelers=[dict(t) for t in travelers],
        audit=[dict(a) for a in audit],
    )


@app.get("/api/admin/submissions/<trip_id>/pdf")
@admin_auth.require_admin
def admin_submission_pdf(trip_id):
    with db.get_conn() as conn:
        row = conn.execute("SELECT pdf_file_ref FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not row or not row["pdf_file_ref"]:
        return jsonify(ok=False, error="הדוח עדיין לא הופק"), 404
    with db.get_conn() as conn:
        db.log_event(conn, trip_id, "pdf_downloaded", actor=request.headers.get("X-Actor", "admin"))
    return send_file(REPORTS_DIR / row["pdf_file_ref"], as_attachment=True, download_name=f"{trip_id}.pdf")


@app.get("/api/admin/rates/<int:year>")
@admin_auth.require_admin
def admin_get_rates(year):
    try:
        data = rates_store.get_rate_table(year)
    except LookupError as e:
        return jsonify(ok=False, error=str(e)), 404
    data["history"] = rates_store.get_history(year, limit=10)
    return jsonify(data)


@app.post("/api/admin/rates/<int:year>")
@admin_auth.require_admin
def admin_save_rates(year):
    body = request.json or {}
    actor = request.headers.get("X-Actor", "admin")
    rates_store.save_rate_table(year, body.get("rates", {}), actor)
    return jsonify(ok=True, history=rates_store.get_history(year, limit=10))


@app.get("/api/admin/fx-status")
@admin_auth.require_admin
def admin_fx_status():
    return jsonify(fx_sync.sync_status())


@app.post("/api/admin/fx-sync")
@admin_auth.require_admin
def admin_fx_sync_now():
    return jsonify(fx_sync.sync(days_back=30))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
