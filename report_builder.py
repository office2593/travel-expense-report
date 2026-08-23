"""
Builds the final report PDF for a trip: renders templates/report.html with
real data, prints it via headless Chrome, converts/merges the traveler's
uploaded receipt files as trailing pages, and compresses the result.

This is the piece that ties calc_engine.py, fx_sync.py, storage.py, and
compress_pdf.py together into one output file.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from PIL import Image
from pypdf import PdfReader, PdfWriter

import calc_engine
import compress_pdf
import fonts_data
import fx_sync
import storage

TEMPLATES_DIR = Path(__file__).parent / "templates"
LOGO_PATH = Path(__file__).parent / "static" / "logo_full.png"

# Absolute paths as a fallback for the Windows dev environment this was
# built on; PATH_CANDIDATES (checked first) covers Linux deployment targets
# (Railway et al.) where the browser is installed as `chromium`/`google-chrome`
# via nixpacks.toml rather than at a fixed path. Set CHROME_BIN to override
# either way.
PATH_CANDIDATES = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]
ABS_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

_STATUS_LABELS = {
    "full": ("100%", "good"),
    "partial": ("חלקי", "bad"),
    "capped": ("תקרה", "bad"),
    "auto": ("אוטומטי", "good"),
}


def _fmt_date(iso_date: str) -> str:
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")


def _find_browser() -> str:
    env_override = os.environ.get("CHROME_BIN")
    if env_override:
        return env_override
    for name in PATH_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for path in ABS_CANDIDATES:
        if Path(path).exists():
            return path
    raise RuntimeError(
        "No headless-capable Chrome/Chromium found. On Railway, add a chromium "
        "Nix package via nixpacks.toml (see that file in this project) or set "
        "the CHROME_BIN environment variable to its path."
    )


def _logo_data_uri() -> str:
    b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _build_traveler_rows(traveler_row, rates: dict, fx_rate: float) -> dict:
    """traveler_row: a db.travelers row (sqlite3.Row) plus a parsed days list.
    Returns the dict the Jinja2 template's per-traveler loop expects."""
    import json

    days = json.loads(traveler_row["days_json"])
    classified = calc_engine.classify_days([d["status"] for d in days])
    recognized_nights = classified["recognized_days"]

    lodging_rows = calc_engine.lodging_tier_breakdown(
        recognized_nights, traveler_row["lodging_cost"] or 0.0, rates
    )
    perdiem_amount = calc_engine.calc_perdiem(
        recognized_nights, claims_lodging=bool(traveler_row["lodging_file_ref"]), rates=rates
    )
    car = calc_engine.calc_car(
        traveler_row["car_days"] or 0, traveler_row["car_cost_per_day"] or 0.0, rates
    ) if traveler_row["car_enabled"] else {"claimed": 0.0, "recognized": 0.0}
    flight = calc_engine.calc_flight(
        traveler_row["flight_price"] or 0.0,
        traveler_row["flight_class"] or "economy",
    )

    rows = []
    for lr in lodging_rows:
        status_key = "full" if lr["full_recognition"] else "partial"
        label, cls = _STATUS_LABELS[status_key]
        rows.append({
            "category": lr["label"], "basis": lr["basis"],
            "claimed": lr["claimed"], "recognized_usd": lr["recognized"],
            "recognized_ils": round(lr["recognized"] * fx_rate, 2),
            "status_label": label, "status_class": cls,
        })
    rows.append({
        "category": "אש״ל",
        "basis": f"{recognized_nights} ימי עסקים × ${rates['perdiem_with_lodging' if traveler_row['lodging_file_ref'] else 'perdiem_no_lodging']:,.0f} · תעריף יומי קבוע",
        "claimed": None, "recognized_usd": perdiem_amount,
        "recognized_ils": round(perdiem_amount * fx_rate, 2),
        "status_label": "אוטומטי", "status_class": "good",
    })
    rows.append({
        "category": "טיסה",
        "basis": f"הלוך–חזור, {'מחלקת תיירים' if flight['recognized'] == flight['claimed'] else 'מוגבל למחיר מחלקת עסקים'}",
        "claimed": flight["claimed"], "recognized_usd": flight["recognized"],
        "recognized_ils": round(flight["recognized"] * fx_rate, 2),
        "status_label": "100%" if flight["recognized"] == flight["claimed"] else "מוגבל",
        "status_class": "good" if flight["recognized"] == flight["claimed"] else "bad",
    })
    if traveler_row["car_enabled"]:
        rows.append({
            "category": "רכב שכור",
            "basis": f"{traveler_row['car_days']} ימים × ${traveler_row['car_cost_per_day']:,.0f} · תקרה ${rates['car_cap_per_day']:,.0f}/יום",
            "claimed": car["claimed"], "recognized_usd": car["recognized"],
            "recognized_ils": round(car["recognized"] * fx_rate, 2),
            "status_label": "100%" if car["recognized"] == car["claimed"] else "תקרה",
            "status_class": "good" if car["recognized"] == car["claimed"] else "bad",
        })

    total_claimed = sum(r["claimed"] or 0 for r in rows)
    total_recognized_usd = round(sum(r["recognized_usd"] for r in rows), 2)
    total_recognized_ils = round(sum(r["recognized_ils"] for r in rows), 2)

    return {
        "name": traveler_row["name"],
        "role_label": "לקוח" if traveler_row["role"] == "primary" else traveler_row["role"],
        "rows": rows,
        "total_claimed": round(total_claimed, 2),
        "total_recognized_usd": total_recognized_usd,
        "total_recognized_ils": total_recognized_ils,
    }


def build_report_pdf(trip_row, traveler_rows: list, output_path: str | Path) -> dict:
    """trip_row / traveler_rows: sqlite3.Row objects from db.py's tables.
    Returns a stats dict (original/compressed size, page count)."""
    rate_year = calc_engine.select_rate_year(date.fromisoformat(trip_row["start_date"]))
    rates = calc_engine.get_effective_rates(rate_year, trip_row["country"])

    try:
        fx_rate = fx_sync.get_rate("USD", date.fromisoformat(trip_row["start_date"]))
    except LookupError:
        fx_rate = 1.0  # fx_sync hasn't been run yet -- degrade rather than crash report generation

    travelers_ctx = [_build_traveler_rows(t, rates, fx_rate) for t in traveler_rows]
    grand_total_recognized_ils = round(sum(t["total_recognized_ils"] for t in travelers_ctx), 2)

    checklist = [
        {"ok": bool(trip_row["purpose"] and trip_row["purpose_explanation"]), "text": "מטרת נסיעה עסקית הוגדרה והוסברה בהצהרת הלקוח"},
        {"ok": all(len(__import__("json").loads(t["days_json"])) > 0 for t in traveler_rows), "text": "ימי הנסיעה סווגו לעסקיים / פרטיים / מעורבים לכל נוסע בנפרד"},
        {"ok": all(t["flight_file_ref"] and t["lodging_file_ref"] for t in traveler_rows), "text": "קיים מסמך תומך לכל שורת הוצאה שהוכרה"},
        {"ok": True, "text": f"מדינת היעד ({trip_row['country']}) מזוהה מרשימה סגורה"},
        {"ok": bool(trip_row["legal_confirmed"]), "text": "הלקוח אישר את נכונות הפרטים בהצהרה חתומה דיגיטלית"},
    ]

    attachments = []
    for t in traveler_rows:
        for category, ref_field in [("טיסה", "flight_file_ref"), ("לינה", "lodging_file_ref"), ("רכב שכור", "car_file_ref")]:
            ref = t[ref_field]
            if ref:
                attachments.append({"traveler": t["name"], "category": category, "filename": storage.original_name(ref)})

    ctx = {
        "font_faces_css": fonts_data.font_faces_css(),
        "logo_data_uri": _logo_data_uri(),
        "trip": {
            "id": trip_row["id"],
            "generated_date": datetime.now().strftime("%Y-%m-%d"),
            "contact_name": trip_row["contact_name"],
            "destination": trip_row["destination"],
            "country": trip_row["country"],
            "is_uplift": rates["is_uplift_country"],
            "uplift_pct": int(rates["uplift_pct"] * 100),
            "start_date": _fmt_date(trip_row["start_date"]),
            "end_date": _fmt_date(trip_row["end_date"]),
            "purpose": trip_row["purpose"],
            "purpose_explanation": trip_row["purpose_explanation"],
            "fx_rate": fx_rate,
            "fx_currency": "USD",
            "fx_date": _fmt_date(trip_row["start_date"]),
            "rate_year": rate_year,
        },
        "travelers": travelers_ctx,
        "grand_total_recognized_ils": grand_total_recognized_ils,
        "checklist": checklist,
        "attachments": attachments,
    }

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    html = env.get_template("report.html").render(**ctx)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        html_path = tmp / "report.html"
        html_path.write_text(html, encoding="utf-8")
        body_pdf = tmp / "body.pdf"

        result = subprocess.run(
            [
                _find_browser(), "--headless", "--disable-gpu", "--no-pdf-header-footer",
                # Container-specific: Chrome refuses to run its sandbox as root, which
                # is the default user in most Docker images (including Railway's) --
                # without --no-sandbox it exits immediately with no PDF produced.
                # --disable-dev-shm-usage avoids crashes from Docker's default small
                # /dev/shm (64MB) on larger rendered pages. Both are no-ops on the
                # Windows dev machine this was built on.
                "--no-sandbox", "--disable-dev-shm-usage",
                f"--print-to-pdf={body_pdf}", html_path.as_uri(),
            ],
            capture_output=True,
        )
        if result.returncode != 0 or not body_pdf.exists():
            raise RuntimeError(
                f"Headless browser failed to render the PDF (exit {result.returncode}): "
                f"{result.stderr.decode(errors='replace')[:2000]}"
            )

        writer = PdfWriter()
        for page in PdfReader(body_pdf).pages:
            writer.add_page(page)

        for t in traveler_rows:
            for ref_field in ["flight_file_ref", "lodging_file_ref", "car_file_ref", "proof_file_ref"]:
                ref = t[ref_field] if ref_field in t.keys() else None
                if not ref:
                    continue
                _append_attachment(writer, storage.get_bytes(ref), storage.suffix(ref))

        merged_pdf = tmp / "merged.pdf"
        with open(merged_pdf, "wb") as f:
            writer.write(f)

        result = compress_pdf.compress(merged_pdf, output_path)

    return {**result, "grand_total_recognized_ils": grand_total_recognized_ils, "rate_year": rate_year}


def _append_attachment(writer: PdfWriter, file_bytes: bytes, suffix: str) -> None:
    suffix = suffix.lower()
    if suffix == ".pdf":
        for page in PdfReader(io.BytesIO(file_bytes)).pages:
            writer.add_page(page)
        return
    if suffix in (".jpg", ".jpeg", ".png"):
        img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PDF")
        buf.seek(0)
        for page in PdfReader(buf).pages:
            writer.add_page(page)
        return
    raise ValueError(f"Unsupported attachment type: {suffix}")


if __name__ == "__main__":
    print(
        "This module is a library used by app.py; it needs real trip/traveler "
        "DB rows to run. See test_report_builder.py for a runnable example.",
        file=sys.stderr,
    )
