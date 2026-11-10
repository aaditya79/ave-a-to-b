"""Recomputes every code-built crash range in the saved runs with today's range function (tools.count_range):
the route table, the stretches that stand out, and the before and after cards, in data/saved_examples.json and
docs/traces.json. No model is called. Text the models wrote (the Writer's sentences, the Researchers'
findings, and the recorded tool results) is left as it was recorded.

Run from the repo root after a change to tools.range_bounds:
    .venv/bin/python build/refresh_ranges.py
then build/render_traces.py and build/verify_samples.py.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tools  # noqa: E402

# A count with its range in the code-built markdown: "7 crashes since 2021 (range 1.7 to 12.3)"
IN_TEXT = re.compile(r"(\b\d+) crash(es)? since 2021 \(range (\d+(?:\.\d+)?) to (\d+(?:\.\d+)?)\)")


def fix_counts(value) -> int:
    """Every {n, low, high} in a map, recomputed in place. Returns how many changed."""
    changed = 0
    if isinstance(value, dict):
        if set(value) >= {"n", "low", "high"} and isinstance(value["n"], int):
            fresh = tools.count_range(value["n"])
            changed += (value["low"], value["high"]) != (fresh["low"], fresh["high"])
            value["low"], value["high"] = fresh["low"], fresh["high"]
        for v in value.values():
            changed += fix_counts(v)
    elif isinstance(value, list):
        for v in value:
            changed += fix_counts(v)
    return changed


def fix_text(text: str) -> tuple[str, int]:
    """The same ranges where code wrote them into the answer's markdown."""
    def new(m):
        fresh = tools.count_range(int(m[1]))
        return f"{m[1]} crash{m[2] or ''} since 2021 (range {fresh['low']:g} to {fresh['high']:g})"
    return IN_TEXT.subn(new, text)


def writer_text(answer: dict | None) -> str:
    return json.dumps(answer or {})


def refresh_run(run: dict) -> int:
    """One /chat response or done event: its map, and its markdown outside the Writer's own sentences."""
    changed = fix_counts(run.get("map"))
    if run.get("response"):
        before = writer_text(run.get("answer"))
        run["response"], n = fix_text(run["response"])
        assert writer_text(run.get("answer")) == before
        changed += n
    return changed


def main() -> None:
    path = ROOT / "data" / "saved_examples.json"
    saved = json.loads(path.read_text())
    for key, example in saved.items():
        changed = 0
        for event in example["events"]:
            if event["type"] == "routes":
                changed += fix_counts(event["data"])
            elif event["type"] == "done":
                changed += refresh_run(event)
        print(f"saved_examples.json {key}: {changed} ranges updated")
    path.write_text(json.dumps(saved, separators=(",", ":")))  # as save_examples.py writes it

    path = ROOT / "docs" / "traces.json"
    traces = json.loads(path.read_text())
    for run in traces:
        print(f"traces.json {run['question'][:50]}: {refresh_run(run)} ranges updated")
    path.write_text(json.dumps(traces, indent=1))  # as the trace export writes it


if __name__ == "__main__":
    main()
