"""TinyFish Research integration (Feature 5) — cited report.

Uses AsyncTinyFish.research.run() (beta). Research is slow (deep mode: 5-20 min),
so this service is NON-BLOCKING in the pipeline: it runs with a short budget and,
if it does not finish in time, returns status="running" without failing the
investigation. Citations are never fabricated — they come from the API only.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from .config import get_api_key, has_api_key

RESEARCH_BUDGET_S = float(os.getenv("RESEARCH_BUDGET_S", "90"))


def build_query(name: str, site: str | None) -> str:
    site_part = f" Its website is {site}." if site else ""
    return (
        f"Investigate the Indian instant-loan app called \"{name}\"."
        f"{site_part} Cover: who operates it, whether a regulated RBI lending partner"
        " (NBFC/bank) is identified, what the RBI digital lending apps directory shows,"
        " what public complaints or warning signals (harassment, hidden fees, data misuse)"
        " exist, what transparency information (grievance officer, interest rate, address)"
        " is missing, and how a borrower can independently verify it before borrowing."
    )


async def run_research(name: str, site: str | None = None) -> dict[str, Any]:
    """Run a cited research report. Never raises; non-blocking on timeout."""
    out: dict[str, Any] = {
        "ok": False,
        "status": "pending",
        "report": None,
        "citations": [],
        "run_id": None,
        "error": None,
    }
    if not has_api_key():
        out["error"] = "TINYFISH_API_KEY not configured"
        out["status"] = "unavailable"
        return out
    try:
        from tinyfish import AsyncTinyFish  # type: ignore

        client = AsyncTinyFish(api_key=get_api_key(), timeout=RESEARCH_BUDGET_S + 30, max_retries=0)
    except Exception as exc:  # pragma: no cover
        out["error"] = f"SDK init failed: {exc}"
        out["status"] = "failed"
        return out

    try:
        response = await asyncio.wait_for(
            client.research.run(query=build_query(name, site), mode="deep"),
            timeout=RESEARCH_BUDGET_S,
        )
    except asyncio.TimeoutError:
        out["status"] = "pending"
        out["error"] = f"Research pending (budget {RESEARCH_BUDGET_S}s reached; TinyFish deep mode takes 5-20 min in background)"
        out["notice"] = "Research pending or unavailable. Preliminary evidence summary is based on completed Search and Fetch results."
        return out
    except Exception as exc:
        out["status"] = "unavailable"
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["notice"] = "Research pending or unavailable. Preliminary evidence summary is based on completed Search and Fetch results."
        return out

    status = str(getattr(response, "status", "")).upper()
    out["run_id"] = getattr(response, "research_run_id", None) or getattr(response, "run_id", None)
    deep = getattr(response, "deep_result", None)
    report = getattr(deep, "result", None) if deep else None
    citations = getattr(deep, "citations", None) if deep else None
    if report:
        out["report"] = report if isinstance(report, str) else str(report)
        out["citations"] = citations if isinstance(citations, list) else (list(citations) if citations else [])
        out["status"] = "completed"
        out["ok"] = True
    else:
        out["status"] = "unavailable"
        out["error"] = "Research completed but returned no report (status={})".format(status)
    return out
