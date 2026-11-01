"""Integration and API endpoint tests for LoanGuard FastAPI application."""

from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app, _load_sample, _build_indicators, _collect_fetch_urls, _build_report, DISCLAIMER
from services.scoring import compute_score, Indicators


@pytest.fixture
def client():
    return TestClient(app)


def test_index_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "LoanGuard" in response.text
    assert "Check before you borrow" in response.text


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "demo_mode" in data
    assert "api_key_configured" in data
    assert "rbi_url" in data
    assert "port" in data
    # Ensure no secrets are leaked
    assert "sk-" not in json.dumps(data)


def test_production_cors_allows_vercel_frontend(client):
    response = client.get(
        "/api/health",
        headers={"Origin": "https://loanguard-five.vercel.app"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://loanguard-five.vercel.app"


def test_frontend_uses_render_backend_and_validates_health_status():
    index_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")
    with open(index_path, encoding="utf-8") as file:
        source = file.read()
    assert "https://loanguard-sqcb.onrender.com" in source
    assert "if(h.status !== 'ok')" in source
    assert "fetch(apiUrl('/api/health'))" in source
    assert "apiUrl('/api/watch')" in source


def test_investigate_empty_name(client):
    response = client.get("/api/investigate?name=")
    assert response.status_code == 400
    assert "required" in response.json().get("error", "").lower()


def test_watch_endpoint_demo_mode(client, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    response = client.post("/api/watch", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["demo"] is True
    assert "SAMPLE DATA" in data["message"]


def test_webhook_and_alerts_endpoints(client, tmp_path, monkeypatch):
    test_alerts_file = str(tmp_path / "alerts.json")
    import main
    monkeypatch.setattr(main, "ALERTS_FILE", test_alerts_file)

    payload = {
        "event": "monitor_change",
        "url": "https://www.rbi.org.in/",
        "change_detected": "New DLA entry added"
    }
    resp = client.post("/api/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json().get("received") is True

    # Retrieve via GET /api/alerts
    resp_alerts = client.get("/api/alerts")
    assert resp_alerts.status_code == 200
    alerts = resp_alerts.json().get("alerts", [])
    assert len(alerts) >= 1
    assert alerts[-1]["payload"] == payload


def test_investigate_stream_demo_mode(client, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    response = client.get("/api/investigate?name=SampleLoan+Demo")
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    lines = [line.strip() for line in response.text.split("\n") if line.startswith("data: ")]
    events = [json.loads(line.removeprefix("data: ")) for line in lines]

    steps = [e.get("step") for e in events]
    assert "start" in steps
    assert "search" in steps
    assert "agent" in steps
    assert "fetch" in steps
    assert "browser" in steps
    assert "research" in steps
    assert "monitor" in steps
    assert "scoring" in steps
    assert "final" in steps

    final_event = next(e for e in events if e.get("step") == "final")
    report = final_event.get("report", {})
    assert report["demo"] is True
    assert report["app_name"] == "SampleLoan Demo"
    assert report["score"]["verdict"] == "HIGH RISK"


def test_collect_fetch_urls_deduplicates():
    site = "https://app.example.com"
    search = {
        "results": [
            {"url": "https://app.example.com"},
            {"url": "https://news.example.com/1"},
            {"url": "https://news.example.com/1"},
            {"url": "https://forum.example.com/2"},
        ]
    }
    urls = _collect_fetch_urls(site, search)
    assert len(urls) == 3
    assert urls[0] == "https://app.example.com"
    assert urls[1] == "https://news.example.com/1"
    assert urls[2] == "https://forum.example.com/2"


def test_build_indicators_and_scoring():
    rbi = {"checked": True, "found": False}
    fetch = {
        "indicators": {
            "grievance_found": False,
            "lending_partner_found": False,
            "address_found": False,
            "interest_rate_found": False,
            "complaint_count": 9,
        }
    }
    ind = _build_indicators(rbi, fetch)
    score_res = compute_score(ind)
    assert score_res["score"] == 0
    assert score_res["verdict"] == "HIGH RISK"
