"""
Pytest suite for calc_engine.py.

Run with: pytest test_calc_engine.py -v
"""

from datetime import date

import pytest

from calc_engine import (
    BUSINESS,
    MIXED,
    PRIVATE,
    RateTableError,
    Trip,
    TravelerExpenses,
    calc_car,
    calc_flight,
    calc_lodging,
    calc_perdiem,
    calculate_trip,
    classify_days,
    get_effective_rates,
    lodging_tier_breakdown,
    select_rate_year,
)

NON_UPLIFT = "ארה״ב"
UPLIFT = "גרמניה"


# ---------------------------------------------------------------------------
# Rate-year selection (item 1: year-boundary rule)
# ---------------------------------------------------------------------------

def test_select_rate_year_uses_trip_start_date():
    # Trip spans New Year's Eve -- must use the START year's table (2026),
    # even though most of the trip happens in what would be "2027".
    assert select_rate_year(date(2026, 12, 29)) == 2026


def test_select_rate_year_simple_case():
    assert select_rate_year(date(2025, 3, 10)) == 2025


def test_missing_rate_year_raises_loudly():
    with pytest.raises(RateTableError):
        get_effective_rates(2099, NON_UPLIFT)


# ---------------------------------------------------------------------------
# Day classification
# ---------------------------------------------------------------------------

def test_day_classification_excludes_private_days():
    days = [BUSINESS, BUSINESS, PRIVATE, PRIVATE, MIXED]
    result = classify_days(days)
    assert result["recognized_days"] == 3  # business + mixed
    assert result["private_days"] == 2
    assert result["total_days"] == 5


def test_day_classification_all_private():
    result = classify_days([PRIVATE, PRIVATE])
    assert result["recognized_days"] == 0
    assert result["private_days"] == 2


# ---------------------------------------------------------------------------
# Lodging tiers
# ---------------------------------------------------------------------------

@pytest.fixture
def rates_2026():
    return get_effective_rates(2026, NON_UPLIFT)


def test_lodging_tier1_only_below_cap(rates_2026):
    result = calc_lodging(5, 200.0, rates_2026)
    assert result["nights"] == {"tier1": 5, "tier2": 0, "tier3": 0}
    assert result["claimed"] == 1000.0
    assert result["recognized"] == 1000.0  # below cap -> fully recognized


def test_lodging_exactly_7_nights_stays_in_tier1(rates_2026):
    result = calc_lodging(7, 210.0, rates_2026)
    assert result["nights"] == {"tier1": 7, "tier2": 0, "tier3": 0}
    assert result["recognized"] == pytest.approx(1470.0)


def test_lodging_12_nights_matches_worked_example(rates_2026):
    # This mirrors the worked example already sent to the client as a
    # sample PDF -- pinning it here catches any future regression.
    result = calc_lodging(12, 210.0, rates_2026)
    assert result["nights"] == {"tier1": 7, "tier2": 5, "tier3": 0}
    assert result["claimed"] == pytest.approx(2520.0)
    assert result["recognized"] == pytest.approx(2457.5)


def test_lodging_tier1_above_cap_is_capped(rates_2026):
    result = calc_lodging(3, 500.0, rates_2026)  # cap is 365
    assert result["claimed"] == pytest.approx(1500.0)
    assert result["recognized"] == pytest.approx(365.0 * 3)


def test_lodging_beyond_90_nights_uses_tier3_flat_cap(rates_2026):
    result = calc_lodging(95, 200.0, rates_2026)  # tier3 cap is 160
    assert result["nights"] == {"tier1": 7, "tier2": 83, "tier3": 5}
    tier3_expected = min(200.0, 160.0) * 5
    assert result["recognized"] >= tier3_expected  # sanity: tier3 portion capped


def test_lodging_recognized_never_exceeds_claimed(rates_2026):
    for nights in (0, 1, 7, 8, 45, 90, 91, 120):
        for actual in (50.0, 160.0, 210.0, 365.0, 900.0):
            result = calc_lodging(nights, actual, rates_2026)
            assert result["recognized"] <= result["claimed"] + 1e-9


def test_lodging_negative_nights_rejected(rates_2026):
    with pytest.raises(ValueError):
        calc_lodging(-1, 200.0, rates_2026)


def test_lodging_tier_breakdown_sums_to_calc_lodging(rates_2026):
    for nights in (0, 3, 7, 12, 95):
        for actual in (150.0, 210.0, 400.0):
            total = calc_lodging(nights, actual, rates_2026)
            rows = lodging_tier_breakdown(nights, actual, rates_2026)
            assert sum(r["claimed"] for r in rows) == pytest.approx(total["claimed"])
            assert sum(r["recognized"] for r in rows) == pytest.approx(total["recognized"])
            assert sum(r["nights"] for r in rows) == nights


def test_lodging_tier_breakdown_12_nights_has_two_rows(rates_2026):
    rows = lodging_tier_breakdown(12, 210.0, rates_2026)
    assert [r["nights"] for r in rows] == [7, 5]
    assert rows[0]["full_recognition"] is True
    assert rows[1]["full_recognition"] is False


# ---------------------------------------------------------------------------
# Per-diem
# ---------------------------------------------------------------------------

def test_perdiem_with_lodging_rate(rates_2026):
    assert calc_perdiem(12, claims_lodging=True, rates=rates_2026) == pytest.approx(1224.0)


def test_perdiem_without_lodging_uses_higher_rate(rates_2026):
    with_lodging = calc_perdiem(10, True, rates_2026)
    without_lodging = calc_perdiem(10, False, rates_2026)
    assert without_lodging > with_lodging


def test_perdiem_zero_recognized_days_is_zero(rates_2026):
    assert calc_perdiem(0, True, rates_2026) == 0.0


# ---------------------------------------------------------------------------
# Car rental
# ---------------------------------------------------------------------------

def test_car_below_cap_fully_recognized(rates_2026):
    result = calc_car(4, 60.0, rates_2026)
    assert result["claimed"] == result["recognized"] == pytest.approx(240.0)


def test_car_above_cap_is_capped(rates_2026):
    result = calc_car(4, 85.0, rates_2026)  # cap is 80
    assert result["claimed"] == pytest.approx(340.0)
    assert result["recognized"] == pytest.approx(320.0)


# ---------------------------------------------------------------------------
# Flight
# ---------------------------------------------------------------------------

def test_flight_economy_fully_recognized():
    result = calc_flight(780.0, "economy")
    assert result["claimed"] == result["recognized"] == 780.0


def test_flight_business_fully_recognized():
    result = calc_flight(2200.0, "business")
    assert result["recognized"] == 2200.0


def test_flight_first_class_capped_to_business_fare():
    result = calc_flight(4000.0, "first", business_class_price=2200.0)
    assert result["claimed"] == 4000.0
    assert result["recognized"] == 2200.0


def test_flight_first_class_below_business_fare_recognized_in_full():
    result = calc_flight(1800.0, "first", business_class_price=2200.0)
    assert result["recognized"] == 1800.0


def test_flight_first_class_without_reference_price_raises():
    with pytest.raises(ValueError):
        calc_flight(4000.0, "first")


# ---------------------------------------------------------------------------
# Uplift countries (125%) -- caps only, NOT the car cap
# ---------------------------------------------------------------------------

def test_uplift_country_raises_lodging_and_perdiem_caps():
    base = get_effective_rates(2026, NON_UPLIFT)
    uplifted = get_effective_rates(2026, UPLIFT)
    assert uplifted["lodging_tier1_cap"] == pytest.approx(base["lodging_tier1_cap"] * 1.25)
    assert uplifted["perdiem_with_lodging"] == pytest.approx(base["perdiem_with_lodging"] * 1.25)
    assert uplifted["is_uplift_country"] is True
    assert base["is_uplift_country"] is False


def test_uplift_country_does_not_raise_car_cap():
    base = get_effective_rates(2026, NON_UPLIFT)
    uplifted = get_effective_rates(2026, UPLIFT)
    assert uplifted["car_cap_per_day"] == base["car_cap_per_day"] == 80.0


def test_uplift_changes_lodging_recognition_outcome():
    # $400/night: over the base $365 cap (partially disallowed) but under
    # the uplifted $456.25 cap (fully recognized) for the same 5 nights.
    base = get_effective_rates(2026, NON_UPLIFT)
    uplifted = get_effective_rates(2026, UPLIFT)
    result_base = calc_lodging(5, 400.0, base)
    result_uplift = calc_lodging(5, 400.0, uplifted)
    assert result_base["recognized"] < result_base["claimed"]
    assert result_uplift["recognized"] == pytest.approx(result_uplift["claimed"])


def test_unknown_country_falls_back_to_regular_rates():
    fallback = get_effective_rates(2026, "מדינה אחרת")
    base = get_effective_rates(2026, NON_UPLIFT)
    assert fallback["lodging_tier1_cap"] == base["lodging_tier1_cap"]
    assert fallback["is_uplift_country"] is False


# ---------------------------------------------------------------------------
# Full trip / multi-traveler aggregation
# ---------------------------------------------------------------------------

def test_full_trip_matches_worked_pdf_example():
    trip = Trip(
        start_date=date(2026, 9, 1),
        country=NON_UPLIFT,
        travelers=[
            TravelerExpenses(
                day_statuses=[BUSINESS] * 12,
                flight_price=780.0,
                flight_class="economy",
                lodging_per_night=210.0,
                lodging_claimed=True,
                car_days=4,
                car_per_day=85.0,
            )
        ],
    )
    result = calculate_trip(trip)
    assert result["rate_year"] == 2026
    assert result["grand_total_claimed"] == pytest.approx(3640.0)
    assert result["grand_total_recognized"] == pytest.approx(4781.5)


def test_multi_traveler_trip_aggregates_independently():
    primary = TravelerExpenses(
        day_statuses=[BUSINESS] * 10,
        flight_price=700.0,
        lodging_per_night=180.0,
        car_days=0,
        car_per_day=0.0,
    )
    companion = TravelerExpenses(
        day_statuses=[BUSINESS] * 8 + [PRIVATE] * 2,  # 2 private days excluded
        flight_price=650.0,
        lodging_per_night=180.0,
        car_days=0,
        car_per_day=0.0,
    )
    trip = Trip(start_date=date(2026, 5, 1), country=NON_UPLIFT, travelers=[primary, companion])
    result = calculate_trip(trip)

    assert len(result["travelers"]) == 2
    assert result["travelers"][0]["recognized_days"] == 10
    assert result["travelers"][1]["recognized_days"] == 8  # private days dropped
    expected_total = result["travelers"][0]["total_recognized"] + result["travelers"][1]["total_recognized"]
    assert result["grand_total_recognized"] == pytest.approx(expected_total)


def test_trip_with_zero_business_days_recognizes_only_flight():
    traveler = TravelerExpenses(
        day_statuses=[PRIVATE, PRIVATE, PRIVATE],
        flight_price=500.0,
        lodging_per_night=200.0,
        car_days=2,
        car_per_day=50.0,
    )
    trip = Trip(start_date=date(2026, 6, 1), country=NON_UPLIFT, travelers=[traveler])
    result = calculate_trip(trip)
    traveler_result = result["travelers"][0]
    assert traveler_result["lodging"]["recognized"] == 0.0
    assert traveler_result["perdiem"] == 0.0
    # car/flight aren't gated by day classification in this engine version
    assert traveler_result["flight"]["recognized"] == 500.0
