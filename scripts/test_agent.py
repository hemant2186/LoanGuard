"""
LoanGuard — PHASE 2: RBI Agent integration test.

Verifies the TinyFish Agent can check the RBI "Digital Lending Apps deployed by
Regulated Entities" directory for a given application name and return structured
JSON.

What it does:
  * Uses the installed TinyFish 0.11.0 SDK (AsyncTinyFish.agent.run()).
  * Starts at https://www.rbi.org.in/ and asks the Agent to navigate to the
    public digital lending apps directory (Citizen's Corner -> "DLA's deployed
    by Regulated Entities"). The directory may be a dynamic BI table, iframe or
    interactive document, so the Agent is told to interact with it directly.
  * Requests structured output via output_schema with these fields:
      checked, found, matched_name, lending_entity, evidence, failure_reason
  * Prints the COMPLETE sanitized raw response before any parsing.
  * Maps the result to LoanGuard's internal normalized fields.
  * Uses a hard timeout + exception handling. NO retry loops.
  * Never treats a failed lookup as confirmed absence (checked=false on failure).

Run:
  python scripts/test_agent.py
  python scripts/test_agent.py "KreditBee"

The TINYFISH_API_KEY is read from the environment / .env and is never printed.
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


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
API_KEY = os.getenv("TINYFISH_API_KEY", "").strip()
RBI_DLA_URL = os.getenv("RBI_DLA_URL", "https://www.rbi.org.in/").strip()
# Hard cap so a hung run fails cleanly instead of hanging the demo.
RUN_TIMEOUT_S = float(os.getenv("AGENT_RUN_TIMEOUT_S", "240"))

APP_NAME = sys.argv[1] if len(sys.argv) > 1 else "KreditBee"


def _mask(value: str | None) -> str:
    if not value:
        return "<not set>"
    return value[:4] + "..." + value[-4:]


def _sanitize(obj: Any, secret: str) -> Any:
    """Recursively redact anything that looks like the API key or its env name."""
    if isinstance(obj, dict):
        return {k: ("***REDACTED***" if k.upper() == "TINYFISH_API_KEY" else _sanitize(v, secret)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v, secret) for v in obj]
    if isinstance(obj, str):
        if secret and obj == secret:
            return "***REDACTED***"
        return obj
    return obj


# --------------------------------------------------------------------------- #
# Goal + structured output schema
# --------------------------------------------------------------------------- #
def build_goal(app_name: str) -> str:
    return f"""You are on the Reserve Bank of India public website (rbi.org.in). Your ONLY task is to check the RBI's public list of Digital Lending Apps (DLAs) deployed by Regulated Entities for an application named "{app_name}".

NAVIGATION (follow exactly — the directory sits behind a tokenized redirect, so you MUST click the real link, not a copied URL):
1. Open https://www.rbi.org.in/ and wait for the homepage to finish loading.
2. Locate the visible link labelled "DLA’s deployed by Regulated Entities" (also accept the ASCII-apostrophe variant "DLA's deployed by Regulated Entities").
3. Scroll the link into view if necessary.
4. CLICK the actual visible link using the current browser session.
5. Do NOT copy the link's href and independently navigate to the bare URL — that drops the token and returns "Unauthorized Access". You must click the element itself.
6. Allow all redirects to complete fully.
7. Preserve cookies, session state and any query parameters / tokens in the URL throughout.
8. If a new tab or popup opens, switch to it and continue there.

ON THE DIRECTORY PAGE:
9. The directory may be a dynamic/interactive table, an embedded iframe, a Power BI / document viewer, or a paginated list. Inspect the resulting page and identify the directory table or search interface. Interact with it directly: use any search or filter field present, scroll, follow pagination / "view all" / expand controls, and switch tabs or pages as needed. Do NOT assume static HTML.

SEARCH:
10. Search for the application named "{app_name}". Use careful fuzzy matching to handle spacing/capitalization variants (e.g. "{app_name}", "{app_name.replace(' ', '')}", "{app_name.lower()}"). Avoid false positives on unrelated apps that merely share a word.
11. Extract the matched name and the regulated lending entity (NBFC/bank) ONLY when supported by actual directory evidence shown on the page.

REPORTING:
12. If a matching app is listed: set found=true with its exact listed name, the regulated lending entity, and a short verbatim quote of the evidence.
13. If you reached and could search the directory but "{app_name}" is not listed after a reasonable search: report found=false AND checked=true.
14. If you CANNOT reach or access the directory at all (e.g. "Unauthorized Access", page missing, blocked by captcha/login, content failed to load, navigation failed): report checked=false and put the actual error evidence in failure_reason. Do NOT report a failed lookup as a confirmed absence — found must be false when checked is false.

CONSTRAINTS:
- Do not hardcode or guess a generated token.
- Do not strip URL parameters.
- Do not retry repeatedly.
- Do not treat an unauthorized response as absence.

Return the result strictly as the structured JSON requested."""


# Minimal schema — no description/title/nullable keywords (the API rejects them).
# Unavailable string values come back as empty strings ("") instead of null.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "checked": {"type": "boolean"},
        "found": {"type": "boolean"},
        "matched_name": {"type": "string"},
        "lending_entity": {"type": "string"},
        "evidence": {"type": "string"},
        "failure_reason": {"type": "string"},
    },
    "required": ["checked", "found", "matched_name", "lending_entity", "evidence", "failure_reason"],
}


# --------------------------------------------------------------------------- #
# Normalized internal result (our fields, NOT assumed TinyFish response fields)
# --------------------------------------------------------------------------- #
def normalize(agent_result: dict | None, status: Any, error: Any) -> dict[str, Any]:
    """Map the agent's structured output to LoanGuard's internal shape, safely."""
    base = {
        "checked": False,
        "found": False,
        "matched_name": None,
        "lending_entity": None,
        "evidence": None,
        "failure_reason": None,
    }
    if isinstance(agent_result, dict):
        for k in base:
            if k in agent_result:
                base[k] = agent_result[k]
    # If the run itself failed, never let "found" be true and record the reason.
    if str(status) not in ("RunStatus.COMPLETED", "COMPLETED", "completed") and not isinstance(agent_result, dict):
        base["failure_reason"] = (
            f"Agent run did not complete (status={status}). "
            + (f"error={getattr(error, 'message', error)}" if error else "")
        )
    return base


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def run() -> int:
    print("=" * 72)
    print("LoanGuard — RBI Agent integration test (PHASE 2)")
    print("=" * 72)
    print(f"TINYFISH_API_KEY  : {_mask(API_KEY)}")
    print(f"RBI_DLA_URL       : {RBI_DLA_URL}")
    print(f"Application name  : {APP_NAME}")
    print(f"Run timeout (s)   : {RUN_TIMEOUT_S}")

    if not API_KEY:
        print("\nERROR: TINYFISH_API_KEY is not set. Put it in .env and retry.")
        return 2

    from tinyfish import AsyncTinyFish  # type: ignore

    client = AsyncTinyFish(api_key=API_KEY, timeout=300.0, max_retries=0)

    goal = build_goal(APP_NAME)
    print("\n--- Goal (truncated) ---")
    print(goal[:500] + ("..." if len(goal) > 500 else ""))

    raw: dict[str, Any] = {}
    try:
        print("\nCalling client.agent.run() ... (this can take 15-120s)")
        response = await asyncio.wait_for(
            client.agent.run(goal=goal, url=RBI_DLA_URL, output_schema=OUTPUT_SCHEMA),
            timeout=RUN_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        print("\n[TIMEOUT] Agent run exceeded the time budget. No retry attempted.")
        raw = {"_outcome": "timeout", "_timeout_s": RUN_TIMEOUT_S}
        print("\n--- RAW RESPONSE (sanitized) ---")
        print(json.dumps(_sanitize(raw, API_KEY), indent=2, default=str))
        return 1
    except Exception as exc:
        print(f"\n[ERROR] Agent run raised an exception: {type(exc).__name__}: {exc}")
        raw = {"_outcome": "exception", "_type": type(exc).__name__, "_message": str(exc)}
        print("\n--- RAW RESPONSE (sanitized) ---")
        print(json.dumps(_sanitize(raw, API_KEY), indent=2, default=str))
        return 1

    # ---- Capture the COMPLETE raw response before parsing ----
    try:
        raw = response.model_dump()  # pydantic v2
    except Exception:
        raw = {
            "type": type(response).__name__,
            "status": getattr(response, "status", None),
            "run_id": getattr(response, "run_id", None),
            "result": getattr(response, "result", None),
            "error": repr(getattr(response, "error", None)),
            "num_of_steps": getattr(response, "num_of_steps", None),
            "started_at": str(getattr(response, "started_at", None)),
            "finished_at": str(getattr(response, "finished_at", None)),
        }

    print("\n--- RAW RESPONSE (sanitized, complete) ---")
    print(json.dumps(_sanitize(raw, API_KEY), indent=2, default=str))

    # ---- Parse into LoanGuard's normalized internal fields ----
    status = getattr(response, "status", None)
    error = getattr(response, "error", None)
    result = getattr(response, "result", None)
    normalized = normalize(result if isinstance(result, dict) else None, status, error)

    print("\n--- NORMALIZED INTERNAL RESULT ---")
    print(json.dumps(normalized, indent=2, default=str))

    # Sanity guard: a failed check must never read as confirmed absence.
    if not normalized["checked"] and normalized["found"]:
        print("\n[GUARD] checked=false but found=true — coercing found=false (no confirmed absence on failure).")
        normalized["found"] = False

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
