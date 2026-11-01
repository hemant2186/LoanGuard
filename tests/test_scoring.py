"""Unit tests for the LoanGuard risk scoring engine.

Covers: score calculation, clamping, mutually-exclusive complaint thresholds,
RBI-directory-unavailable vs confirmed-absence distinction, missing fields,
and the demo-mode sample scenario.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.scoring import Indicators, compute_score, verdict_color
from services.config import VERDICT_HIGH, VERDICT_LOW, VERDICT_UNCLEAR


def score(**kw) -> dict:
    return compute_score(Indicators(**kw))


def total_points(res) -> int:
    return sum(d["points"] for d in res["deductions"])


# --- Basic scoring -------------------------------------------------------- #
def test_perfect_app_scores_100_lower_risk():
    res = score(rbi_checked=True, rbi_found=True, grievance_found=True,
                lending_partner_found=True, address_found=True,
                interest_rate_found=True, complaint_count=0)
    assert res["score"] == 100
    assert res["verdict"] == VERDICT_LOW
    assert res["deductions"] == []


def test_not_in_rbi_deducts_40():
    res = score(rbi_checked=True, rbi_found=False)
    assert res["score"] == 100 - 40 - 10 - 15 - 5 - 10  # no complaints
    assert any(d["points"] == 40 for d in res["deductions"])


def test_rbi_unavailable_deducts_15_not_40():
    """A failed lookup must NOT be treated as confirmed absence."""
    res = score(rbi_checked=False, rbi_found=False)
    assert res["score"] == 100 - 15 - 10 - 15 - 5 - 10
    # only the 15 'could not be checked' deduction, never the 40
    assert any(d["points"] == 15 for d in res["deductions"])
    assert not any(d["points"] == 40 for d in res["deductions"])


def test_rbi_unavailable_found_stays_false_guard():
    """Even if a buggy caller passes found=True with checked=False, score must
    not apply the confirmed-absence 40 (the agent service coerces found, and
    scoring only subtracts 40 when checked=True AND not found)."""
    res = score(rbi_checked=False, rbi_found=True)
    assert not any(d["points"] == 40 for d in res["deductions"])
    assert any(d["points"] == 15 for d in res["deductions"])


# --- Complaint thresholds (mutually exclusive) ---------------------------- #
def test_complaint_threshold_8_deducts_25():
    res = score(complaint_count=9)
    assert any(d["points"] == 25 for d in res["deductions"])
    assert not any(d["points"] == 12 for d in res["deductions"])


def test_complaint_threshold_3_deducts_12():
    res = score(complaint_count=4)
    assert any(d["points"] == 12 for d in res["deductions"])
    assert not any(d["points"] == 25 for d in res["deductions"])


def test_complaint_below_3_no_deduction():
    res = score(complaint_count=2)
    assert not any(d["points"] in (12, 25) for d in res["deductions"])


def test_complaint_thresholds_mutually_exclusive():
    for c in range(3, 8):
        res = score(complaint_count=c)
        pts = {d["points"] for d in res["deductions"]}
        assert pts.isdisjoint({25})  # never both
    for c in range(8, 15):
        res = score(complaint_count=c)
        pts = {d["points"] for d in res["deductions"]}
        assert pts.isdisjoint({12})  # never both


# --- Clamping & verdict bands -------------------------------------------- #
def test_score_clamped_to_zero():
    res = score(rbi_checked=True, rbi_found=False, grievance_found=False,
                lending_partner_found=False, address_found=False,
                interest_rate_found=False, complaint_count=20)
    assert res["score"] == 0
    assert res["verdict"] == VERDICT_HIGH


def test_verdict_bands():
    # 100 -> LOWER (everything checks out, no complaints)
    r = score(rbi_checked=True, rbi_found=True, grievance_found=True,
              lending_partner_found=True, address_found=True,
              interest_rate_found=True, complaint_count=0)
    assert r["score"] == 100 and r["verdict"] == VERDICT_LOW

    # 70 (LOWER boundary): rbi not checked(-15) + no interest(-10) + no address(-5), rest found
    r = score(rbi_checked=False, grievance_found=True, lending_partner_found=True,
              address_found=False, interest_rate_found=False, complaint_count=0)
    assert r["score"] == 70 and r["verdict"] == VERDICT_LOW

    # 60 -> UNCLEAR: not found in RBI(-40), all transparency found
    r = score(rbi_checked=True, rbi_found=False, grievance_found=True,
              lending_partner_found=True, address_found=True,
              interest_rate_found=True, complaint_count=0)
    assert r["score"] == 60 and r["verdict"] == VERDICT_UNCLEAR

    # 45 (UNCLEAR boundary): all defaults -> 100-15-10-15-5-10 = 45
    r = score()
    assert r["score"] == 45 and r["verdict"] == VERDICT_UNCLEAR

    # 35 -> HIGH: not in RBI(-40) + no grievance(-10) + no lending partner(-15)
    r = score(rbi_checked=True, rbi_found=False, grievance_found=False,
              lending_partner_found=False, address_found=True,
              interest_rate_found=True, complaint_count=0)
    assert r["score"] == 35 and r["verdict"] == VERDICT_HIGH


def test_missing_website_all_fields_missing():
    # No website -> no transparency indicators found; RBI not checked.
    res = score()  # defaults: not checked, nothing found, 0 complaints
    assert res["score"] == 45  # 100 - 15 - 10 - 15 - 5 - 10
    assert res["verdict"] == VERDICT_UNCLEAR


def test_missing_grievance_deducts_10():
    base = score(rbi_checked=True, rbi_found=True, lending_partner_found=True,
                 address_found=True, interest_rate_found=True, complaint_count=0,
                 grievance_found=True)
    miss = score(rbi_checked=True, rbi_found=True, lending_partner_found=True,
                 address_found=True, interest_rate_found=True, complaint_count=0,
                 grievance_found=False)
    assert miss["score"] == base["score"] - 10


# --- Demo-mode sample scenario ------------------------------------------- #
def test_demo_sample_scenario_matches_high_risk():
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "demo", "sample_events.json"), encoding="utf-8") as f:
        sample = json.load(f)
    res = compute_score(Indicators(**sample["indicators"]))
    assert res["score"] == sample["score"]["score"] == 0
    assert res["verdict"] == sample["score"]["verdict"] == VERDICT_HIGH
    assert {d["points"] for d in res["deductions"]} == {40, 10, 15, 5, 10, 25}


# --- verdict_color helper ------------------------------------------------- #
def test_verdict_color_returns_expected():
    assert verdict_color(VERDICT_LOW).startswith("#")
    assert verdict_color(VERDICT_HIGH) == "#dc2626"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
