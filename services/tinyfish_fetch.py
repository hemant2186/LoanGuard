"""TinyFish Fetch integration (Feature 3).

Reads publicly available pages via AsyncTinyFish.fetch.get_contents() (markdown),
then heuristically extracts transparency indicators and counts complaint keywords.

Extracted indicators (each means "found in inspected sources", never proof):
  - grievance redressal details
  - named lending partner (NBFC / bank)
  - registered business address
  - interest-rate disclosure
  - contact information
Plus a complaint-keyword count across all fetched text, with the source URLs that
contained the keywords. Context matters — occurrences are signals, never proof.
"""

from __future__ import annotations

import re
from typing import Any

from .config import COMPLAINT_KEYWORDS, get_api_key, has_api_key

# Indian NBFC / bank signals commonly named as lending partners.
LENDING_PARTNER_SIGNALS = [
    "nbfc",
    "lending partner",
    "in partnership with",
    "in association with",
    "powered by",
    "regulated by rbi",
    "registered with rbi",
]
GRIEVANCE_SIGNALS = [
    "grievance",
    "grievance officer",
    "grievance redressal",
    "complaint officer",
    "nodal officer",
    "grievance redressal officer",
]
ADDRESS_SIGNALS = [
    "registered office",
    "registered address",
    "corporate office",
    "office address",
]
INTEREST_SIGNALS = [
    "interest rate",
    "annual percentage rate",
    "apr",
    "per annum",
    "p.a.",
    "% per month",
    "processing fee",
]
CONTACT_RE = re.compile(r"(\+?\d[\d\s\-]{8,}\d|[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})")
PIN_RE = re.compile(r"\b\d{6}\b")


def _client():
    from tinyfish import AsyncTinyFish  # type: ignore

    return AsyncTinyFish(api_key=get_api_key(), timeout=180.0, max_retries=0)


def _has_any(text: str, signals: list[str]) -> bool:
    low = text.lower()
    return any(s in low for s in signals)


def _count_keywords(text: str) -> dict[str, int]:
    low = text.lower()
    counts: dict[str, int] = {}
    for kw in COMPLAINT_KEYWORDS:
        counts[kw] = low.count(kw)
    return counts


def _parse_result(item: Any) -> dict[str, Any]:
    text = getattr(item, "text", "") or ""
    return {
        "url": getattr(item, "url", None),
        "final_url": getattr(item, "final_url", None),
        "title": getattr(item, "title", None),
        "description": getattr(item, "description", None),
        "text": text,
        "text_len": len(text),
        "links": list(getattr(item, "links", None) or []),
    }


def _parse_error(item: Any) -> dict[str, Any]:
    return {
        "url": getattr(item, "url", None),
        "error": getattr(item, "error", None),
        "status": getattr(item, "status", None),
    }


def extract_indicators(fetched: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute transparency indicators + complaint signals from parsed results."""
    grievance = False
    lending_partner = False
    address = False
    interest_rate = False
    contact = False
    total_complaint = 0
    per_keyword: dict[str, int] = {kw: 0 for kw in COMPLAINT_KEYWORDS}
    complaint_sources: list[dict[str, Any]] = []

    for r in fetched:
        text = r.get("text", "") or ""
        url = r.get("final_url") or r.get("url")
        if _has_any(text, GRIEVANCE_SIGNALS):
            grievance = True
        if _has_any(text, LENDING_PARTNER_SIGNALS):
            lending_partner = True
        if _has_any(text, ADDRESS_SIGNALS) or bool(PIN_RE.search(text)):
            address = True
        if _has_any(text, INTEREST_SIGNALS):
            interest_rate = True
        if CONTACT_RE.search(text):
            contact = True

        counts = _count_keywords(text)
        page_total = sum(counts.values())
        if page_total:
            total_complaint += page_total
            for kw, c in counts.items():
                per_keyword[kw] += c
            if page_total >= 2:
                complaint_sources.append({"url": url, "count": page_total})

    return {
        "grievance_found": grievance,
        "lending_partner_found": lending_partner,
        "address_found": address,
        "interest_rate_found": interest_rate,
        "contact_found": contact,
        "complaint_count": total_complaint,
        "complaint_keywords": per_keyword,
        "complaint_sources": complaint_sources,
    }


async def fetch_pages(urls: list[str], purpose: str | None = None) -> dict[str, Any]:
    """Fetch up to 10 URLs. Never raises — returns a status dict."""
    out: dict[str, Any] = {
        "ok": False,
        "results": [],
        "errors": [],
        "indicators": {},
        "error": None,
    }
    urls = [u for u in urls if u][:10]
    if not urls:
        out["error"] = "No URLs to fetch"
        return out
    if not has_api_key():
        out["error"] = "TINYFISH_API_KEY not configured"
        return out
    try:
        client = _client()
    except Exception as exc:  # pragma: no cover
        out["error"] = f"SDK init failed: {exc}"
        return out

    try:
        resp = await client.fetch.get_contents(
            urls,
            format="markdown",
            links=True,
            per_url_timeout_ms=60_000,
            **({"purpose": purpose} if purpose else {}),
        )
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["results"] = [_parse_result(r) for r in (getattr(resp, "results", None) or [])]
    out["errors"] = [_parse_error(e) for e in (getattr(resp, "errors", None) or [])]
    out["indicators"] = extract_indicators(out["results"])
    out["ok"] = bool(out["results"])
    if not out["ok"] and out["error"] is None:
        out["error"] = "All URLs failed to fetch"
    return out
