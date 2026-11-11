"""The Verifier and daily round-trip minutes. The full /chat pipeline runs with a scripted model in place of
Gemini (the Agents SDK, tools and handoffs run for real); no model is called."""
import json
import re

import pytest
from agents.items import ModelResponse
from agents.usage import Usage
from fastapi.testclient import TestClient
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

import app
import tools as route_tools

TRIP = ("Columbia University", "Union Square, Manhattan")


def text(t):
    return ResponseOutputMessage(id="m", type="message", role="assistant", status="completed",
                                 content=[ResponseOutputText(type="output_text", text=t, annotations=[])])


def call(name, args):
    return ResponseFunctionToolCall(type="function_call", id="fc1", call_id="c1", name=name, arguments=json.dumps(args))


class Scripted(app.ThrottledModel):
    """Lead: route_options then DONE. Writer: the draft (and the fix on a second call). Verifier: the given flags."""

    def __init__(self, draft, fixed, problems):
        super().__init__(model="scripted", api_key="unused")
        self.draft, self.fixed, self.problems, self.writer_calls, self.verifier_prompt = draft, fixed, problems, 0, ""

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kw):
        done = [x for x in input if isinstance(x, dict) and x.get("type") == "function_call_output"] if isinstance(input, list) else []
        prompt = input if isinstance(input, str) else json.dumps(input)
        if output_schema is not None and "summary" in json.dumps(output_schema.json_schema()):
            self.writer_calls += 1
            sentence = self.draft if self.writer_calls == 1 else self.fixed
            out = text(json.dumps({"summary": [{"text": "We recommend the Balanced route.", "sources": []}],
                                   "tradeoff": [{"text": sentence, "sources": []}], "notes": []}))
        elif output_schema is not None:
            self.verifier_prompt = prompt
            out = text(json.dumps({"ok": not self.problems, "problems": self.problems}))
        elif any(t.name == "route_options" for t in tools):
            out = call("route_options", {"start": TRIP[0], "end": TRIP[1]}) if not done else text("DONE")
        else:
            out = text("Checked.")
        return ModelResponse(output=[out], usage=Usage(), response_id=None)


@pytest.fixture(scope="module")
def one_way():
    """The Balanced route's extra minutes each way, as the pipeline rounds them."""
    return round(route_tools.route_options(*TRIP)["routes"]["balanced"]["extra_minutes_vs_fastest"], 1)


def run(monkeypatch, draft, problems, fixed="The Balanced route takes a little longer."):
    model = Scripted(draft, fixed, problems)
    for agent in [app.lead, app.writer, app.verifier, *app.RESEARCHERS.values()]:
        monkeypatch.setattr(agent, "model", model)
    monkeypatch.setattr(app, "RESEARCHER_FALLBACK", model)
    app.asked.clear(); app.day_count.update(day="", n=0); app.answer_cache.clear()
    monkeypatch.setattr(app, "CACHE_FILE", app.Path("/tmp/does-not-exist-verifier-test.json"))
    reply = TestClient(app.app).post("/chat", json={"message": f"I bike from {TRIP[0]} to {TRIP[1]}. Which route should I take?"}).json()
    return reply, model


def test_the_verifier_gets_one_way_and_daily_minutes(monkeypatch, one_way):
    reply, model = run(monkeypatch, f"The Balanced route adds {round(one_way * 2, 1):g} minutes daily, round trip.", [])
    assert f"{one_way:g} one way; {round(one_way * 2, 1):g} a day, round trip" in model.verifier_prompt
    assert "don't halve it" in app.verifier.instructions


@pytest.mark.parametrize("phrasing", ["adds {d} minutes of travel time daily", "means {d} minutes of daily travel time",
                                      "costs {d} extra minutes a day on a round trip"])
def test_a_correct_daily_figure_is_accepted(monkeypatch, one_way, phrasing):
    daily = round(one_way * 2, 1)
    draft = "The Balanced route " + phrasing.format(d=f"{daily:.1f}") + "."
    # A Verifier that mixes up one way and daily, as on the live runs (7.4 -> 3.7, 7.0 -> 3.5)
    wrong = f"The draft says {daily:.1f} minutes daily, but the route options say {one_way:g} extra minutes. Change it to {one_way:g}."
    reply, model = run(monkeypatch, draft, [wrong])
    assert reply["verification"] == {"ok": True, "problems": []}
    assert model.writer_calls == 1 and f"{daily:.1f}" in reply["response"]  # no rewrite: the draft's daily figure stands
    assert not reply.get("unverified")


def test_a_wrong_daily_figure_is_still_fixed(monkeypatch, one_way):
    daily = round(one_way * 2, 1)
    draft = f"The Balanced route adds {one_way:g} minutes daily."  # the one-way figure, called daily: wrong
    flag = f"The draft says {one_way:g} minutes daily, but round trip it is {daily:g} a day. Change it to {daily:g}."
    reply, model = run(monkeypatch, draft, [flag], fixed=f"The Balanced route adds {daily:g} minutes a day, round trip.")
    assert reply["verification"]["problems"] == [flag] and model.writer_calls == 2
    assert f"{daily:g} minutes a day" in reply["response"]


def test_other_flags_are_kept(monkeypatch, one_way):
    daily = round(one_way * 2, 1)
    other = "The draft says 80% lower crash risk; the route options say 62% lower. Change it to 62%."
    reply, model = run(monkeypatch, f"The Balanced route adds {daily:.1f} minutes daily.",
                       [f"Draft says {daily:.1f} daily; options say {one_way:g}. Use {one_way:g}.", other])
    assert reply["verification"]["problems"] == [other] and model.writer_calls == 2


def test_drop_round_trip_flags_unit():
    routes = {"routes": {"fastest": {"extra_minutes_vs_fastest": 0}, "balanced": {"extra_minutes_vs_fastest": 3.7}}}
    halving = ["Draft says 7.4 minutes daily; results say 3.7. Fix to 3.7."]
    assert app.drop_round_trip_flags(halving, "Balanced adds 7.4 minutes of travel time daily.", routes) == []
    assert app.drop_round_trip_flags(halving, "Balanced adds 3.7 minutes daily.", routes) == halving  # draft was wrong
    assert app.drop_round_trip_flags(halving, "Balanced adds 7.4 minutes each way.", routes) == halving  # not stated as daily


def test_fresh_skips_the_answer_cache_and_plain_compare_reuses_it(monkeypatch, one_way):
    first, model = run(monkeypatch, "The Balanced route takes a little longer.", [])
    assert model.writer_calls == 1 and app.answer_cache  # a plain trip question is cached
    client = TestClient(app.app)
    message = f"I bike from {TRIP[0]} to {TRIP[1]}. Which route should I take?"
    again = Scripted("The Balanced route takes a little longer.", "", [])
    for agent in [app.lead, app.writer, app.verifier, *app.RESEARCHERS.values()]:
        monkeypatch.setattr(agent, "model", again)
    reused = client.post("/chat", json={"message": message}).json()
    assert again.writer_calls == 0 and reused.get("cached_at")  # Compare routes again: the cached answer
    fresh = client.post("/chat", json={"message": message, "fresh": True}).json()
    assert again.writer_calls == 1 and not fresh.get("cached_at")  # Run a fresh one: a new run
    with client.stream("GET", "/run", params={"message": message, "fresh": "1"}) as s:
        events = [json.loads(line[6:]) for line in s.iter_lines() if line.startswith("data: ")]
    assert again.writer_calls == 2 and not events[-1].get("cached_at")
