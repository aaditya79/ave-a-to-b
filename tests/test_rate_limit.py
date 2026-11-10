"""The model service saying "busy" (429 RESOURCE_EXHAUSTED): backoff, the shared limit on calls at once, the
Researcher fallback, and the message when a whole run can't get through. Every model is stubbed."""
import asyncio
import json

import litellm
import pytest
from agents.extensions.models.litellm_model import LitellmModel
from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


def busy_error():
    return litellm.RateLimitError("vertex_aiException - RESOURCE_EXHAUSTED", llm_provider="vertex_ai", model="gemini-3.5-flash")


@pytest.fixture
def sleeps(monkeypatch):
    """Record the backoff waits instead of waiting."""
    waited = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *args, **kwargs):
        waited.append(seconds)
        await real_sleep(0)
    monkeypatch.setattr(app.asyncio, "sleep", fake_sleep)
    return waited


def script(monkeypatch, failures: int | None):
    """LitellmModel.get_response fails with a 429 `failures` times (None: always), then answers."""
    calls = {"n": 0}

    async def get_response(self, *args, **kwargs):
        calls["n"] += 1
        if failures is None or calls["n"] <= failures:
            raise busy_error()
        return "answer"
    monkeypatch.setattr(LitellmModel, "get_response", get_response)
    return calls


def test_busy_twice_then_it_answers_after_backing_off(monkeypatch, sleeps):
    calls = script(monkeypatch, failures=2)
    assert asyncio.run(app.MODEL.get_response()) == "answer"
    assert calls["n"] == 3
    assert len(sleeps) == 2 and 1.5 <= sleeps[0] <= 2.5 and 3 <= sleeps[1] <= 5  # about 2, then 4 seconds, with jitter


def test_always_busy_gives_up_after_five_tries(monkeypatch, sleeps):
    calls = script(monkeypatch, failures=None)
    with pytest.raises(litellm.RateLimitError):
        asyncio.run(app.MODEL.get_response())
    assert calls["n"] == app.MAX_TRIES == 5
    assert [round(s / b) for s, b in zip(sleeps, [2, 4, 8, 16])] == [1, 1, 1, 1]  # 2, 4, 8, 16 seconds, ±25%
    assert app.SETTINGS.extra_args["num_retries"] == 0  # no fast LiteLLM retries underneath


def test_other_errors_are_not_retried(monkeypatch, sleeps):
    async def broken(self, *args, **kwargs):
        raise ValueError("a bug")
    monkeypatch.setattr(LitellmModel, "get_response", broken)
    with pytest.raises(ValueError):
        asyncio.run(app.MODEL.get_response())
    assert sleeps == []


def test_at_most_three_model_calls_at_once(monkeypatch):
    now = {"in": 0, "most": 0}

    async def slow(self, *args, **kwargs):
        now["in"] += 1
        now["most"] = max(now["most"], now["in"])
        await asyncio.sleep(0.02)
        now["in"] -= 1
        return "ok"
    monkeypatch.setattr(LitellmModel, "get_response", slow)

    async def fan_out():  # eight calls started together, from both models, as a fan-out of Researchers would
        return await asyncio.gather(*[(app.MODEL if i % 2 else app.RESEARCHER_MODEL).get_response() for i in range(8)])
    assert asyncio.run(fan_out()) == ["ok"] * 8
    assert now["most"] == app.MAX_MODEL_CALLS == 3


def test_vertex_calls_use_the_global_endpoint():
    if app.os.getenv("GEMINI_API_KEY"):
        pytest.skip("the Gemini API has no locations")
    assert app.SETTINGS.extra_args["vertex_location"] == "global"


def test_a_busy_researcher_falls_back_once_to_the_other_model(monkeypatch):
    used = []

    async def run(agent, text, context=None, hooks=None, max_turns=None):
        used.append(agent.model)
        if agent.model is app.RESEARCHER_MODEL:
            raise busy_error()
        return type("Result", (), {"final_output": "Findings [route:abcdef12]"})()
    monkeypatch.setattr(app.Runner, "run", run)
    hooks = app.TraceHooks(app.AgentContexts(), lambda event: None)
    research = app.Research(hooks=hooks)
    text, status = asyncio.run(app.run_researcher(app.RESEARCHERS["crash"].clone(name="Crash Researcher 1"), "Task: x", research))
    assert (text, status) == ("Findings [route:abcdef12]", "done")
    assert used == [app.RESEARCHER_MODEL, app.RESEARCHER_FALLBACK]
    note = [h for h in hooks.handoffs if h["kind"] == "retry"]
    assert len(note) == 1 and "busy (429)" in note[0]["text"] and "gemini-3.5-flash" in note[0]["text"]


def test_a_researcher_busy_on_both_models_is_marked_failed_not_retried_again(monkeypatch):
    used = []

    async def run(agent, text, context=None, hooks=None, max_turns=None):
        used.append(agent.model)
        raise busy_error()
    monkeypatch.setattr(app.Runner, "run", run)
    research = app.Research(hooks=app.TraceHooks(app.AgentContexts(), lambda event: None))
    text, status = asyncio.run(app.run_researcher(app.RESEARCHERS["exposure"].clone(name="Exposure Researcher 1"), "Task: x", research))
    assert status == "failed" and len(used) == 2


# --- Whole runs through /run and /chat ---

def trip_events():
    with client.stream("GET", "/run", params={"message": "I bike from Columbus Circle to Harlem Meer. Which route should I take?"}) as r:
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


@pytest.fixture
def stubbed_trip(monkeypatch, tmp_path):
    """The full pipeline, with each agent's run going through MODEL.get_response (scripted) before it answers."""
    import re

    async def run(agent, text, context=None, hooks=None, max_turns=None):
        await app.MODEL.get_response()
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
    monkeypatch.setattr(app, "BACKOFF_SECONDS", 0.001)
    app.answer_cache.clear()


def test_a_trip_gets_through_when_the_service_is_busy_twice(monkeypatch, stubbed_trip):
    calls = script(monkeypatch, failures=2)
    events = trip_events()
    assert events[-1]["type"] == "done" and events[-1]["map"]["routes"]
    assert calls["n"] > 3  # the first call waited through two 429s, the rest went straight through


def test_a_trip_that_stays_busy_says_so_with_retry(monkeypatch, stubbed_trip):
    script(monkeypatch, failures=None)
    events = trip_events()
    assert events[-1] == {"type": "error", "message": app.BUSY_MESSAGE, "retry": True, "busy": True}
    assert app.BUSY_MESSAGE == "The AI service is busy right now. Please try again in a minute, or try a saved example."
    reply = client.post("/chat", json={"message": "I bike from Columbus Circle to Harlem Meer. Which route should I take?"}).json()
    assert reply["response"] == app.BUSY_MESSAGE and reply["busy"] is True and reply["error"] is True and reply["map"] is None


def test_other_failures_keep_the_generic_message(monkeypatch, stubbed_trip):
    async def broken(*args, **kwargs):
        raise RuntimeError("something else")
    monkeypatch.setattr(app, "answer", broken)
    events = trip_events()
    assert events[-1] == {"type": "error", "message": app.FRIENDLY_ERROR, "retry": True, "busy": False}
