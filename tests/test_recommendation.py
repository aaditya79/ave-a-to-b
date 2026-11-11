"""One recommendation per trip, decided in code. Replays the DUMBO -> Chelsea Market live run (2026-11-11) whose
answer contradicted itself: the headline said Lowest risk, the Writer said Fastest, and the Verifier asked for
Fastest. The full /chat pipeline runs with a scripted model in place of Gemini; no model is called."""
import json
import re

import pytest
from agents.items import ModelResponse
from agents.usage import Usage
from fastapi.testclient import TestClient
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

import app
import tools as route_tools

TRIP = ("DUMBO, Brooklyn", "Chelsea Market, Manhattan")
MESSAGE = f"I bike from {TRIP[0]} to {TRIP[1]} in the evening, around 6pm. Which route should I take?"

# The live run's Writer draft and Verifier flags, as saved in its session
LIVE_DRAFT = {
    "summary": [{"text": "The routes are about equally safe, so we recommend the Fastest route (via Canal St and Manhattan Bridge) because it is faster.",
                 "sources": ["route:b3469724", "route:709a39e6"]},
                {"text": "It uses the Manhattan Bridge and Greenwich Street, and avoids the longer detour through Brooklyn Bridge Promenade.",
                 "sources": ["route:b3469724", "route:709a39e6"]}],
    "tradeoff": [{"text": "Taking the Lowest risk route instead adds 9.6 minutes per day to a round trip.", "sources": ["route:b3469724", "route:709a39e6"]}],
    "notes": [{"text": "The recommended route has 1.73 miles of protected lanes, compared to 3.95 miles on the lowest risk route.",
               "sources": ["route:b3469724", "route:709a39e6"]}],
}
LIVE_FLAGS = [
    "The draft recommends the Lowest risk route, but the safety_label is 'about_equally_safe' and the safety_note says to recommend the faster route. Fix: Recommend the Fastest route.",
    "The draft says the Lowest risk route is recommended \"because it is faster\", but the results show it is the slowest route at 31.2 minutes compared to 26.5 minutes for the fastest route. Fix: Recommend the Fastest route.",
]
GOOD = {"summary": [{"text": "The Balanced and Lowest risk routes are about equally safe, and the Balanced route is the faster of them.", "sources": []},
                    {"text": "It uses Prince St and Bedford St.", "sources": []}],
        "tradeoff": [{"text": "It adds 1.4 minutes a day, round trip.", "sources": []}], "notes": []}


def text(t):
    return ResponseOutputMessage(id="m", type="message", role="assistant", status="completed",
                                 content=[ResponseOutputText(type="output_text", text=t, annotations=[])])


def call(name, args):
    return ResponseFunctionToolCall(type="function_call", id="fc1", call_id="c1", name=name, arguments=json.dumps(args))


class Scripted(app.ThrottledModel):
    """Lead: route_options then DONE. Writer: drafts[0], then drafts[1] on the fix. Verifier: the given flags."""

    def __init__(self, drafts, flags):
        super().__init__(model="scripted", api_key="unused")
        self.drafts, self.flags, self.writer_prompts, self.verifier_prompt = drafts, flags, [], ""

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kw):
        done = [x for x in input if isinstance(x, dict) and x.get("type") == "function_call_output"] if isinstance(input, list) else []
        prompt = input if isinstance(input, str) else json.dumps(input)
        if output_schema is not None and "summary" in json.dumps(output_schema.json_schema()):
            self.writer_prompts.append(prompt)
            out = text(json.dumps(self.drafts[min(len(self.writer_prompts), len(self.drafts)) - 1]))
        elif output_schema is not None:
            self.verifier_prompt = prompt
            out = text(json.dumps({"ok": not self.flags, "problems": self.flags}))
        elif any(t.name == "route_options" for t in tools):
            out = call("route_options", {"start": TRIP[0], "end": TRIP[1]}) if not done else text("DONE")
        else:
            out = text("Checked.")
        return ModelResponse(output=[out], usage=Usage(), response_id=None)


def run(monkeypatch, drafts, flags):
    model = Scripted(drafts, flags)
    for agent in [app.lead, app.writer, app.verifier, *app.RESEARCHERS.values()]:
        monkeypatch.setattr(agent, "model", model)
    monkeypatch.setattr(app, "RESEARCHER_FALLBACK", model)
    app.asked.clear(); app.day_count.update(day="", n=0); app.answer_cache.clear()
    monkeypatch.setattr(app, "CACHE_FILE", app.Path("/tmp/does-not-exist-recommendation-test.json"))
    return TestClient(app.app).post("/chat", json={"message": MESSAGE}).json(), model


@pytest.fixture(scope="module")
def options():
    return route_tools.route_options(*TRIP)


def recommendation_section(response: str) -> str:
    return response.split("### Recommendation", 1)[1].split("###", 1)[0]


def test_the_rule_on_this_trip(options):
    """Balanced's range overlaps Lowest risk's at every setting; Fastest's is clearly above at all three.
    So Balanced and Lowest risk are about equally safe, and Balanced, the faster of the two, is recommended."""
    assert options["safety_label"] == "about_equally_safe"
    assert set(options["routes_about_equally_safe"]) == {"balanced", "lowest_risk"}
    assert options["recommended_route"] == "balanced"
    r = options["routes"]
    assert r["balanced"]["minutes"] < r["lowest_risk"]["minutes"]


def test_a_tie_always_has_two_routes():
    """If no route overlapped the lowest's range under any setting, the lowest would be the safest outright."""
    for start, end in [TRIP, ("Columbia University", "Union Square, Manhattan"), ("Astoria, Queens", "Times Square")]:
        o = route_tools.route_options(start, end)
        if o["safety_label"] == "about_equally_safe":
            refs = {o["routes"][k]["route_ref"] for k in o["routes_about_equally_safe"]}
            assert len(refs) >= 2, (start, end)
            fastest_of_them = min(o["routes_about_equally_safe"], key=lambda k: o["routes"][k]["minutes"])
            assert o["routes"][o["recommended_route"]]["route_ref"] == o["routes"][fastest_of_them]["route_ref"]


def test_the_writer_and_verifier_are_given_the_decision(monkeypatch):
    reply, model = run(monkeypatch, [GOOD], [])
    brief = model.writer_prompts[0]
    assert "recommend the Balanced route (route:" in brief
    assert "About equally safe" in brief and "Balanced, Lowest risk" in brief
    assert "Never recommend, or tell the rider to take, any route other than the Balanced route" in brief
    assert "Decision (made in code, final): recommend the Balanced route" in model.verifier_prompt
    assert "Recommend only that route" in app.WRITER_RULES and "Flag every sentence that\nrecommends" in app.verifier.instructions
    assert reply["verification"] == {"ok": True, "problems": []} and len(model.writer_prompts) == 1


def test_the_live_run_is_fixed(monkeypatch):
    """The live draft (Fastest) with the live flags (asking for Fastest): the flags asking for another route are set
    aside, code's own check sends the draft back, and the fix recommends Balanced."""
    reply, model = run(monkeypatch, [LIVE_DRAFT, GOOD], LIVE_FLAGS)
    problems = reply["verification"]["problems"]
    assert not any("Fix: Recommend the Fastest" in p for p in problems)  # the Verifier's wrong fixes don't go to the Writer
    assert any("recommend the Balanced route" in p for p in problems)  # code's own flag does
    assert len(model.writer_prompts) == 2 and "Fix: recommend the Balanced route" in model.writer_prompts[1]
    assert reply["map"]["recommended_route"] == "balanced"
    assert reply["map"]["headline"].startswith("Take the Balanced route")
    assert "Fastest route" not in recommendation_section(reply["response"])
    assert app.wrong_recommendations(app.Answer.model_validate(reply["answer"]), route_tools.route_options(*TRIP)) == []


def test_the_backstop_replaces_a_wrong_route_the_writer_keeps(monkeypatch):
    """The Writer ignores the fix and still recommends Fastest: the final answer states the code's choice instead."""
    reply, model = run(monkeypatch, [LIVE_DRAFT, LIVE_DRAFT], [])
    summary = [x["text"] for x in reply["answer"]["summary"]]
    assert summary[0].startswith("We recommend the Balanced route")
    assert not any("Fastest route" in s and "recommend" in s for s in summary)
    assert "Taking the Lowest risk route instead" in reply["answer"]["tradeoff"][0]["text"]  # a tradeoff sentence isn't a recommendation


def test_headline_cards_and_text_name_the_same_route(monkeypatch):
    reply, _ = run(monkeypatch, [LIVE_DRAFT, GOOD], LIVE_FLAGS)
    m = reply["map"]
    name = app.LABELS[m["recommended_route"]]
    assert m["headline"].startswith(f"Take the {name} route")
    said = {k for x in reply["answer"]["summary"] for k in app.recommended_names(x["text"], True)}
    assert said <= {m["recommended_route"]}
    assert "balanced" in m["routes_about_equally_safe"]  # the cards' badges read this list, the map label reads recommended_route


def test_recommended_route_is_named(monkeypatch):
    """'the recommended route' gets its label, so the Verifier checks its numbers against the right route."""
    options = route_tools.route_options(*TRIP)
    named = app.name_the_recommended(app.Answer.model_validate(LIVE_DRAFT), options)
    assert named.notes[0].text.startswith("The recommended Balanced route has 1.73 miles")


@pytest.mark.parametrize("sentence,keys", [
    ("We recommend the Fastest route because it is faster.", {"fastest"}),
    ("The Lowest-risk route is recommended for this trip.", {"lowest_risk"}),
    ("Take the Balanced route.", {"balanced"}),
    ("The recommended route (Fastest) uses Canal St.", {"fastest"}),
    ("Taking the Lowest risk route instead adds 9.6 minutes per day.", set()),
    ("The Balanced and Lowest risk routes are about equally safe.", set()),
])
def test_recommended_names(sentence, keys):
    assert app.recommended_names(sentence, True) == keys


def test_one_rounding_rule_for_daily_minutes(options):
    """2 x the one-way figure rounded to one decimal: 4.8 one way is 9.6 a day on the card, the table, and in the text."""
    assert app.daily_minutes(4.8) == 9.6 and app.daily_minutes(0.74) == 1.4 and app.daily_minutes(0.75) == 1.6
    table = app.time_table(options)
    for k, r in options["routes"].items():
        if k != "fastest":
            assert f"{app.daily_minutes(r['extra_minutes_vs_fastest']):g} a day, round trip" in table
