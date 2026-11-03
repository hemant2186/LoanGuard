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
import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app, investigate_stream, _build_indicators
from services.scoring import Indicators, compute_score
from services.tinyfish_search import default_queries, identify_official_website
from services.tinyfish_agent import _normalize, check_rbi_directory, get_rbi_directory_status
from services.tinyfish_research import run_research, get_research_status


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


@pytest.mark.asyncio
async def test_first_wave_starts_search_agent_and_research_concurrently(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")
    started: set[str] = set()
    release = asyncio.Event()

    async def first_wave(service: str):
        started.add(service)
        if len(started) == 3:
            release.set()
        await release.wait()
        if service == "search":
            return {"ok": True, "status": "completed", "results": [], "identified_site": None}
        if service == "agent":
            return {"ok": False, "status": "pending", "run_id": "agent-1", "checked": False, "found": False}
        return {"ok": False, "status": "pending", "research_run_id": "research-1", "run_id": "research-1"}

    async def empty_fetch(*args, **kwargs):
        return {"ok": False, "status": "failed", "results": [], "indicators": {}, "error": "No URLs to fetch"}

    async def empty_browser(*args, **kwargs):
        return {"ok": False, "status": "failed", "error": "No URL"}

    async def search(*args, **kwargs):
        return await first_wave("search")

    async def agent(*args, **kwargs):
        return await first_wave("agent")

    async def research(*args, **kwargs):
        return await first_wave("research")

    with patch("main.search_loan_app", new=search), \
         patch("main.check_rbi_directory", new=agent), \
         patch("main.run_research", new=research), \
         patch("main.fetch_pages", empty_fetch), \
         patch("main.capture_screenshot", empty_browser):
        events = [json.loads(chunk.removeprefix("data: ").strip())
                  async for chunk in investigate_stream("WaveApp", None)]

    assert started == {"search", "agent", "research"}
    assert any(e["step"] == "research" and e["status"] == "pending" for e in events)


@pytest.mark.asyncio
async def test_fetch_and_browser_start_concurrently_after_search_without_site(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")
    starts: list[str] = []
    fetch_started = asyncio.Event()
    browser_started = asyncio.Event()

    async def search(*args, **kwargs):
        return {"ok": True, "status": "completed",
                "results": [{"url": "https://official.example"}],
                "identified_site": "https://official.example"}

    async def fetch(*args, **kwargs):
        starts.append("fetch")
        fetch_started.set()
        await browser_started.wait()
        return {"ok": True, "status": "completed", "results": [], "indicators": {}}

    async def browser(*args, **kwargs):
        starts.append("browser")
        browser_started.set()
        await fetch_started.wait()
        return {"ok": False, "status": "failed", "error": "mocked"}

    with patch("main.search_loan_app", search), \
         patch("main.check_rbi_directory", AsyncMock(return_value={"ok": False, "status": "pending", "run_id": "a"})), \
         patch("main.run_research", AsyncMock(return_value={"ok": False, "status": "pending", "run_id": "r"})), \
         patch("main.fetch_pages", fetch), \
         patch("main.capture_screenshot", browser):
        events = [json.loads(chunk.removeprefix("data: ").strip())
                  async for chunk in investigate_stream("DependentApp", None)]

    assert set(starts) == {"fetch", "browser"}
    assert [e["step"] for e in events].index("fetch") < len(events)
    assert any(e["step"] == "browser" and e["status"] == "failed" for e in events)


@pytest.mark.asyncio
async def test_supplied_site_starts_fetch_and_browser_in_first_wave(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")
    starts: set[str] = set()
    release = asyncio.Event()

    async def service(name: str, result: dict[str, Any]):
        starts.add(name)
        if len(starts) == 5:
            release.set()
        await release.wait()
        return result

    async def search(*args, **kwargs):
        return await service("search", {"ok": True, "status": "completed", "results": []})

    async def agent(*args, **kwargs):
        return await service("agent", {"ok": False, "status": "pending", "run_id": "a"})

    async def research(*args, **kwargs):
        return await service("research", {"ok": False, "status": "pending", "run_id": "r"})

    async def fetch(*args, **kwargs):
        return await service("fetch", {"ok": True, "status": "completed", "results": [], "indicators": {}})

    async def browser(*args, **kwargs):
        return await service("browser", {"ok": False, "status": "failed", "error": "mocked"})

    with patch("main.search_loan_app", new=search), \
         patch("main.check_rbi_directory", new=agent), \
         patch("main.run_research", new=research), \
         patch("main.fetch_pages", new=fetch), \
         patch("main.capture_screenshot", new=browser):
        await asyncio.wait_for(
            _collect_events(investigate_stream("SuppliedSiteApp", "https://official.example")),
            timeout=1,
        )
    assert starts == {"search", "agent", "research", "fetch", "browser"}


@pytest.mark.asyncio
async def test_agent_timeout_does_not_block_preliminary_report(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    async def slow_agent(*args, **kwargs):
        await asyncio.sleep(0.05)
        return {"ok": False, "status": "timed_out", "checked": False, "found": False,
                "failure_reason": "does NOT indicate absence"}

    with patch("main.search_loan_app", AsyncMock(return_value={
            "ok": True, "status": "completed", "results": [],
        })), \
         patch("main.check_rbi_directory", side_effect=slow_agent), \
         patch("main.run_research", AsyncMock(return_value={
             "ok": False, "status": "pending", "run_id": "research-1",
         })), \
         patch("main.fetch_pages", AsyncMock(return_value={
             "ok": False, "status": "failed", "results": [], "indicators": {},
         })), \
         patch("main.capture_screenshot", AsyncMock(return_value={
             "ok": False, "status": "failed", "error": "mocked",
         })):
        events = await asyncio.wait_for(
            _collect_events(investigate_stream("TimeoutApp", "https://official.example")),
            timeout=0.03,
        )
    assert any(e["step"] == "final" for e in events)
    assert not any(e["step"] == "agent" and e.get("status") == "timed_out" for e in events)


async def _collect_events(stream):
    return [json.loads(chunk.removeprefix("data: ").strip()) async for chunk in stream]


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


@pytest.mark.asyncio
async def test_agent_queue_and_result_retrieval(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    class FakeAgent:
        async def queue(self, **kwargs):
            return type("Queued", (), {"run_id": "agent-run-1", "error": None})()

    class FakeRuns:
        async def get(self, run_id):
            assert run_id == "agent-run-1"
            return type("Run", (), {
                "status": "COMPLETED",
                "result": {"checked": True, "found": True, "matched_name": "KreditBee",
                           "lending_entity": "Example Bank", "evidence": "directory row",
                           "failure_reason": ""},
                "error": None,
            })()

    class FakeClient:
        def __init__(self, **kwargs):
            self.agent, self.runs = FakeAgent(), FakeRuns()

    with patch("tinyfish.AsyncTinyFish", FakeClient):
        queued = await check_rbi_directory("KreditBee")
        result = await get_rbi_directory_status(queued["run_id"])
    assert queued["status"] == "pending"
    assert queued["run_id"] == "agent-run-1"
    assert result["status"] == "completed"
    assert result["found"] is True


@pytest.mark.asyncio
async def test_agent_timeout_and_access_denied_are_not_absence(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    class FakeRuns:
        async def get(self, run_id):
            return type("Run", (), {
                "status": "FAILED", "result": {"checked": True, "found": True},
                "error": type("Error", (), {"message": "Unauthorized Access", "code": "ACCESS_DENIED"})(),
            })()

    class FakeClient:
        def __init__(self, **kwargs):
            self.runs = FakeRuns()

    with patch("tinyfish.AsyncTinyFish", FakeClient):
        result = await get_rbi_directory_status("agent-denied")
    assert result["status"] == "failed"
    assert result["checked"] is False
    assert result["found"] is False
    assert "Unauthorized Access" in result["failure_reason"]

    class SlowAgent:
        async def queue(self, **kwargs):
            await asyncio.sleep(0.01)

    class SlowClient:
        def __init__(self, **kwargs):
            self.agent = SlowAgent()

    import services.tinyfish_agent as agent_service
    monkeypatch.setattr(agent_service, "QUEUE_TIMEOUT_S", 0.001)
    with patch("tinyfish.AsyncTinyFish", SlowClient):
        timed_out = await check_rbi_directory("SlowApp")
    assert timed_out["status"] == "timed_out"
    assert timed_out["found"] is False
    assert "does NOT indicate absence" in timed_out["failure_reason"]


@pytest.mark.asyncio
async def test_research_queue_and_completed_report_with_citations(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    class FakeResearch:
        async def queue(self, **kwargs):
            return type("Queued", (), {"research_run_id": "research-run-1"})()
        async def get(self, run_id):
            return type("Research", (), {
                "status": "COMPLETED",
                "deep_result": type("Deep", (), {
                    "result": "Verified report", "citations": ["https://rbi.org.in/source"]
                })(),
                "error": None,
            })()

    class FakeClient:
        def __init__(self, **kwargs):
            self.research = FakeResearch()

    with patch("tinyfish.AsyncTinyFish", FakeClient):
        queued = await run_research("KreditBee")
        result = await get_research_status(queued["research_run_id"])
    assert queued["status"] == "pending"
    assert queued["research_run_id"] == "research-run-1"
    assert result["status"] == "completed"
    assert result["report"] == "Verified report"
    assert result["citations"] == [{"title": "https://rbi.org.in/source", "url": "https://rbi.org.in/source"}]


@pytest.mark.asyncio
async def test_research_failure_and_cancellation(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")

    class FakeResearch:
        def __init__(self):
            self.status = "FAILED"
        async def get(self, run_id):
            return type("Research", (), {
                "status": self.status, "deep_result": None,
                "error": type("Error", (), {"message": "provider failure"})(),
            })()

    fake_research = FakeResearch()
    class FakeClient:
        def __init__(self, **kwargs):
            self.research = fake_research

    with patch("tinyfish.AsyncTinyFish", FakeClient):
        failed = await get_research_status("research-failed")
        fake_research.status = "CANCELLED"
        cancelled = await get_research_status("research-cancelled")
    assert failed["status"] == "failed"
    assert cancelled["status"] == "cancelled"


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


@pytest.mark.asyncio
async def test_monitor_creation_is_active_and_not_duplicated(monkeypatch, client):
    import main
    main._active_monitor = None


def test_monitor_creation_failure_preserves_provider_error(monkeypatch, client):
    import main
    main._active_monitor = None
    monkeypatch.setenv("DEMO_MODE", "0")
    with patch("main.create_monitor", AsyncMock(return_value={
        "ok": False, "status": "unavailable", "error": "402: insufficient credits",
        "message": "Monitoring unavailable — insufficient credits for this beta feature.",
    })):
        response = client.post("/api/watch", json={})
    assert response.json()["ok"] is False
    assert "insufficient credits" in response.json()["error"]
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy_test_key")
    mock_monitor_func = AsyncMock(return_value={
        "ok": True, "monitor_id": "mon-live-active", "status": "created",
        "message": "Monitor created",
    })
    with patch("main.create_monitor", mock_monitor_func):
        first = client.post("/api/watch", json={})
        second = client.post("/api/watch", json={})
    assert first.json()["status"] == "active"
    assert second.json()["status"] == "active"
    assert second.json()["monitor_id"] == "mon-live-active"
    mock_monitor_func.assert_awaited_once()
    main._active_monitor = None


def test_monitor_timeline_is_not_completed_and_updates_from_panel():
    index_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")
    with open(index_path, encoding="utf-8") as file:
        source = file.read()
    assert "status:'active'" in source
    assert "setStep('monitor','active'" in source
    assert "setStep('monitor','completed'" not in source


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
