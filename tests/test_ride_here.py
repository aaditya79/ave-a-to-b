"""Full trips through /run and /chat with every model stubbed, so they cost nothing: the Ride here trips from
Popular rides, and the friendly message when something breaks."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app

client = TestClient(app.app)
RIDES = json.loads((Path(__file__).resolve().parent.parent / "data" / "popular_rides.json").read_text())


def trip_question(start: str, end: str) -> str:
    """The page's tripQuestion() for a trip with no ride time ("Any time")."""
    return f"I bike from {start} to {end}. Which route should I take?"


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """Every agent stubbed: the Lead calls find_routes as its route_options tool would, then says DONE."""
    async def run(agent, text, context=None, hooks=None, max_turns=None):
        if agent.name == "Lead":
            start, end = re.search(r"I bike from (.+?) to (.+?)\. Which route", text).groups()
            app.find_routes(context, start, end)
            return type("Result", (), {"final_output": "DONE"})()
        out = {"Verifier": app.Check(ok=True, problems=[]),
               "Writer": app.Answer(summary=[app.Sentence(text="The Balanced route is a good choice here.", sources=[])],
                                    tradeoff=[], notes=[])}.get(agent.name, "Nothing to add.")
        return type("Result", (), {"final_output": out})()
    monkeypatch.setattr(app.Runner, "run", run)
    monkeypatch.setattr(app, "CACHE_FILE", tmp_path / "cache.json")
    app.answer_cache.clear()
    app.asked.clear()
    app.day_count.update(day="", n=0)


def run_events(message: str) -> list[dict]:
    with client.stream("GET", "/run", params={"message": message}) as response:
        return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]


def test_central_park_ride_here_trip_finishes(stubbed):
    # Regression: a list named events was shadowed by the stream function, so every /run failed with
    # "AttributeError: 'function' object has no attribute 'append'" on its first event
    events = run_events(trip_question("Columbus Circle", "Harlem Meer"))
    assert [e for e in events if e["type"] == "error"] == []
    assert events[-1]["type"] == "done" and events[-1]["map"]["routes"]
    assert any(e["type"] == "routes" for e in events)


@pytest.mark.parametrize("ride", RIDES, ids=lambda r: r["name"])
def test_every_ride_here_trip_finishes(stubbed, ride):
    events = run_events(trip_question(ride["start"], ride["end"]))
    assert events[-1]["type"] == "done", events[-1]
    assert events[-1]["map"] and events[-1]["map"]["routes"], f"{ride['start']} to {ride['end']} found no routes"


def test_a_forced_exception_shows_the_friendly_message(stubbed, monkeypatch, caplog):
    async def broken(*args, **kwargs):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(app, "answer", broken)
    events = run_events(trip_question("Columbus Circle", "Harlem Meer"))
    assert events[-1] == {"type": "error", "message": app.FRIENDLY_ERROR, "retry": True, "busy": False}
    reply = client.post("/chat", json={"message": trip_question("Columbus Circle", "Harlem Meer")}).json()
    assert reply["response"] == app.FRIENDLY_ERROR and reply["error"] is True
    assert "secret internal detail" not in json.dumps(events) + json.dumps(reply)
    assert "secret internal detail" in caplog.text  # the full traceback is in the server log


def test_a_trip_from_map_pin_coordinates_finishes(stubbed):
    # "Pick on map" fills coordinates when the address lookup fails; they pass the guardrail and route
    events = run_events(trip_question("40.7484, -73.9857", "40.7359, -73.9911"))
    assert events[-1]["type"] == "done" and events[-1]["map"]["routes"], events[-1]
    assert abs(events[-1]["map"]["start"]["lat"] - 40.7484) < 1e-6


def slow_writer(monkeypatch, slow_call: int, problems: list[str]):
    """The pipeline stubbed as above, with the Writer's slow_call-th call taking far past the time budget."""
    import asyncio
    writer_calls = {"n": 0}

    async def run(agent, text, context=None, hooks=None, max_turns=None):
        if agent.name == "Lead":
            start, end = re.search(r"I bike from (.+?) to (.+?)\. Which route", text).groups()
            app.find_routes(context, start, end)
            return type("Result", (), {"final_output": "DONE"})()
        if agent.name == "Writer":
            writer_calls["n"] += 1
            if writer_calls["n"] == slow_call:
                await asyncio.sleep(30)
            out = app.Answer(summary=[app.Sentence(text="The Balanced route is a good choice here.", sources=[])], tradeoff=[], notes=[])
        elif agent.name == "Verifier":
            out = app.Check(ok=not problems, problems=problems)
        else:
            out = "Nothing to add."
        return type("Result", (), {"final_output": out})()
    monkeypatch.setattr(app.Runner, "run", run)
    monkeypatch.setattr(app, "time_left", lambda started, floor=20: 0.3)  # a budget that's nearly spent


def test_a_writer_past_the_time_budget_ends_with_retry(stubbed, monkeypatch):
    import time
    slow_writer(monkeypatch, slow_call=1, problems=[])
    began = time.monotonic()
    events = run_events(trip_question("Columbus Circle", "Harlem Meer"))
    assert time.monotonic() - began < 10
    assert events[-1] == {"type": "error", "message": app.FRIENDLY_ERROR, "retry": True, "busy": False}


def test_a_rewrite_past_the_time_budget_sends_the_draft_with_its_flags(stubbed, monkeypatch):
    slow_writer(monkeypatch, slow_call=2, problems=["The draft says 9 minutes; the results say 8."])
    events = run_events(trip_question("Columbus Circle", "Harlem Meer"))
    assert events[-1]["type"] == "done" and events[-1]["unverified"] == ["The draft says 9 minutes; the results say 8."]
    assert "The Balanced route is a good choice here." in events[-1]["response"]


def test_the_whole_answer_fits_in_five_minutes():
    assert app.TOTAL_SECONDS + 15 < 300 and app.RESEARCH_SECONDS < app.TOTAL_SECONDS
