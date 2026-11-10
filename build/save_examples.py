"""Records the three example trips from a running copy of the app into data/saved_examples.json.

    .venv/bin/python build/save_examples.py https://ave-a-to-b-929001279438.us-east1.run.app

Each example keeps every event the page received, in order, so the page can replay it at once.
The page always shows these under a "Saved example" banner with the time below, never as a live run.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

OUT = Path(__file__).resolve().parent.parent / "data" / "saved_examples.json"
# The same trips as the chips on the page (data-key on each chip)
TRIPS = {
    "columbia-union-sq": {"start": "Columbia University", "end": "Union Square, Manhattan", "when": "around 8am"},
    "prospect-heights-dumbo": {"start": "Prospect Heights, Brooklyn", "end": "DUMBO, Brooklyn", "when": "in the evening, around 6pm"},
    "astoria-times-sq": {"start": "Astoria, Queens", "end": "Times Square", "when": ""},
}


def question(t: dict) -> str:
    """The page's tripQuestion, word for word."""
    return f"I bike from {t['start']} to {t['end']}{' ' + t['when'] if t['when'] else ''}. Which route should I take?"


def record(url: str, trip: dict) -> dict:
    events, started = [], time.monotonic()
    with httpx.stream("GET", f"{url}/run", params={"message": question(trip)}, timeout=600) as reply:
        for line in reply.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "start":
                continue  # that session lives on one server instance; a replay starts its own
            events.append(event)
            if event["type"] in ("done", "error"):
                break
    done = events[-1]
    if done["type"] != "done" or not done.get("map"):
        raise RuntimeError(f"No full answer for {trip}: {done}")
    saved_at = datetime.now(ZoneInfo("America/New_York"))
    return {"trip": trip, "question": question(trip), "saved_at": saved_at.isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - started, 1), "events": events}


def main(url: str) -> None:
    saved = {}
    for key, trip in TRIPS.items():
        saved[key] = record(url, trip)
        print(key, saved[key]["seconds"], "s,", len(saved[key]["events"]), "events")
    OUT.write_text(json.dumps(saved, separators=(",", ":")))
    print(f"{OUT.name}: {OUT.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main(sys.argv[1].rstrip("/"))
