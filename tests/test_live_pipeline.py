"""Comprehensive test suite for LoanGuard LIVE investigation mode (DEMO_MODE=0).

Validates all Section 13 requirements:
1. DEMO_MODE=1 uses sample fixtures.
2. DEMO_MODE=0 never loads sample fixtures.
3. Live Search results are processed and displayed.
4. Fetch failure is handled correctly.
5. Agent timeout does not become confirmed absence.
6. Missing fields are only scored when actually inspected.
7. Browser failures are handled gracefully.
8. Research pending state is accurate and non-blocking.
9. Monitor creation happens only on demand (button click).
10. JSON reports contain the actual investigation results.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app, investigate_stream, _build_indicators
from services.scoring import Indicators, compute_score
from services.tinyfish_search import default_queries, identify_official_website
from services.tinyfish_agent import _normalize


@pytest.fixture
def client():
    return TestClient(app)


# 1. DEMO_MODE=1 uses sample fixtures
def test_demo_mode_uses_sample_fixtures(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    client = TestClient(app)
    response = client.get("/api/investigate?name=SomeRealApp")
    assert response.status_code == 200
    lines = [line.strip() for line in response.text.split("\n") if line.startswith("data: ")]
    events = [json.loads(line.removeprefix("data: ")) for line in lines]
    final_event = next(e for e in events if e.get("step") == "final")
    rep = final_event["report"]
    assert rep["demo"] is True
    # Name is clamped to fictional SampleLoan Demo
    assert rep["app_name"] == "SampleLoan Demo"
    assert "SIMULATED RBI RESULT" in rep["rbi"]["status"]


def test_service_events_have_shared_status_contract(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    response = TestClient(app).get("/api/investigate?name=SampleLoan+Demo")
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    service_event = next(event for event in events if event["step"] == "search")
    assert service_event["type"] == "service_status"
    assert service_event["service"] == "search"
    assert service_event["status"] == "completed"


# 2. DEMO_MODE=0 never loads sample fixtures
@pytest.mark.asyncio
async def test_live_mode_never_loads_sample_fixtures(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    mock_search = {
        "ok": True,
        "results": [{"title": "KreditBee Real", "url": "https://www.kreditbee.in", "snippet": "Instant personal loans"}],
        "queries": [],
        "sources": ["https://www.kreditbee.in"],
        "identified_site": "https://www.kreditbee.in",
    }
    mock_rbi = {
        "ok": True,
        "status": "completed",
        "checked": True,
        "found": True,
        "matched_name": "KreditBee",
        "lending_entity": "Krazybee Services Private Limited",
        "evidence": "Listed as DLA for Krazybee Services Private Limited",
        "failure_reason": "",
    }
    mock_fetch = {
        "ok": True,
        "results": [{"url": "https://www.kreditbee.in", "title": "KreditBee Official", "text": "grievance officer address apr"}],
        "errors": [],
        "indicators": {
            "grievance_found": True,
            "lending_partner_found": True,
            "address_found": True,
            "interest_rate_found": True,
            "contact_found": True,
            "complaint_count": 0,
        },
    }
    mock_browser = {"ok": True, "status": "completed", "screenshot_url": "/static/evidence/live_test.png"}
    mock_research = {"ok": True, "status": "completed", "report": "Live verified report.", "citations": []}

    with patch("main._load_sample", side_effect=AssertionError("_load_sample must NEVER be called in DEMO_MODE=0")), \
         patch("main.search_loan_app", AsyncMock(return_value=mock_search)), \
         patch("main.check_rbi_directory", AsyncMock(return_value=mock_rbi)), \
         patch("main.fetch_pages", AsyncMock(return_value=mock_fetch)), \
         patch("main.capture_screenshot", AsyncMock(return_value=mock_browser)), \
         patch("main.run_research", AsyncMock(return_value=mock_research)):

        events = []
        async for chunk in investigate_stream("KreditBee", "https://www.kreditbee.in"):
            data = json.loads(chunk.removeprefix("data: ").strip())
            events.append(data)

        final_event = next(e for e in events if e.get("step") == "final")
        rep = final_event["report"]
        assert rep["demo"] is False
        assert rep["app_name"] == "KreditBee"
        assert rep["site"] == "https://www.kreditbee.in"
        assert rep["service_statuses"]["monitor"] == "not_started"
        assert rep["rbi"]["found"] is True
        assert rep["score"]["score"] == 100


# 3. Live Search results are displayed and identify official domains
def test_identify_official_website_from_search():
    results = [
        {"title": "RBI DLA Notice", "url": "https://www.rbi.org.in/scripts/dla.aspx"},
        {"title": "LinkedIn Discussion", "url": "https://www.linkedin.com/posts/kreditbee-app"},
        {"title": "KreditBee Official Portal", "url": "https://www.kreditbee.in/personal-loan"},
        {"title": "Consumer Complaints", "url": "https://www.consumercomplaints.in/kreditbee"},
    ]
    site = identify_official_website("KreditBee", results)
    assert site == "https://www.kreditbee.in"


def test_search_queries_include_supplied_official_site():
    queries = default_queries("KreditBee", "https://www.kreditbee.in")
    assert "https://www.kreditbee.in" in queries[0]


def test_agent_non_completed_result_cannot_confirm_directory_check():
    result = _normalize(
        {"checked": True, "found": True, "matched_name": "Fake", "lending_entity": "Unknown"},
        "FAILED",
        "Unauthorized Access",
    )
    assert result["checked"] is False
    assert result["found"] is False
    assert "did not complete" in result["failure_reason"]


# 4. Fetch failure is handled correctly
@pytest.mark.asyncio
async def test_fetch_failure_handled_gracefully(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    mock_search = {"ok": True, "results": [{"url": "https://unreachable.site"}], "sources": []}
    mock_rbi = {"ok": True, "checked": False, "found": False, "failure_reason": "Timeout"}
    mock_fetch = {"ok": False, "error": "Connection timed out", "results": [], "errors": [{"error": "Timeout"}]}
    mock_browser = {"ok": False, "status": "failed", "error": "Page could not load"}
    mock_research = {"ok": False, "status": "pending", "error": "Budget exceeded"}

    with patch("main.search_loan_app", AsyncMock(return_value=mock_search)), \
         patch("main.check_rbi_directory", AsyncMock(return_value=mock_rbi)), \
         patch("main.fetch_pages", AsyncMock(return_value=mock_fetch)), \
         patch("main.capture_screenshot", AsyncMock(return_value=mock_browser)), \
         patch("main.run_research", AsyncMock(return_value=mock_research)):

        events = []
        async for chunk in investigate_stream("UnreachableApp", None):
            data = json.loads(chunk.removeprefix("data: ").strip())
            events.append(data)

        fetch_event = [e for e in events if e.get("step") == "fetch"][-1]
        assert fetch_event["status"] == "failed"
        final_event = next(e for e in events if e.get("step") == "final")
        assert final_event["report"]["fetch"]["ok"] is False


# 5. Agent timeout does not become confirmed absence
def test_agent_timeout_does_not_become_confirmed_absence():
    ind = Indicators(rbi_checked=False, rbi_found=False, fetch_checked=True,
                     grievance_found=True, lending_partner_found=True, address_found=True, interest_rate_found=True)
    res = compute_score(ind)
    assert res["score"] == 85  # 100 - 15 (lookup unavailable), NOT -40
    assert not any(d["points"] == 40 for d in res["deductions"])
    assert any(d["points"] == 15 for d in res["deductions"])
    assert res["verdict"] == "UNCLEAR"


def test_confirmed_rbi_presence_can_support_lower_risk():
    result = compute_score(Indicators(
        rbi_checked=True,
        rbi_found=True,
        fetch_checked=True,
        grievance_found=True,
        lending_partner_found=True,
        address_found=True,
        interest_rate_found=True,
    ))
    assert result["verdict"] == "LOWER RISK"
    assert result["confidence"] == "high"


# 6. Missing fields are only scored when actually inspected
def test_missing_fields_only_scored_when_inspected():
    # If fetch succeeded (fetch_checked=True) but fields missing -> deductions apply
    ind_inspected = Indicators(rbi_checked=True, rbi_found=True, fetch_checked=True,
                               grievance_found=False, lending_partner_found=False,
                               address_found=False, interest_rate_found=False)
    score_inspected = compute_score(ind_inspected)
    # Deductions: 10 + 15 + 5 + 10 = 40
    assert score_inspected["score"] == 60

    # If fetch failed (fetch_checked=False) -> do NOT assume missing fields are absent
    ind_uninspected = Indicators(rbi_checked=True, rbi_found=True, fetch_checked=False,
                                 grievance_found=False, lending_partner_found=False,
                                 address_found=False, interest_rate_found=False)
    score_uninspected = compute_score(ind_uninspected)
    assert score_uninspected["score"] == 100  # no missing-fields deductions applied


# 7. Browser failures are handled gracefully
@pytest.mark.asyncio
async def test_browser_failure_handled(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    mock_search = {"ok": True, "results": [], "sources": []}
    mock_rbi = {"ok": True, "checked": False, "found": False}
    mock_fetch = {"ok": False, "results": []}
    mock_browser = {"ok": False, "status": "failed", "error": "Chrome CDP disconnected"}
    mock_research = {"ok": False, "status": "pending"}

    with patch("main.search_loan_app", AsyncMock(return_value=mock_search)), \
         patch("main.check_rbi_directory", AsyncMock(return_value=mock_rbi)), \
         patch("main.fetch_pages", AsyncMock(return_value=mock_fetch)), \
         patch("main.capture_screenshot", AsyncMock(return_value=mock_browser)), \
         patch("main.run_research", AsyncMock(return_value=mock_research)):

        events = []
        async for chunk in investigate_stream("BrowserFailApp", "https://example.com"):
            data = json.loads(chunk.removeprefix("data: ").strip())
            events.append(data)

        browser_event = [e for e in events if e.get("step") == "browser"][-1]
        assert browser_event["status"] == "failed"
        assert "Chrome CDP disconnected" in browser_event["message"]


# 8. Research pending state is accurate
@pytest.mark.asyncio
async def test_research_pending_state(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    mock_search = {"ok": True, "results": []}
    mock_rbi = {"ok": True, "checked": False}
    mock_fetch = {"ok": False, "results": []}
    mock_browser = {"ok": False}
    mock_research = {
        "ok": False,
        "status": "pending",
        "error": "Budget exceeded",
        "notice": "Research pending or unavailable. Preliminary evidence summary is based on completed Search and Fetch results.",
    }

    with patch("main.search_loan_app", AsyncMock(return_value=mock_search)), \
         patch("main.check_rbi_directory", AsyncMock(return_value=mock_rbi)), \
         patch("main.fetch_pages", AsyncMock(return_value=mock_fetch)), \
         patch("main.capture_screenshot", AsyncMock(return_value=mock_browser)), \
         patch("main.run_research", AsyncMock(return_value=mock_research)):

        events = []
        async for chunk in investigate_stream("ResearchPendingApp", None):
            data = json.loads(chunk.removeprefix("data: ").strip())
            events.append(data)

        res_event = [e for e in events if e.get("step") == "research"][-1]
        assert res_event["status"] == "pending"
        final_event = next(e for e in events if e.get("step") == "final")
        assert final_event["report"]["research"]["status"] == "pending"


# 9. Monitor creation happens only on button click (POST /api/watch)
@pytest.mark.asyncio
async def test_monitor_creation_only_on_watch_endpoint(monkeypatch, client):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    mock_monitor_func = AsyncMock(return_value={"ok": True, "monitor_id": "mon_live_123", "status": "active", "message": "Monitor created"})
    with patch("main.create_monitor", mock_monitor_func):
        # Investigation stream should NEVER invoke create_monitor
        mock_search = {"ok": True, "results": []}
        with patch("main.search_loan_app", AsyncMock(return_value=mock_search)), \
             patch("main.check_rbi_directory", AsyncMock(return_value={"ok": True})), \
             patch("main.fetch_pages", AsyncMock(return_value={"ok": True})), \
             patch("main.capture_screenshot", AsyncMock(return_value={"ok": True})), \
             patch("main.run_research", AsyncMock(return_value={"ok": True})):
            async for _ in investigate_stream("TestApp", None):
                pass
        mock_monitor_func.assert_not_called()

        # POST /api/watch DOES invoke create_monitor
        resp = client.post("/api/watch", json={})
        assert resp.status_code == 200
        mock_monitor_func.assert_called_once()
        assert resp.json()["monitor_id"] == "mon_live_123"


# 10. JSON reports contain the actual investigation results
@pytest.mark.asyncio
async def test_json_report_contains_actual_results(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    mock_search = {
        "ok": True,
        "results": [{"title": "Live Title", "url": "https://real.lender.in", "snippet": "Live Snippet"}],
        "sources": ["https://real.lender.in"],
        "identified_site": "https://real.lender.in",
    }
    mock_rbi = {"ok": True, "checked": True, "found": False, "failure_reason": ""}
    mock_fetch = {"ok": True, "results": [{"url": "https://real.lender.in"}], "indicators": {"complaint_count": 0}}
    screenshot_path = tmp_path / "live_screen.png"
    with open(screenshot_path, "wb") as screenshot_file:
        screenshot_file.write(b"test screenshot")
    mock_browser = {
        "ok": True,
        "screenshot_url": "/static/evidence/live_screen.png",
        "screenshot_path": str(screenshot_path),
    }
    mock_research = {"ok": True, "report": "Live research report", "citations": [{"title": "Source 1", "url": "https://real.lender.in/terms"}]}

    with patch("main.search_loan_app", AsyncMock(return_value=mock_search)), \
         patch("main.check_rbi_directory", AsyncMock(return_value=mock_rbi)), \
         patch("main.fetch_pages", AsyncMock(return_value=mock_fetch)), \
         patch("main.capture_screenshot", AsyncMock(return_value=mock_browser)), \
         patch("main.run_research", AsyncMock(return_value=mock_research)):

        events = []
        async for chunk in investigate_stream("RealLenderApp", "https://real.lender.in"):
            data = json.loads(chunk.removeprefix("data: ").strip())
            events.append(data)

        final_rep = next(e for e in events if e.get("step") == "final")["report"]
        assert final_rep["app_name"] == "RealLenderApp"
        assert final_rep["site"] == "https://real.lender.in"
        assert final_rep["demo"] is False
        assert final_rep["search"]["results"][0]["url"] == "https://real.lender.in"
        assert final_rep["browser"]["screenshot_url"] == (
            "https://loanguard-sqcb.onrender.com/static/evidence/live_screen.png"
        )
        assert len(final_rep["research"]["citations"]) == 1
