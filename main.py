"""LoanGuard — FastAPI backend.

Endpoints:
  GET  /                    Serve the frontend dashboard.
  GET  /api/investigate      SSE investigation pipeline (name=, site=).
  POST /api/watch            Create an RBI directory monitor (on demand).
  POST /api/webhook         Receive monitor notifications -> stored alerts.
  GET  /api/alerts          Return stored monitor alerts.
  GET  /api/health          Health + configuration status (no secrets).
  GET  /static/*            Static evidence files.

The pipeline streams six TinyFish feature stages over SSE. Research is
non-blocking. Monitor is only created on demand. Demo mode replays clearly
labelled sample data. Live errors are never silently replaced with sample data.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from services.config import (
    EVIDENCE_DIR,
    PROJECT_ROOT,
    get_api_key,
    get_port,
    get_rbi_url,
    has_api_key,
    is_demo_mode,
)
from services.scoring import Indicators, compute_score
from services.tinyfish_agent import check_rbi_directory
from services.tinyfish_browser import capture_screenshot
from services.tinyfish_fetch import fetch_pages
from services.tinyfish_monitor import create_monitor
from services.tinyfish_research import run_research
from services.tinyfish_search import search_loan_app

ALERTS_FILE = os.path.join(PROJECT_ROOT, "demo", "alerts.json")
DISCLAIMER = (
    "LoanGuard provides preliminary risk indicators based on publicly available information. "
    "It is not an RBI service, legal opinion or guarantee of lender legitimacy. RBI's directory "
    "is based on information submitted by regulated entities and is not independently validated by "
    "RBI. Always verify directly with the regulated lender before borrowing."
)

app = FastAPI(title="LoanGuard", version="1.0.0")
os.makedirs(EVIDENCE_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(PROJECT_ROOT, "static")), name="static")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sse(event: str, payload: dict[str, Any]) -> str:
    payload = {"step": event, **payload}
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _load_sample() -> dict[str, Any]:
    with open(os.path.join(PROJECT_ROOT, "demo", "sample_events.json"), encoding="utf-8") as f:
        return json.load(f)


def _build_indicators(rbi: dict, fetch: dict) -> Indicators:
    ind = fetch.get("indicators", {}) if fetch else {}
    return Indicators(
        rbi_checked=bool(rbi.get("checked", False)),
        rbi_found=bool(rbi.get("found", False)),
        grievance_found=bool(ind.get("grievance_found", False)),
        lending_partner_found=bool(ind.get("lending_partner_found", False)),
        address_found=bool(ind.get("address_found", False)),
        interest_rate_found=bool(ind.get("interest_rate_found", False)),
        complaint_count=int(ind.get("complaint_count", 0) or 0),
    )


def _collect_fetch_urls(site: str | None, search: dict) -> list[str]:
    urls: list[str] = []
    if site:
        urls.append(site)
    for r in (search.get("results") or []):
        u = r.get("url")
        if u:
            urls.append(u)
    # de-duplicate, cap at 10
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= 10:
            break
    return out


def _build_report(name: str, site: str | None, demo: bool, search: dict, rbi: dict,
                  fetch: dict, browser: dict, research: dict, score: dict) -> dict[str, Any]:
    return {
        "app_name": name,
        "site": site or "",
        "demo": demo,
        "score": score,
        "search": search,
        "rbi": rbi,
        "fetch": fetch,
        "browser": browser,
        "research": research,
        "indicators": {
            "rbi_checked": bool(rbi.get("checked", False)),
            "rbi_found": bool(rbi.get("found", False)),
            "grievance_found": bool(fetch.get("indicators", {}).get("grievance_found", False)),
            "lending_partner_found": bool(fetch.get("indicators", {}).get("lending_partner_found", False)),
            "address_found": bool(fetch.get("indicators", {}).get("address_found", False)),
            "interest_rate_found": bool(fetch.get("indicators", {}).get("interest_rate_found", False)),
            "complaint_count": int(fetch.get("indicators", {}).get("complaint_count", 0) or 0),
        },
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# SSE investigation pipeline
# --------------------------------------------------------------------------- #
async def investigate_stream(name: str, site: str | None) -> AsyncGenerator[str, None]:
    demo = is_demo_mode()
    # In demo mode the report always reflects the fictional sample app, never
    # the name the user typed, so fictional findings are never attached to a
    # real loan app brand.
    sample = _load_sample() if demo else None
    report_name = sample["app_name"] if demo and sample else name
    report_site = sample.get("site") if demo and sample else (site or "")

    yield _sse("start", {"app_name": report_name, "site": report_site, "demo": demo,
                         "message": "Investigation started."})

    if demo:
        yield _sse("search", {"status": "completed", "message": "Public evidence discovered (sample).",
                              "data": sample["search"], "demo": True})
        yield _sse("agent", {"status": "completed", "message": "SIMULATED RBI RESULT — SAMPLE DATA (sample check).",
                             "data": sample["rbi"], "demo": True})
        yield _sse("fetch", {"status": "completed", "message": "Public websites inspected (sample).",
                             "data": sample["fetch"], "demo": True})
        yield _sse("browser", {"status": "completed", "message": "Visual evidence captured (sample).",
                               "data": sample["browser"], "demo": True})
        yield _sse("research", {"status": "completed", "message": "Cited findings prepared (sample).",
                                "data": sample["research"], "demo": True})
        yield _sse("monitor", {"status": "available",
                               "message": "SIMULATED MONITOR — SAMPLE DATA. Click Monitor to simulate.", "demo": True})
        yield _sse("scoring", {"status": "completed", "message": "Risk score computed (sample).",
                               "data": sample["score"], "demo": True})
        report = _build_report(report_name, report_site, True, sample["search"], sample["rbi"], sample["fetch"],
                               sample["browser"], sample["research"], sample["score"])
        yield _sse("final", {"status": "completed", "report": report, "demo": True})
        return

    # ---------------- LIVE MODE ----------------
    if not has_api_key():
        yield _sse("error", {"status": "failed",
                             "message": "Live mode is on but TINYFISH_API_KEY is not configured. Set the key or enable DEMO_MODE=1."})
        yield _sse("final", {"status": "failed", "report": {"app_name": name, "site": site or "", "demo": False,
                         "error": "API key not configured", "disclaimer": DISCLAIMER}})
        return

    # 1. SEARCH
    yield _sse("search", {"status": "started", "message": "Discovering public evidence..."})
    search = await search_loan_app(name)
    yield _sse("search", {"status": "completed", "message": "Public evidence discovered.",
                          "data": search})

    # 2. AGENT (RBI directory)
    yield _sse("agent", {"status": "started", "message": "Checking the RBI directory..."})
    rbi = await check_rbi_directory(name)
    yield _sse("agent", {"status": "completed",
                         "message": "RBI directory checked." if rbi.get("checked") else "RBI directory lookup completed (check status).",
                         "data": rbi})

    # 3. FETCH
    yield _sse("fetch", {"status": "started", "message": "Inspecting public websites..."})
    urls = _collect_fetch_urls(site, search)
    fetch = await fetch_pages(urls, purpose=f"Investigate the loan app {name} for risk indicators")
    yield _sse("fetch", {"status": "completed", "message": "Public websites inspected.", "data": fetch})

    # 4. BROWSER (visual evidence)
    yield _sse("browser", {"status": "started", "message": "Capturing visual evidence..."})
    shot_url = site or (search["results"][0]["url"] if search.get("results") else "")
    if shot_url:
        browser = await capture_screenshot(shot_url, name)
    else:
        browser = {"ok": False, "error": "No website URL available to screenshot"}
    yield _sse("browser", {"status": "completed", "message": "Visual evidence captured." if browser.get("ok") else "Visual evidence capture failed.",
                           "data": browser})

    # 5. RESEARCH (non-blocking)
    yield _sse("research", {"status": "started", "message": "Preparing cited findings (this can take several minutes)..."})
    research = await run_research(name, site)
    yield _sse("research", {"status": research.get("status", "completed"),
                            "message": "Cited findings prepared." if research.get("ok") else "Research still running or unavailable — not blocking the report.",
                            "data": research})

    # 6. MONITOR stage (informational — created on demand only)
    yield _sse("monitor", {"status": "available",
                           "message": "Monitoring available on demand — click Monitor to create a watch on the RBI directory."})

    # SCORING
    indicators = _build_indicators(rbi, fetch)
    score = compute_score(indicators)
    yield _sse("scoring", {"status": "completed", "message": "Risk score computed.", "data": score})

    # FINAL
    report = _build_report(name, site, False, search, rbi, fetch, browser, research, score)
    yield _sse("final", {"status": "completed", "report": report})


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    return FileResponse(os.path.join(PROJECT_ROOT, "index.html"))


@app.get("/api/investigate")
async def investigate(name: str, site: str | None = None):
    if not name or not name.strip():
        return JSONResponse({"error": "A loan application name is required."}, status_code=400)
    name = name.strip()
    site = (site or "").strip() or None
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        investigate_stream(name, site),
        media_type="text/event-stream",
        headers=headers,
    )


@app.post("/api/watch")
async def watch(request: Request):
    """Create an RBI directory monitor on demand (Feature 6)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    webhook_url = body.get("webhook_url")
    # If in demo mode, simulate.
    if is_demo_mode():
        return {"ok": True, "demo": True, "monitor_id": "demo-monitor-001", "status": "created",
                "message": "SIMULATED MONITOR — SAMPLE DATA. Monitoring the RBI directory (simulated)."}
    result = await create_monitor(webhook_url=webhook_url)
    return result


@app.post("/api/webhook")
async def webhook(request: Request):
    """Receive monitor notifications and persist them as alerts."""
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": await request.body()}
    alerts = _load_alerts()
    alerts.append({"payload": payload})
    _save_alerts(alerts)
    return {"ok": True, "received": True}


@app.get("/api/alerts")
async def alerts():
    return {"alerts": _load_alerts()}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "demo_mode": is_demo_mode(),
        "api_key_configured": has_api_key(),
        "rbi_url": get_rbi_url(),
        "port": get_port(),
    }


# --------------------------------------------------------------------------- #
# Alert persistence (simple JSON file)
# --------------------------------------------------------------------------- #
def _load_alerts() -> list[dict[str, Any]]:
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_alerts(alerts: list[dict[str, Any]]) -> None:
    try:
        with open(ALERTS_FILE, "w", encoding="utf-8") as f:
            json.dump(alerts, f, indent=2, default=str)
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=get_port(), reload=False)
