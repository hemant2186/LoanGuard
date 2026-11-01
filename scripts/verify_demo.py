"""End-to-end verification of the demo-mode SSE pipeline (no server required).

Consumes main.investigate_stream() in DEMO_MODE and asserts all six stages plus
the final report appear with correct labels.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force demo mode for this verification regardless of .env
os.environ["DEMO_MODE"] = "1"

from main import investigate_stream  # noqa: E402


async def main() -> int:
    events = []
    async for raw in investigate_stream("SampleLoan Demo", "https://demo.sampleloan.example"):
        events.append(raw)
    print(f"Total SSE events: {len(events)}")
    steps = []
    has_final = False
    for e in events:
        # strip the "data: " prefix and trailing newlines
        payload = e.removeprefix("data: ").strip()
        import json
        d = json.loads(payload)
        step = d.get("step")
        steps.append(step)
        print(f"  step={step:9} status={str(d.get('status')):12} demo={d.get('demo')}")
        if step == "final":
            has_final = True
            rep = d.get("report", {})
            print(f"\nFINAL report: score={rep.get('score',{}).get('score')} "
                  f"verdict={rep.get('score',{}).get('verdict')} demo={rep.get('demo')}")
            print(f"rbi.checked={rep.get('rbi',{}).get('checked')} "
                  f"rbi.found={rep.get('rbi',{}).get('found')}")

    required = ["start", "search", "agent", "fetch", "browser", "research", "monitor", "scoring"]
    missing = [s for s in required if s not in steps]
    if missing:
        print(f"\nFAIL: missing stages: {missing}")
        return 1
    if not has_final:
        print("\nFAIL: no final event")
        return 1
    if not all(d.get("demo") for d in [json.loads(e.removeprefix('data: ').strip()) for e in events if json.loads(e.removeprefix('data: ').strip()).get('step') in ('search','agent','fetch','browser','research')]):
        print("\nFAIL: demo events not labelled demo=true")
        return 1
    print("\nPASS: all six stages + scoring + final present, demo-labelled.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
