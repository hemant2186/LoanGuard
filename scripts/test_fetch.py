"""
LoanGuard — Fetch integration smoke test.

Uses AsyncTinyFish.fetch.get_contents() to fetch one public URL and prints the
sanitized raw JSON response (results + errors) so we can inspect the real shape
before writing parsers.

Run:
  python scripts/test_fetch.py
  python scripts/test_fetch.py https://example.com
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

API_KEY = os.getenv("TINYFISH_API_KEY", "").strip()
URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
TIMEOUT_S = float(os.getenv("FETCH_RUN_TIMEOUT_S", "180"))


def _mask(value: str | None) -> str:
    if not value:
        return "<not set>"
    return value[:4] + "..." + value[-4:]


def _sanitize(obj: Any, secret: str) -> Any:
    if isinstance(obj, dict):
        return {k: ("***REDACTED***" if k.upper() == "TINYFISH_API_KEY" else _sanitize(v, secret)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v, secret) for v in obj]
    if isinstance(obj, str):
        return "***REDACTED***" if (secret and obj == secret) else obj
    return obj


async def run() -> int:
    print("=" * 72)
    print("LoanGuard — Fetch smoke test")
    print("=" * 72)
    print(f"TINYFISH_API_KEY : {_mask(API_KEY)}")
    print(f"URL              : {URL}")
    print(f"Timeout (s)      : {TIMEOUT_S}")
    if not API_KEY:
        print("\nERROR: TINYFISH_API_KEY is not set.")
        return 2

    from tinyfish import AsyncTinyFish  # type: ignore

    client = AsyncTinyFish(api_key=API_KEY, timeout=200.0, max_retries=0)
    print("\nCalling client.fetch.get_contents() ...")
    try:
        resp = await asyncio.wait_for(
            client.fetch.get_contents(
                [URL],
                format="markdown",
                links=True,
                per_url_timeout_ms=60_000,
            ),
            timeout=TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        print("\n[TIMEOUT] Fetch exceeded the time budget.")
        return 1
    except Exception as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}")
        return 1

    try:
        raw = resp.model_dump()
    except Exception:
        raw = {"type": type(resp).__name__, "results": getattr(resp, "results", None), "errors": getattr(resp, "errors", None)}

    print("\n--- RAW RESPONSE (sanitized, complete) ---")
    print(json.dumps(_sanitize(raw, API_KEY), indent=2, default=str)[:8000])

    # Friendly summary of what we found (purely observational, no parsing assumed yet)
    results = getattr(resp, "results", None) or []
    errors = getattr(resp, "errors", None) or []
    print(f"\nresults count : {len(results)}")
    for r in results:
        print("  - url        :", getattr(r, "url", None))
        print("    final_url  :", getattr(r, "final_url", None))
        print("    title      :", getattr(r, "title", None))
        print("    text len   :", len(getattr(r, "text", "") or ""))
        print("    links count:", len(getattr(r, "links", None) or []))
    print(f"errors count  : {len(errors)}")
    for e in errors:
        print("  - url   :", getattr(e, "url", None))
        print("    error :", getattr(e, "error", None))
        print("    status:", getattr(e, "status", None))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
