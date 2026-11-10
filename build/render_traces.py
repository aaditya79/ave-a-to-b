"""Writes docs/traces.md from docs/traces.json (the raw /chat responses from the live site).

    ../.venv/bin/python render_traces.py
"""
import json
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
URL = "https://ave-a-to-b-929001279438.us-east1.run.app"

HEADER = f"""# Traces: the sample queries on the live site

Each query was sent to `POST /chat` at {URL}. Every tool call lists the
agent that made it and when it finished, in seconds from the start of the run, so calls from different Researchers
that finish at nearly the same time ran in parallel. `Pipeline` rows are code: the stretches that stand out and the
before/after results, which the page and the answer show exactly as computed. `Verifier` rows are code re-running
the tool calls behind every ref and edge ID the Writer cited, before the Verifier model compares them.
The raw responses, with full tool results, are in `docs/traces.json`.
"""


def cell(text: str, limit: int) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text[:limit]


def verdict(run: dict) -> str:
    check = run.get("verification")
    if run.get("clarify"):
        return "No Writer or Verifier: the Lead stopped to ask a clarifying question."
    if not check:
        return "Verifier: the check did not finish in time."
    if check.get("ok"):
        return "Verifier: every cited number matched a fresh run of the tools."
    return "Verifier found these problems, which the Writer then fixed:\n\n" + "\n".join(f"- {p}" for p in check["problems"])


def render(run: dict) -> str:
    calls = run["tool_calls"]
    lines = [f"## {run['question']}", "",
             f"{run['seconds']} s on the live site, {len(calls)} tool call{'s' if len(calls) != 1 else ''}.", "", verdict(run), ""]
    if calls:
        lines += ["| finished at (s) | agent | tool | args | result (start) |", "|---:|---|---|---|---|"]
        for c in calls:
            args = json.dumps(c["args"]) if not isinstance(c["args"], str) else c["args"]
            lines.append(f"| {c.get('finished_at_seconds', '')} | {c['agent']} | {c['name']} | `{cell(args, 160)}` | {cell(c['result'], 180)} |")
        lines.append("")
    lines += ["### Answer", "", run["response"].strip(), ""]
    if run.get("clarify") and run.get("options"):
        lines += ["Quick replies offered:", "", *[f"- {o}" for o in run["options"]], ""]
    return "\n".join(lines)


def main():
    runs = json.loads((DOCS / "traces.json").read_text())
    (DOCS / "traces.md").write_text(HEADER + "\n" + "\n".join(render(r) for r in runs))


if __name__ == "__main__":
    main()
