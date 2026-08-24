"""
Integration tests for app.py, exercised through Flask's real test client
(actual HTTP request/response cycle, real SQLite DB, real PDF generation via
headless Chrome -- not mocked). Each test gets a fresh DB via the `client`
fixture pointing db.DB_PATH at a temp file.

Run with: pytest test_app.py -v
(Slower than test_calc_engine.py -- PDF generation genuinely launches Chrome.)
"""

from __future__ import annotations

import copy
import io
import json

import pytest
from pypdf import PdfWriter

import calc_engine
import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    # rates_store.load_all_into_engine() mutates calc_engine.RATE_TABLE (a shared
    # module-level dict) in place -- without snapshotting and restoring it, a rate
    # edit made in one test here leaks into test_calc_engine.py's tests whenever
    # both files run in the same pytest session (monkeypatch.setattr alone doesn't
    # catch in-place dict mutation, only attribute reassignment).
    original_rate_table = copy.deepcopy(calc_engine.RATE_TABLE)

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    import storage
    monkeypatch.setattr(storage, "UPLOAD_ROOT", tmp_path / "uploads")

    import app as appmod
    appmod.REPORTS_DIR = tmp_path / "reports"
    appmod.REPORTS_DIR.mkdir(exist_ok=True)
    appmod.app.config["TESTING"] = True
    # The rate limiter is a module-level singleton shared by every test in
    # this session (appmod is the same cached module object each time), so
    # its counters would otherwise accumulate across tests and start
    # rejecting requests partway through the suite.
    appmod.limiter.enabled = False
    appmod._init()
    try:
        with appmod.app.test_client() as c:
            yield c
    finally:
        calc_engine.RATE_TABLE.clear()
        calc_engine.RATE_TABLE.update(original_rate_table)


def _captured_otp(monkeypatch, email):
    import otp
    captured = {}

    def fake_send(to, subject, body):
        captured["code"] = body.split(": ")[1].split("\n")[0]

    monkeypatch.setattr(otp, "send_email", fake_send)
    return captured


def _login_admin(client, email="office@odcpa.co.il"):
    with client.session_transaction() as sess:
        sess["admin_email"] = email


def _verify(client, monkeypatch, email="test@example.co.il"):
    captured = _captured_otp(monkeypatch, email)
    client.post("/api/otp/send", json={"email": email})
    r = client.post("/api/otp/verify", json={"email": email, "code": captured["code"]})
    assert r.status_code == 200
    return email


def _valid_pdf_bytes():
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _submission_payload(flight_ref, lodging_ref):
    return {
        "trip": {
            "contact_name": "בודק אוטומטי", "destination": "מינכן", "country": "גרמניה",
            "start_date": "2026-09-01", "end_date": "2026-09-12",
            "purpose": "פגישות לקוחות", "purpose_explanation": "בדיקה אוטומטית",
        },
        "travelers": [{
            "name": "בודק אוטומטי", "role": "primary",
            "days": [{"date": f"2026-09-{d:02d}", "status": "business"} for d in range(1, 13)],
            "flight_price": 780.0, "flight_currency": "USD", "flight_class": "economy",
            "flight_file_ref": flight_ref,
            "lodging_hotel": "מלון", "lodging_cost": 210.0, "lodging_currency": "USD",
            "lodging_file_ref": lodging_ref,
            "car_enabled": False,
        }],
        "legal_confirmed": True,
    }


# ---------------------------------------------------------------------------
# OTP gate
# ---------------------------------------------------------------------------

def test_otp_wrong_code_rejected(client, monkeypatch):
    _captured_otp(monkeypatch, "a@b.co.il")
    client.post("/api/otp/send", json={"email": "a@b.co.il"})
    r = client.post("/api/otp/verify", json={"email": "a@b.co.il", "code": "000000"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_otp_correct_code_accepted(client, monkeypatch):
    _verify(client, monkeypatch, "a@b.co.il")  # raises via assert inside if it fails


def test_trips_start_requires_verified_session(client):
    r = client.post("/api/trips/start")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def test_upload_rejects_bad_type(client, monkeypatch):
    _verify(client, monkeypatch)
    trip_id = client.post("/api/trips/start").get_json()["trip_id"]
    r = client.post(
        f"/api/trips/{trip_id}/upload",
        data={"file": (io.BytesIO(b"not a pdf"), "virus.exe")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400


def test_upload_accepts_valid_pdf(client, monkeypatch):
    _verify(client, monkeypatch)
    trip_id = client.post("/api/trips/start").get_json()["trip_id"]
    r = client.post(
        f"/api/trips/{trip_id}/upload",
        data={"file": (io.BytesIO(_valid_pdf_bytes()), "receipt.pdf")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["filename"] == "receipt.pdf"


# ---------------------------------------------------------------------------
# Submit -> full pipeline (real PDF generation)
# ---------------------------------------------------------------------------

def test_submit_generates_real_pdf_and_appears_in_admin(client, monkeypatch):
    _verify(client, monkeypatch)
    trip_id = client.post("/api/trips/start").get_json()["trip_id"]

    def upload(name):
        r = client.post(
            f"/api/trips/{trip_id}/upload",
            data={"file": (io.BytesIO(_valid_pdf_bytes()), name)},
            content_type="multipart/form-data",
        )
        return r.get_json()["ref"]

    flight_ref = upload("flight.pdf")
    lodging_ref = upload("lodging.pdf")

    r = client.post(f"/api/trips/{trip_id}/submit", json=_submission_payload(flight_ref, lodging_ref))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True
    # matches the pinned worked example in test_calc_engine.py
    assert body["grand_total_recognized_ils"] > 0

    # shows up in admin list
    _login_admin(client)
    r = client.get("/api/admin/submissions")
    ids = [row["id"] for row in r.get_json()]
    assert trip_id in ids

    # PDF actually downloadable and non-trivial size
    r = client.get(f"/api/admin/submissions/{trip_id}/pdf")
    assert r.status_code == 200
    assert r.content_length > 10_000


def test_double_submit_rejected(client, monkeypatch):
    _verify(client, monkeypatch)
    trip_id = client.post("/api/trips/start").get_json()["trip_id"]
    flight_ref = client.post(
        f"/api/trips/{trip_id}/upload", data={"file": (io.BytesIO(_valid_pdf_bytes()), "f.pdf")},
        content_type="multipart/form-data",
    ).get_json()["ref"]
    lodging_ref = client.post(
        f"/api/trips/{trip_id}/upload", data={"file": (io.BytesIO(_valid_pdf_bytes()), "l.pdf")},
        content_type="multipart/form-data",
    ).get_json()["ref"]

    payload = _submission_payload(flight_ref, lodging_ref)
    r1 = client.post(f"/api/trips/{trip_id}/submit", json=payload)
    assert r1.status_code == 200
    r2 = client.post(f"/api/trips/{trip_id}/submit", json=payload)
    assert r2.status_code == 409


def test_submit_without_legal_confirmation_rejected(client, monkeypatch):
    _verify(client, monkeypatch)
    trip_id = client.post("/api/trips/start").get_json()["trip_id"]
    payload = _submission_payload(None, None)
    payload["legal_confirmed"] = False
    r = client.post(f"/api/trips/{trip_id}/submit", json=payload)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Rate management
# ---------------------------------------------------------------------------

def test_rate_save_persists_and_logs_history(client):
    _login_admin(client)
    r = client.get("/api/admin/rates/2026")
    assert r.status_code == 200
    rates = r.get_json()["rates"]
    rates["car_cap_per_day"] = 90.0

    r = client.post("/api/admin/rates/2026", json={"rates": rates})
    assert r.status_code == 200
    history = r.get_json()["history"]
    assert any(h["field"] == "car_cap_per_day" for h in history)

    r = client.get("/api/admin/rates/2026")
    assert r.get_json()["rates"]["car_cap_per_day"] == 90.0


# ---------------------------------------------------------------------------
# FX status
# ---------------------------------------------------------------------------

def test_fx_status_reports_cached_data(client):
    _login_admin(client)
    r = client.get("/api/admin/fx-status")
    assert r.status_code == 200
    # uses the real, already-populated fx_rates.db from fx_sync.py's earlier run
    assert r.get_json()["row_counts"].get("USD", 0) > 0


# ---------------------------------------------------------------------------
# Admin auth -- regression test for a real decorator-ordering bug: @app.get
# must be the OUTER decorator and @admin_auth.require_admin the inner one, or
# Flask registers the raw, unprotected view function in its routing table
# and the auth check never actually runs despite the decorator being present
# in the source. Every /api/admin/* and /admin route must be covered here.
# ---------------------------------------------------------------------------

ADMIN_GET_ROUTES = [
    "/admin",
    "/api/admin/submissions",
    "/api/admin/submissions/SUBM-NONEXISTENT",
    "/api/admin/submissions/SUBM-NONEXISTENT/pdf",
    "/api/admin/rates/2026",
    "/api/admin/fx-status",
]


@pytest.mark.parametrize("route", ADMIN_GET_ROUTES)
def test_admin_routes_reject_unauthenticated(client, route):
    r = client.get(route)
    assert r.status_code in (302, 401), f"{route} returned {r.status_code} without an admin session"


@pytest.mark.parametrize("route", ADMIN_GET_ROUTES)
def test_admin_routes_allow_authenticated_session(client, route):
    with client.session_transaction() as sess:
        sess["admin_email"] = "office@odcpa.co.il"
    r = client.get(route)
    assert r.status_code not in (302, 401), f"{route} still blocked with a valid admin session"


def test_admin_post_routes_reject_unauthenticated(client):
    assert client.post("/api/admin/rates/2026", json={"rates": {}}).status_code == 401
    assert client.post("/api/admin/fx-sync").status_code == 401
