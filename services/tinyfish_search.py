"""TinyFish Search integration (Feature 1).

Discovers public information about a loan application using
AsyncTinyFish.search.query(). Defensive parsing only against documented fields
(.query, .total_results, .results[] with .title/.url/.snippet).
"""

from __future__ import annotations

from typing import Any

from .config import get_api_key, has_api_key


def _client():
    from tinyfish import AsyncTinyFish  # type: ignore

    return AsyncTinyFish(api_key=get_api_key(), timeout=20.0, max_retries=0)


def default_queries(name: str) -> list[str]:
    return [
        f"{name} loan app complaints",
        f"{name} loan app harassment recovery agent",
        f"{name} lending partner NBFC bank",
        "RBI digital lending apps deployed by regulated entities directory",
    ]


def _parse_result(item: Any) -> dict[str, Any]:
    return {
        "title": getattr(item, "title", None),
        "url": getattr(item, "url", None),
        "snippet": getattr(item, "snippet", None),
    }


async def search_loan_app(name: str, queries: list[str] | None = None) -> dict[str, Any]:
    """Run discovery searches. Never raises — returns a status dict."""
    out: dict[str, Any] = {
        "ok": False,
        "queries": [],
        "results": [],
        "error": None,
    }
    if not has_api_key():
        out["error"] = "TINYFISH_API_KEY not configured"
        return out
    try:
        client = _client()
    except Exception as exc:  # pragma: no cover
        out["error"] = f"SDK init failed: {exc}"
        return out

    qs = queries or default_queries(name)
    for q in qs:
        entry: dict[str, Any] = {"query": q, "total": None, "results": [], "error": None}
        try:
            resp = await client.search.query(q)
            results = [_parse_result(r) for r in (getattr(resp, "results", None) or [])]
            entry["total"] = getattr(resp, "total_results", None)
            entry["results"] = results
            out["results"].extend(results)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            out["error"] = entry["error"]
        out["queries"].append(entry)

    # ok if at least one query returned results without error
    out["ok"] = any(qe["results"] for qe in out["queries"])
    if not out["ok"] and out["error"] is None:
        out["error"] = "No search results returned"
    return out
