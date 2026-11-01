"""
LoanGuard — TinyFish smoke test (PHASE 1).

Purpose:
  1. Confirm the `tinyfish` SDK imports and introspect its real methods.
  2. If a TINYFISH_API_KEY is set, make ONE live Search call and print the
     sanitized raw response so we can inspect the actual schema before
     building parsers.
  3. Report clearly what is verified vs. what still needs a live key.

Run:
  python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# --------------------------------------------------------------------------- #
# 0. Environment / dotenv
# --------------------------------------------------------------------------- #
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
    _DOTENV = True
except Exception:  # pragma: no cover
    _DOTENV = False


def _mask(value: str | None) -> str:
    """Return a redacted view of the API key for logging."""
    if not value:
        return "<not set>"
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-4:]


API_KEY = os.getenv("TINYFISH_API_KEY", "").strip()
RBI_DLA_URL = os.getenv("RBI_DLA_URL", "").strip()


def banner(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


# --------------------------------------------------------------------------- #
# 1. SDK import + introspection
# --------------------------------------------------------------------------- #
def introspect_sdk() -> dict[str, Any]:
    """Import tinyfish and report the available client + method names."""
    report: dict[str, Any] = {}
    try:
        import tinyfish  # type: ignore
        from tinyfish import TinyFish, AsyncTinyFish  # type: ignore

        report["tinyfish_version"] = getattr(tinyfish, "__version__", "unknown")
        report["TinyFish"] = True
        report["AsyncTinyFish"] = True
    except Exception as exc:  # pragma: no cover
        report["import_error"] = repr(exc)
        return report

    # List the resource groups and their methods on a client instance.
    try:
        client = TinyFish(api_key="dummy-key-for-introspection-only")
        groups: dict[str, list[str]] = {}
        for attr in sorted(vars(client)):
            obj = getattr(client, attr, None)
            if obj is None or attr.startswith("_"):
                continue
            if hasattr(obj, "__call__"):
                groups.setdefault("callable", []).append(attr)
            else:
                methods = [
                    m
                    for m in sorted(vars(type(obj)))
                    if not m.startswith("_") and callable(getattr(obj, m, None))
                ]
                if methods:
                    groups[attr] = methods
        report["client_resources"] = groups
    except Exception as exc:  # pragma: no cover
        report["introspect_error"] = repr(exc)

    return report


# --------------------------------------------------------------------------- #
# 2. Live Search probe (only if a real key is present)
# --------------------------------------------------------------------------- #
def live_search_probe() -> dict[str, Any]:
    """Make one minimal live Search call and capture the raw response shape."""
    if not API_KEY:
        return {"skipped": "TINYFISH_API_KEY not set"}

    from tinyfish import TinyFish  # type: ignore

    client = TinyFish(api_key=API_KEY)
    query = "RBI digital lending apps directory"
    try:
        resp = client.search.query(query)
        # Capture the documented fields defensively.
        raw = {
            "type": type(resp).__name__,
            "query": getattr(resp, "query", None),
            "total_results": getattr(resp, "total_results", None),
            "results": [],
        }
        results = getattr(resp, "results", None) or []
        for i, item in enumerate(results[:3]):
            entry: dict[str, Any] = {"index": i}
            for field in ("title", "url", "snippet", "description"):
                entry[field] = getattr(item, field, "<missing>")
            raw["results"].append(entry)
        # Also dump the full object via its __dict__ / model_dump if available.
        try:
            raw["_full_dump"] = resp.model_dump()  # pydantic v2
        except Exception:
            raw["_full_repr"] = repr(resp)
        return {"ok": True, "raw": raw}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


# --------------------------------------------------------------------------- #
# 3. Main
# --------------------------------------------------------------------------- #
def main() -> int:
    banner("LoanGuard — TinyFish smoke test")
    print(f"dotenv loaded            : {_DOTENV}")
    print(f"TINYFISH_API_KEY         : {_mask(API_KEY)}")
    print(f"RBI_DLA_URL              : {RBI_DLA_URL or '<not set>'}")

    banner("STEP 1 — SDK import & introspection")
    sdk = introspect_sdk()
    if "import_error" in sdk:
        print("FAILED to import tinyfish:", sdk["import_error"])
        return 1
    print("tinyfish version         :", sdk.get("tinyfish_version"))
    print("TinyFish client          : present")
    print("AsyncTinyFish client     : present")
    groups = sdk.get("client_resources", {})
    for name in ("agent", "search", "fetch", "research", "monitors", "runs", "wallet", "profiles"):
        methods = groups.get(name)
        if methods:
            print(f"  {name:10s}: {', '.join(methods)}")
        else:
            print(f"  {name:10s}: <not found>")
    if "callable" in groups:
        print(f"  (top-level callables): {', '.join(groups['callable'])}")

    banner("STEP 2 — Live Search probe")
    probe = live_search_probe()
    if probe.get("skipped"):
        print(probe["skipped"])
        print("\nNo live call made. To inspect a raw response, set TINYFISH_API_KEY")
        print("in a .env file (copy .env.example) and re-run this script.")
    elif probe.get("ok"):
        print("Search call succeeded. Raw response shape:")
        print(json.dumps(probe["raw"], indent=2, default=str)[:4000])
    else:
        print("Search call FAILED:", probe.get("error"))

    banner("STEP 3 — Playwright connect_over_cdp availability")
    try:
        from playwright.sync_api import sync_playwright

        p = sync_playwright().start()
        print("playwright chromium path :", p.chromium.executable_path)
        # confirm connect_over_cdp exists on the chromium object
        print("chromium.connect_over_cdp : callable =", callable(p.chromium.connect_over_cdp))
        p.stop()
    except Exception as exc:  # pragma: no cover
        print("Playwright check failed:", repr(exc))

    banner("SUMMARY")
    print("Verified without a key : SDK import, method list, Playwright CDP method.")
    print("Needs a live key       : actual response schemas (run with key to capture).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
