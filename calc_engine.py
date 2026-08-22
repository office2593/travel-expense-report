"""
Reconciliation calculation engine for foreign business-travel expenses,
per Israeli Income Tax Regulations (Deduction of Certain Expenses), 1972.

IMPORTANT: The figures in RATE_TABLE are illustrative/placeholder values used
to develop and test this engine. They must be verified against the current
year's official Income Tax circular (חוזר מס הכנסה) before this module is
used for real client filings. Do not treat RATE_TABLE as authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# Rate configuration (per calendar year). In production this should be loaded
# from the database table the admin "rate management" screen edits, not
# hardcoded here -- this dict is the illustrative in-code equivalent.
# ---------------------------------------------------------------------------

RATE_TABLE: dict[int, dict] = {
    2026: {
        "lodging_tier1_nights": 7,
        "lodging_tier1_cap": 365.0,
        "lodging_tier2_max_night": 90,
        "lodging_tier2_floor": 160.0,
        "lodging_tier2_pct": 0.75,
        "lodging_tier2_upper_cap": 365.0,
        "lodging_tier3_cap": 160.0,
        "perdiem_with_lodging": 102.0,
        "perdiem_no_lodging": 171.0,
        "car_cap_per_day": 80.0,  # uplift does NOT apply to this cap
        "uplift_pct": 1.25,
        "uplift_countries": {
            "אוסטרליה", "אוסטריה", "איטליה", "איסלנד", "אירלנד", "בלגיה",
            "גרמניה", "דובאי", "דנמרק", "הולנד", "הונג קונג", "בריטניה",
            "טייוואן", "יוון", "יפן", "לוקסמבורג", "נורווגיה", "ספרד",
            "עומאן", "פינלנד", "צרפת", "קטאר", "דרום קוריאה", "קנדה",
            "שוודיה", "שוויץ",
        },
    },
    # Placeholder prior-year table -- ONLY for exercising the year-selection
    # logic in tests. Not verified real 2025 figures.
    2025: {
        "lodging_tier1_nights": 7,
        "lodging_tier1_cap": 350.0,
        "lodging_tier2_max_night": 90,
        "lodging_tier2_floor": 155.0,
        "lodging_tier2_pct": 0.75,
        "lodging_tier2_upper_cap": 350.0,
        "lodging_tier3_cap": 155.0,
        "perdiem_with_lodging": 98.0,
        "perdiem_no_lodging": 165.0,
        "car_cap_per_day": 75.0,
        "uplift_pct": 1.25,
        "uplift_countries": {
            "אוסטרליה", "אוסטריה", "איטליה", "איסלנד", "אירלנד", "בלגיה",
            "גרמניה", "דובאי", "דנמרק", "הולנד", "הונג קונג", "בריטניה",
            "טייוואן", "יוון", "יפן", "לוקסמבורג", "נורווגיה", "ספרד",
            "עומאן", "פינלנד", "צרפת", "קטאר", "קנדה", "שוודיה", "שוויץ",
        },
    },
}

# Rate fields that the uplift multiplier applies to. Car rental is
# deliberately excluded -- its cap does not increase for uplift countries.
_UPLIFT_FIELDS = (
    "lodging_tier1_cap",
    "lodging_tier2_floor",
    "lodging_tier2_upper_cap",
    "lodging_tier3_cap",
    "perdiem_with_lodging",
    "perdiem_no_lodging",
)


class RateTableError(LookupError):
    """Raised when no rate table is configured for a requested year."""


def select_rate_year(trip_start_date: date) -> int:
    """A trip that crosses a calendar year boundary is calculated in full
    under the rate table of the year it STARTED in."""
    return trip_start_date.year


def get_effective_rates(year: int, country: str) -> dict:
    """Return the rate table for `year`, with the uplift multiplier applied
    to the relevant caps if `country` is on that year's uplift list. Raises
    RateTableError (loudly, on purpose) if the year has no configured table
    -- silently falling back to a neighboring year would risk misreporting."""
    if year not in RATE_TABLE:
        raise RateTableError(
            f"No rate table configured for {year}. "
            f"Add one to RATE_TABLE before calculating trips for this year."
        )
    base = RATE_TABLE[year]
    is_uplift = country in base["uplift_countries"]
    multiplier = base["uplift_pct"] if is_uplift else 1.0

    effective = dict(base)
    for f in _UPLIFT_FIELDS:
        effective[f] = base[f] * multiplier
    effective["is_uplift_country"] = is_uplift
    effective["year"] = year
    return effective


# ---------------------------------------------------------------------------
# Day classification
# ---------------------------------------------------------------------------

BUSINESS = "business"
MIXED = "mixed"
PRIVATE = "private"


def classify_days(day_statuses: list[str]) -> dict:
    """business + mixed days count toward tiering/recognition; private days
    are excluded entirely, per the eligibility threshold rule."""
    recognized_days = sum(1 for s in day_statuses if s in (BUSINESS, MIXED))
    private_days = sum(1 for s in day_statuses if s == PRIVATE)
    return {
        "recognized_days": recognized_days,
        "private_days": private_days,
        "total_days": len(day_statuses),
    }


# ---------------------------------------------------------------------------
# Category calculators
# ---------------------------------------------------------------------------

def calc_lodging(recognized_nights: int, actual_per_night: float, rates: dict) -> dict:
    """Tiered lodging recognition: nights 1-7 capped at tier1_cap (100%);
    nights 8-90 recognized in full up to tier2_floor, tier2_pct of the
    excess up to tier2_upper_cap; nights 91+ capped flat at tier3_cap."""
    if recognized_nights < 0:
        raise ValueError("recognized_nights cannot be negative")

    tier1_n = min(recognized_nights, rates["lodging_tier1_nights"])
    remaining = recognized_nights - tier1_n
    tier2_n = min(remaining, rates["lodging_tier2_max_night"] - rates["lodging_tier1_nights"])
    tier3_n = remaining - tier2_n

    tier1_claimed = actual_per_night * tier1_n
    tier1_recognized = min(actual_per_night, rates["lodging_tier1_cap"]) * tier1_n

    floor = rates["lodging_tier2_floor"]
    upper = rates["lodging_tier2_upper_cap"]
    if actual_per_night <= floor:
        tier2_per_night = actual_per_night
    else:
        excess = min(actual_per_night, upper) - floor
        tier2_per_night = floor + excess * rates["lodging_tier2_pct"]
    tier2_claimed = actual_per_night * tier2_n
    tier2_recognized = tier2_per_night * tier2_n

    tier3_per_night = min(actual_per_night, rates["lodging_tier3_cap"])
    tier3_claimed = actual_per_night * tier3_n
    tier3_recognized = tier3_per_night * tier3_n

    return {
        "claimed": round(tier1_claimed + tier2_claimed + tier3_claimed, 2),
        "recognized": round(tier1_recognized + tier2_recognized + tier3_recognized, 2),
        "nights": {"tier1": tier1_n, "tier2": tier2_n, "tier3": tier3_n},
    }


def lodging_tier_breakdown(recognized_nights: int, actual_per_night: float, rates: dict) -> list[dict]:
    """Same tier math as calc_lodging(), but returns one row per non-empty
    tier (label, nights, claimed, recognized, full_recognition: bool) for
    itemized display in the PDF report. calc_lodging() stays the source of
    truth for totals; this must always sum to the same claimed/recognized
    -- see test_lodging_tier_breakdown_sums_to_calc_lodging."""
    tier1_n = min(recognized_nights, rates["lodging_tier1_nights"])
    remaining = recognized_nights - tier1_n
    tier2_n = min(remaining, rates["lodging_tier2_max_night"] - rates["lodging_tier1_nights"])
    tier3_n = remaining - tier2_n

    rows = []
    if tier1_n:
        recognized = min(actual_per_night, rates["lodging_tier1_cap"]) * tier1_n
        claimed = actual_per_night * tier1_n
        rows.append({
            "label": f"לינה — לילות 1–{tier1_n}",
            "basis": f"{tier1_n} לילות × ${actual_per_night:,.0f} · תקרה ${rates['lodging_tier1_cap']:,.0f}/לילה (100%)",
            "nights": tier1_n, "claimed": round(claimed, 2), "recognized": round(recognized, 2),
            "full_recognition": recognized >= claimed - 1e-9,
        })
    if tier2_n:
        floor = rates["lodging_tier2_floor"]
        upper = rates["lodging_tier2_upper_cap"]
        if actual_per_night <= floor:
            per_night = actual_per_night
        else:
            excess = min(actual_per_night, upper) - floor
            per_night = floor + excess * rates["lodging_tier2_pct"]
        claimed = actual_per_night * tier2_n
        recognized = per_night * tier2_n
        start = tier1_n + 1
        end = tier1_n + tier2_n
        rows.append({
            "label": f"לינה — לילות {start}–{end}",
            "basis": f"{tier2_n} לילות × ${actual_per_night:,.0f} · עד ${floor:,.0f} מוכר במלואו, מעל זה {int(rates['lodging_tier2_pct']*100)}%",
            "nights": tier2_n, "claimed": round(claimed, 2), "recognized": round(recognized, 2),
            "full_recognition": recognized >= claimed - 1e-9,
        })
    if tier3_n:
        cap = rates["lodging_tier3_cap"]
        per_night = min(actual_per_night, cap)
        claimed = actual_per_night * tier3_n
        recognized = per_night * tier3_n
        start = tier1_n + tier2_n + 1
        rows.append({
            "label": f"לינה — מלילה {start} ואילך",
            "basis": f"{tier3_n} לילות × ${actual_per_night:,.0f} · תקרה ${cap:,.0f}/לילה",
            "nights": tier3_n, "claimed": round(claimed, 2), "recognized": round(recognized, 2),
            "full_recognition": recognized >= claimed - 1e-9,
        })
    return rows


def calc_perdiem(recognized_days: int, claims_lodging: bool, rates: dict) -> float:
    rate = rates["perdiem_with_lodging"] if claims_lodging else rates["perdiem_no_lodging"]
    return round(rate * recognized_days, 2)


def calc_car(days: int, actual_per_day: float, rates: dict) -> dict:
    cap = rates["car_cap_per_day"]
    recognized_per_day = min(actual_per_day, cap)
    return {
        "claimed": round(actual_per_day * days, 2),
        "recognized": round(recognized_per_day * days, 2),
    }


def calc_flight(price: float, flight_class: str, business_class_price: Optional[float] = None) -> dict:
    """Economy/business recognized in full; first class capped at the
    business-class fare on the same flight."""
    if flight_class == "first":
        if business_class_price is None:
            raise ValueError(
                "business_class_price is required to cap a first-class fare"
            )
        recognized = min(price, business_class_price)
    else:
        recognized = price
    return {"claimed": round(price, 2), "recognized": round(recognized, 2)}


# ---------------------------------------------------------------------------
# Per-traveler / per-trip orchestration
# ---------------------------------------------------------------------------

@dataclass
class TravelerExpenses:
    day_statuses: list[str]
    flight_price: float
    flight_class: str = "economy"
    flight_business_class_price: Optional[float] = None
    lodging_per_night: float = 0.0
    lodging_claimed: bool = True  # whether lodging is claimed with receipts (affects perdiem rate)
    car_days: int = 0
    car_per_day: float = 0.0


@dataclass
class Trip:
    start_date: date
    country: str
    travelers: list[TravelerExpenses] = field(default_factory=list)


def calculate_traveler(traveler: TravelerExpenses, rates: dict) -> dict:
    days = classify_days(traveler.day_statuses)
    recognized_nights = days["recognized_days"]

    lodging = calc_lodging(recognized_nights, traveler.lodging_per_night, rates)
    perdiem = calc_perdiem(recognized_nights, traveler.lodging_claimed, rates)
    car = calc_car(traveler.car_days, traveler.car_per_day, rates)
    flight = calc_flight(traveler.flight_price, traveler.flight_class, traveler.flight_business_class_price)

    claimed = lodging["claimed"] + car["claimed"] + flight["claimed"]
    recognized = lodging["recognized"] + perdiem + car["recognized"] + flight["recognized"]

    return {
        "recognized_days": recognized_nights,
        "private_days": days["private_days"],
        "lodging": lodging,
        "perdiem": perdiem,
        "car": car,
        "flight": flight,
        "total_claimed": round(claimed, 2),
        "total_recognized": round(recognized, 2),
    }


def calculate_trip(trip: Trip) -> dict:
    year = select_rate_year(trip.start_date)
    rates = get_effective_rates(year, trip.country)

    per_traveler = [calculate_traveler(t, rates) for t in trip.travelers]
    grand_claimed = round(sum(t["total_claimed"] for t in per_traveler), 2)
    grand_recognized = round(sum(t["total_recognized"] for t in per_traveler), 2)

    return {
        "rate_year": year,
        "is_uplift_country": rates["is_uplift_country"],
        "travelers": per_traveler,
        "grand_total_claimed": grand_claimed,
        "grand_total_recognized": grand_recognized,
    }
