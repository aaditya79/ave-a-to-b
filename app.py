"""NYC bike commute safety agent: a Lead plans and fans out, Researchers dig in parallel, a Writer
answers, and a Verifier re-runs the tools behind every number before the answer goes back.

    POST /chat   {message, session_id?} -> {response, session_id, tool_calls, map}
    GET  /run    the same pipeline, streamed as Server-Sent Events for the page's live cards
    GET  /route  one route for the page's λ slider
"""

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import uvicorn
from agents import Agent, ModelSettings, RunContextWrapper, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import tools
from context_panel import AgentContexts, PanelHooks, add_context_panel

load_dotenv(Path(__file__).parent / ".env")

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# With GEMINI_API_KEY set, LiteLLM calls the Gemini API with it. Otherwise it calls Vertex AI with
# your gcloud credentials, which is what Cloud Run uses.
if os.getenv("GEMINI_API_KEY"):
    MODEL = LitellmModel(model="gemini/gemini-3.5-flash", api_key=os.environ["GEMINI_API_KEY"])
    RESEARCHER_MODEL = LitellmModel(model="gemini/gemini-3.5-flash-lite", api_key=os.environ["GEMINI_API_KEY"])
    SETTINGS = ModelSettings(extra_args={"timeout": 90, "num_retries": 3})
else:
    MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
    RESEARCHER_MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused")
    SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 3})

MAX_RESEARCHERS = 10  # in all, across rounds
MAX_ROUNDS = 3  # the first fan-out plus two gap rounds
RESEARCH_SECONDS = 120  # then the Writer works with what came back
VERIFY_SECONDS = 60
FIX_DEADLINE_SECONDS = 210  # graders wait 5 minutes: past this, skip the rewrite


@dataclass
class Research:
    """One run's state, shared by every agent and tool in it. The model never sees it."""

    hooks: "TraceHooks"
    researchers: int = 0
    active: int = 0  # researchers running now: a call that starts when none are running opens a round
    rounds: int = 0
    routes: dict | None = None  # route_options' full result, with the map lines
    findings: list[dict] = field(default_factory=list)


# --- Tools the agents call. Each returns JSON text; map-only fields stay out of the model's context. ---


def as_text(result: dict) -> str:
    return json.dumps(result, default=str)


def for_the_model(result: dict) -> dict:
    """route_options returns a line per segment for the map. The model only needs the numbers."""
    if "routes" not in result:
        return result
    return {**result, "routes": {k: {f: v for f, v in r.items() if f not in MAP_ONLY} for k, r in result["routes"].items()}}


MAP_ONLY = {"line", "segment_risk", "turns"}


def edge_ids(where: str) -> str | list[str]:
    """A ref like 'corridor:ab12cd34', or one or more edge IDs separated by commas."""
    where = where.strip()
    if ":" in where:
        return where
    return [e.strip() for e in where.split(",") if e.strip()]


@function_tool
def route_options(ctx: RunContextWrapper[Research], start: str, end: str) -> str:
    """Find three bike routes between two NYC places: fastest, balanced, and lowest risk. Each comes with
    minutes, miles, risk relative to the fastest at three rider-share settings, a route_ref, and corridors
    (each with a corridor_ref) to hand to researchers. Also says whether one route is clearly safest.

    Args:
        start: Where the trip starts: an address, intersection, or place name, with the borough.
        end: Where the trip ends, the same way.
    """
    result = tools.route_options(start, end)
    if "routes" in result:
        ctx.context.routes = result
        ctx.context.hooks.emit({"type": "routes", "data": map_routes(result)})  # draw the map before research ends
    return as_text(for_the_model(result))


def crash_summary(where: str, year_from: int = 2021, year_to: int = 2026, severe_only: bool = False,
                  rider_type: Literal["any", "pedal", "e-bike"] = "any", hour_from: int = 0, hour_to: int = 24) -> str:
    """Police-reported bike crashes on a stretch: counts with ranges, the expected count for similar
    streets, causes, time of day, the top hotspot segments with their main cause, and collision IDs.

    Args:
        where: A route_ref or corridor_ref, or edge IDs separated by commas.
        year_from: First year to count (data starts in 2021).
        year_to: Last year to count.
        severe_only: Count only crashes where a cyclist was killed or seriously hurt.
        rider_type: 'pedal', 'e-bike', or 'any'.
        hour_from: Count crashes from this hour (0-23).
        hour_to: Count crashes before this hour (1-24).
    """
    filters = {"year_from": year_from, "year_to": year_to, "severe_only": severe_only, "hour_from": hour_from, "hour_to": hour_to}
    if rider_type != "any":
        filters["rider_type"] = rider_type
    return as_text(tools.crash_summary(edge_ids(where), filters))


def infrastructure_summary(where: str) -> str:
    """Bike lanes by type, lanes installed since 2021, Street Improvement Projects (name and year),
    truck route miles, and blocked-bike-lane complaints on a stretch.

    Args:
        where: A route_ref or corridor_ref, or edge IDs separated by commas.
    """
    return as_text(tools.infrastructure_summary(edge_ids(where)))


def exposure_summary(where: str) -> str:
    """Estimated riders per day on a stretch, where the estimate comes from, and how far to trust it.

    Args:
        where: A route_ref or corridor_ref, or edge IDs separated by commas.
    """
    return as_text(tools.exposure_summary(edge_ids(where)))


def similar_streets(where: str) -> str:
    """Crashes on a stretch compared with the expected count for similar streets (same lane type, road
    class, and borough), class by class, with ranges.

    Args:
        where: A route_ref or corridor_ref, or edge IDs separated by commas.
    """
    return as_text(tools.similar_streets(edge_ids(where)))


def before_after(where: str) -> str:
    """For segments that got a bike lane or Street Improvement Project since 2021: crashes before and
    after, against comparison streets that didn't change. The finding is worded for you: report it as is.

    Args:
        where: A route_ref or corridor_ref, or edge IDs separated by commas.
    """
    return as_text(tools.before_after(edge_ids(where)))


def run_sql(query: str) -> str:
    """Run one read-only DuckDB SELECT over the raw tables: crashes, crash_people, crash_locations,
    edge_lanes, edge_sips, before_after, citibike_h3, citibike_stations, counter_daily,
    counter_sensors, blocked_edges. At most 100 rows. Use it only when no other tool answers.

    Args:
        query: One SELECT statement.
    """
    return as_text(tools.run_sql(query))


EVIDENCE = {f.__name__: f for f in [crash_summary, infrastructure_summary, exposure_summary, similar_streets, before_after]}
TOOLS = {name: function_tool(f) for name, f in [*EVIDENCE.items(), ("run_sql", run_sql)]}

# --- Researchers: three kinds, each with only the tools its job needs ---

RESEARCHER_RULES = """
Rules:
- Use only numbers your tools return. End each bullet with the source of its numbers in brackets: the
  ref or edge ID you passed, and collision IDs for a hotspot, e.g. [corridor:ab12cd34] or
  [edge 42731051-9903143738-0; collisions 1f0e..., 9a3b...].
- Give counts with their range, e.g. "38 crashes (range 26 to 50)".
- Single out a stretch only when the tool marks it above_average, and say it "stands out compared to
  similar streets". Don't list stretches that don't. Never use the word "dangerous".
- Compare lane types only within similar streets. Never say painted lanes are more dangerous than no lane:
  painted lanes sit mostly on busy arterials.
- If a tool says too_sparse_for_segment_conclusions, say the stretch is too sparse for segment-level
  conclusions.
- Never turn anything into a rider's personal odds or a per-rider rate.
- Reply with at most 10 short bullets. If a tool fails or part of the task can't be answered, say so."""

RESEARCHERS = {
    "crash": Agent(
        name="Crash Researcher",
        instructions="""\
You study police-reported bike crashes on the routes or corridors you're given.
1. Call crash_summary once per ref, all in one turn. Pass the hours if the task names a time window.
2. Report all_crashes with its range and whether it is above_average, then the count matching the
   time window. For each hotspot: its edge ID, crash count and range, main cause, whether it is
   above_average, and two collision IDs. Call similar_streets if the task asks how a stretch compares.
""" + RESEARCHER_RULES,
        model=RESEARCHER_MODEL, model_settings=SETTINGS, tools=[TOOLS["crash_summary"], TOOLS["similar_streets"], TOOLS["run_sql"]]),
    "infrastructure": Agent(
        name="Infrastructure Researcher",
        instructions="""\
You study bike lanes and street redesigns on the routes or corridors you're given.
1. Call infrastructure_summary once per ref, all in one turn.
2. If any lane was installed or any Street Improvement Project happened since 2021, call before_after on
   that ref. It returns one result per project: report each project's name, year, and finding word for
   word. Never say a lane worked or failed.
""" + RESEARCHER_RULES,
        model=RESEARCHER_MODEL, model_settings=SETTINGS, tools=[TOOLS["infrastructure_summary"], TOOLS["before_after"], TOOLS["run_sql"]]),
    "exposure": Agent(
        name="Exposure Researcher",
        instructions="""\
You estimate how many people bike the routes or corridors you're given, and how far to trust that.
1. Call exposure_summary once per ref, all in one turn.
2. Report riders per day (as a rough estimate), the share of miles with Citi Bike coverage, and whether
   the estimate is lower confidence. Call similar_streets if the task asks about busy versus risky.
""" + RESEARCHER_RULES,
        model=RESEARCHER_MODEL, model_settings=SETTINGS, tools=[TOOLS["exposure_summary"], TOOLS["similar_streets"]]),
}


@function_tool
async def research(ctx: RunContextWrapper[Research], researcher: Literal["crash", "infrastructure", "exposure"],
                   task: str, refs: list[str]) -> str:
    """Send one researcher, in a fresh context, to study some routes or corridors. Call several at once
    to run them in parallel. Returns its findings with citations.

    Args:
        researcher: 'crash' for crash counts, hotspots and causes; 'infrastructure' for lanes, redesigns
            and before/after; 'exposure' for how many people ride there and how sure that is.
        task: What to find out, in one or two sentences. Include the time of day if the user gave one.
        refs: route_ref or corridor_ref values from route_options, or edge IDs.
    """
    run = ctx.context
    if run.active == 0:
        run.rounds += 1  # nobody is running: this call opens a new round
    if run.rounds > MAX_ROUNDS:
        return "No research rounds left. Reply DONE."
    if run.researchers >= MAX_RESEARCHERS:
        return "No researchers left. Reply DONE."
    run.researchers += 1
    run.active += 1
    agent = RESEARCHERS[researcher].clone(name=f"{RESEARCHERS[researcher].name} {run.researchers}")
    run.hooks.emit({"type": "researcher", "agent": agent.name, "kind": researcher, "status": "running", "task": task,
                    "round": run.rounds})
    try:
        prompt = f"Task: {task}\nRefs: {', '.join(refs)}"
        result = await Runner.run(agent, prompt, context=run, hooks=run.hooks, max_turns=8)
        text = str(result.final_output)
        status = "done"
    except Exception as e:
        text = f"This researcher failed ({type(e).__name__}). Treat its task as unanswered."
        status = "failed"
    finally:
        run.active -= 1
    run.findings.append({"agent": agent.name, "task": task, "refs": refs, "text": text})
    run.hooks.emit({"type": "researcher", "agent": agent.name, "kind": researcher, "status": status, "text": text})
    return text


# --- The Lead: checks the question, gets routes, fans out, and fills gaps ---

lead = Agent(
    name="Lead",
    instructions=f"""\
You lead a team that compares bike commute routes in New York City for safety.

1. Check the question. If it doesn't name both a start and an end place, reply with exactly two lines:
   CLARIFY: <one short question asking for the start, the end, and the time of day they ride>
   OPTIONS: <two or three example replies a rider could tap, separated by " | ", each a full trip with
   a time, built from places in their question, e.g. "From Park Slope to DUMBO, around 8am">
   Call no tools in that case. Example: "Is biking in Brooklyn safe?" needs a clarifying question.
2. Call route_options with the start and end. If it returns no_route or an error you can't fix, reply DONE.
3. In ONE turn, call research several times so the researchers work in parallel:
   - a 'crash' researcher for each route that differs from the fastest, and one for the fastest
     (pass that route's route_ref, plus its corridor_refs);
   - one 'infrastructure' researcher with all the route_refs;
   - one 'exposure' researcher with all the route_refs.
   Routes with the same route_ref are the same route: research it once. If the user gave a time of day,
   put a three-hour window around it in each crash task, e.g. "around 8am" -> hours 7 to 10.
4. Read the findings and look for gaps: a corridor too sparse to judge, a lane or project installed
   since 2021 without a before/after result, or two findings that disagree. For each real gap, send one
   targeted researcher with the specific corridor_ref or edge IDs. At most {MAX_ROUNDS - 1} gap rounds.
5. When the findings cover the routes, reply DONE and nothing else. A writer writes the answer.""",
    model=MODEL, model_settings=SETTINGS, tools=[route_options, research],
)

# --- The Writer: the recommendation, as sentences that each carry their sources ---


class Sentence(BaseModel):
    text: str  # plain English, no IDs or brackets
    sources: list[str]  # "route:ab12cd34", "corridor:ab12cd34", "edge 4273-9903-0", "collision 1f0e..."


class Answer(BaseModel):
    summary: list[Sentence]
    tradeoff: list[Sentence]
    notes: list[Sentence]


WRITER_RULES = """\
You write the recommendation for a New York cyclist choosing between bike routes. Use only the numbers in
the route options, standout stretches, before/after results, and findings you're given. Other parts of
the page already show the route table, the standout stretches, each project's before/after result, and
the limits, so you write only these parts:

Keep every sentence under 25 words.

summary: 2 or 3 short sentences. Name the route to take by its label and its two streets exactly as in
  route names (never list more streets), e.g. "Take the Balanced route via Columbus Avenue and 7th Avenue. It adds 3.5
  minutes each way and has about 62% lower estimated crash risk than the Fastest route." Follow
  safety_label:
  - safest: call that route "the safest of these three routes", never just "the safest route".
  - about_equally_safe: say the routes in routes_about_equally_safe are about equally safe, and
    recommend recommended_route because it is faster. Don't call any route safest.
  - depends_on_assumption: say which route has lower risk depends on assumptions about where people
    ride that the data can't settle. Give the tradeoff. Don't recommend a route or call one safest.
  Use the risk at the assumed setting. Routes with the same route_ref are one route.
tradeoff: 1 or 2 sentences on time versus safety in daily terms: extra minutes per day for a round
  trip (extra minutes × 2) against the change in risk. Say the risk is lower at the half and double
  settings too only if it is.
notes: up to 4 short sentences with other useful findings for this rider, such as the main crash causes
  at their time of day, the miles on protected lanes, blocked-bike-lane complaints, or lower confidence
  where Citi Bike data doesn't cover a route. Skip what the other parts already show. Can be empty.
If there is no route, put the explanation in summary (mention the Staten Island Ferry if the note does)
and leave tradeoff and notes empty.

Every sentence has text and sources. text is plain English with no IDs, refs, or brackets. sources lists
where its numbers come from, e.g. "route:ab12cd34", "corridor:ab12cd34", "edge 4273-9903-0", or
"collision 1f0e...". A sentence with a number needs at least one source.
Never: per-rider rates or personal odds; the word "dangerous"; a lane that "worked" or "failed"; painted
lanes called more dangerous than no lane (compare lane types only within similar streets)."""

# Default reasoning spent 16,000 thinking tokens and up to 150 s on one answer; low writes the same answer in about 20 s.
WRITER_SETTINGS = SETTINGS.resolve(ModelSettings(extra_args={**SETTINGS.extra_args, "reasoning_effort": "low"}))
writer = Agent(name="Writer", instructions=WRITER_RULES, model=MODEL, model_settings=WRITER_SETTINGS, output_type=Answer)


# --- The Verifier: re-runs the tools for every number the Writer cites ---


class Check(BaseModel):
    ok: bool
    problems: list[str]


verifier = Agent(
    name="Verifier",
    instructions="""\
You check the Writer's sentences in a bike route answer against fresh tool results. Each sentence ends
with its sources in brackets. The code has already re-run every tool call behind the refs and edge IDs
cited; their results follow the draft. The route options cover minutes and relative risks. Compare every
number in the sentences with them: crash counts, ranges, minutes, relative risks, project counts and
years. The route table, standout stretches, before/after results, and limits are built by code from the
same tools, so don't check those.

Also flag: a number with no source, a number that no result supports, the word "dangerous", a lane said
to have worked or failed, a per-rider rate or personal odds, painted lanes called more dangerous than no
lane, a route called safest when safety_label isn't 'safest', a route called safest without the words
"of these three routes", or a recommendation that doesn't follow safety_label.

Return ok=true with no problems if everything matches. Otherwise list each problem in one line: what the
draft says, what the results say, and the fix.""",
    model=MODEL, model_settings=SETTINGS, output_type=Check,
)

CITED = re.compile(r"\b(?:route|corridor):[0-9a-f]{8}\b|\b\d+-\d+-\d+\b")


def recheck(draft: str, hooks: "TraceHooks") -> str:
    """Re-run, in code, every evidence call behind a ref or edge ID the draft cites: the Researchers' own
    calls with their own arguments, and a plain crash_summary for a cited segment no one asked about.
    Each re-run goes in the trace under the Verifier."""
    cited = set(CITED.findall(draft))
    earlier = [c for c in hooks.tool_calls if c["name"] in EVIDENCE and c["agent"] != "Verifier"]
    todo = {json.dumps([c["name"], c["args"]], sort_keys=True): (c["name"], c["args"]) for c in earlier
            if str(c["args"].get("where", "")).strip() in cited}
    covered = {args.get("where", "").strip() for _, args in todo.values()}
    for ref in sorted(cited - covered):
        todo[json.dumps(["crash_summary", {"where": ref}])] = ("crash_summary", {"where": ref})
    results = []
    for name, args in todo.values():
        result = EVIDENCE[name](**args)
        hooks.record("Verifier", name, args, result)
        results.append(f"{name}({json.dumps(args)}):\n{result}")
    return "\n\n".join(results) or "The draft cites no refs or edge IDs."


# --- The pipeline ---


class TraceHooks(PanelHooks):
    """PanelHooks, plus every tool call with the agent that made it, for /chat's tool_calls."""

    def __init__(self, contexts: AgentContexts, emit):
        super().__init__(contexts, emit)
        self.tool_calls: list[dict] = []
        self.started = time.monotonic()

    def record(self, agent: str, name: str, args, result) -> None:
        call = {"agent": agent, "name": name, "args": args, "result": str(result),
                "finished_at_seconds": round(time.monotonic() - self.started, 1)}
        self.tool_calls.append(call)
        self.emit({"type": "tool", **call, "result": call["result"][:2000]})

    async def on_tool_end(self, context, agent, tool, result):
        args = getattr(context, "tool_arguments", None)
        try:
            args = json.loads(args) if args else {}
        except ValueError:
            pass
        self.record(agent.name, tool.name, args, result)


def route_brief(routes: dict | None) -> str:
    return json.dumps(for_the_model(routes), default=str) if routes else "No routes were found."


def parse_clarify(text: str) -> dict:
    question, options = text, []
    for line in text.splitlines():
        if line.startswith("CLARIFY:"):
            question = line.removeprefix("CLARIFY:").strip()
        elif line.startswith("OPTIONS:"):
            options = [o.strip() for o in line.removeprefix("OPTIONS:").split("|") if o.strip()][:3]
    return {"response": question, "clarify": True, "options": options, "answer": None, "map": None}


async def answer(question: str, hooks: TraceHooks) -> dict:
    run = Research(hooks=hooks)
    started = time.monotonic()

    # 1. Research: check the question, get routes, fan out, and fill gaps
    try:
        result = await asyncio.wait_for(Runner.run(lead, question, context=run, hooks=hooks, max_turns=10), RESEARCH_SECONDS)
        lead_said = str(result.final_output).strip()
    except asyncio.TimeoutError:
        lead_said = "DONE"  # out of time: write with what came back
    if lead_said.startswith("CLARIFY:"):
        return parse_clarify(lead_said)
    if run.routes is None:
        # No route: the tool's note (or the Lead's reply) is all there is to explain
        notes = [c["result"] for c in hooks.tool_calls if c["name"] == "route_options"]
        prompt = f"Question: {question}\n\nRoute search result:\n{notes[-1] if notes else lead_said}"
        written = (await Runner.run(writer, prompt, hooks=hooks)).final_output
        return {"response": " ".join(x.text for x in written.summary), "clarify": False, "answer": written.model_dump(), "map": None}

    # 2. The exact parts come from code: standout stretches and per-project before/after, across all routes
    facts = page_facts(run, hooks)

    # 3. Write: one call. The Writer sees only the question, the routes, the facts, and the findings.
    findings = "\n\n---\n\n".join(f"{f['agent']} (task: {f['task']}; refs: {', '.join(f['refs'])})\n{f['text']}" for f in run.findings)
    names = {k: f"the {LABELS[k]} route via {r['via']}" for k, r in map_routes(run.routes)["routes"].items()}
    material = (f"Question: {question}\n\nRoute names: {json.dumps(names)}\n\nRoute options:\n{route_brief(run.routes)}\n\n"
                f"Standout stretches (shown to the rider as a numbered list):\n{json.dumps(facts['hotspots_for_writer'])}\n\n"
                f"Before/after results (shown as cards):\n{json.dumps(facts['before_after_for_writer'])}\n\n"
                f"Findings:\n\n{findings or 'None came back.'}")
    written = (await Runner.run(writer, material, hooks=hooks)).final_output

    # 4. Verify, then fix once
    check = None
    try:
        hooks.activity("Verifier", "started")
        draft = render_sentences(written)
        fresh = recheck(draft, hooks)
        prompt = f"Route options:\n{route_brief(run.routes)}\n\nDraft:\n{draft}\n\nFresh tool results:\n\n{fresh}"
        verified = await asyncio.wait_for(Runner.run(verifier, prompt, hooks=hooks, max_turns=2), VERIFY_SECONDS)
        check = verified.final_output
    except Exception as e:  # out of time or a bad reply: say the check didn't finish, don't block the answer
        hooks.activity("Verifier", f"could not finish: {type(e).__name__}")
    late = time.monotonic() - started > FIX_DEADLINE_SECONDS
    unverified = None
    if check is not None and not check.ok and check.problems and late:
        unverified = check.problems  # no time for a rewrite: show the flags rather than hide them
    elif check is not None and not check.ok and check.problems:
        problems = "\n".join(f"- {p}" for p in check.problems)
        fix = (f"{material}\n\nYour answer:\n{written.model_dump_json()}\n\nA verifier re-ran the tools and found "
               f"these problems. Fix them and return the full corrected answer:\n{problems}")
        written = (await Runner.run(writer, fix, hooks=hooks)).final_output
    hooks.activity("Pipeline", f"finished in {time.monotonic() - started:.0f} s")
    map_ = {**map_routes(run.routes), "hotspots": facts["hotspots"], "before_after": facts["before_after"]}
    return {"response": render_markdown(written, map_, unverified, check is None), "clarify": False,
            "answer": written.model_dump(), "map": map_, "unverified": unverified,
            "verification": check.model_dump() if check is not None else None}


# --- The parts of the answer that come from code ---

LABELS = {"fastest": "Fastest", "balanced": "Balanced", "lowest_risk": "Lowest risk"}


def route_edges(routes: dict) -> dict[str, list[str]]:
    return {name: tools.resolve(r["route_ref"]) for name, r in routes["routes"].items()}


def page_facts(run: Research, hooks: TraceHooks) -> dict:
    """Standout stretches and per-project before/after across all routes, each recorded in the trace."""
    by_route = route_edges(run.routes)
    every = list(dict.fromkeys(e for ids in by_route.values() for e in ids))
    standout = tools.standout_segments(every)
    hooks.record("Pipeline", "standout_segments", {"where": "all routes"}, json.dumps(standout))
    changes = tools.before_after(every)
    hooks.record("Pipeline", "before_after", {"where": "all routes"}, json.dumps(changes))
    hotspots = standout.get("hotspots", [])
    for spot in hotspots:
        spot["routes"] = [LABELS[n] for n, ids in by_route.items() if spot["edge_id"] in ids]
    projects = changes.get("results", [])
    treated = tools.treated.assign(name=[tools._project_name(p, e) for p, e in zip(tools.treated.project, tools.treated.edge_id)])
    for result in projects:
        project_edges = set(treated[treated.name == result["project"]].edge_id)
        result["routes"] = [LABELS[n] for n, ids in by_route.items() if project_edges & set(ids)]
        result["route_refs"] = [run.routes["routes"][n]["route_ref"] for n, ids in by_route.items() if project_edges & set(ids)]
    return {"hotspots": hotspots, "before_after": projects,
            "hotspots_for_writer": [{k: h[k] for k in ("number", "street", "crashes", "main_cause", "routes")} for h in hotspots],
            "before_after_for_writer": [{k: r[k] for k in ("project", "year", "kind", "finding", "routes")} for r in projects if not r["too_sparse"]]}


def via(route: dict, others: list[dict]) -> str:
    """Two named streets that set this route apart: the longest ones some other route doesn't use."""
    names = list(dict.fromkeys(c["street"] for c in sorted(route["corridors"], key=lambda c: -c["miles"]) if c["street"] != "unnamed path"))
    other_streets = [{c["street"] for c in o["corridors"]} for o in others if o["route_ref"] != route["route_ref"]]
    # names is longest first; a stable sort by how many other routes lack each street keeps that order within ties
    ranked = sorted(names, key=lambda n: -sum(n not in streets for streets in other_streets))
    return " and ".join(ranked[:2])


def map_routes(routes: dict) -> dict:
    return {"start": routes["start"], "end": routes["end"], "safety_label": routes["safety_label"],
            "recommended_route": routes["recommended_route"], "routes_about_equally_safe": routes["routes_about_equally_safe"],
            "routes": {k: {"label": LABELS[k], "via": via(r, list(routes["routes"].values())), **{f: r[f] for f in (
                "line", "segment_risk", "turns", "minutes", "miles", "extra_minutes_vs_fastest", "relative_risk_vs_fastest",
                "route_ref", "miles_by_lane", "lower_confidence", "crashes_5yr", "corridors")}}
                for k, r in routes["routes"].items()}}


def cite(sources: list[str]) -> str:
    return f" [{'; '.join(sources)}]" if sources else ""


def render_sentences(written: Answer) -> str:
    parts = []
    for section, sentences in (("Recommendation", written.summary), ("Time versus safety", written.tradeoff), ("Other findings", written.notes)):
        if sentences:
            parts.append(f"### {section}\n\n" + " ".join(f"{x.text}{cite(x.sources)}" for x in sentences))
    return "\n\n".join(parts)


def pct(relative: float) -> str:
    change = round((1 - relative) * 100)
    return "same" if change == 0 else f"{abs(change)}% {'lower' if change > 0 else 'higher'}"


def render_markdown(written: Answer, map_: dict, unverified: list[str] | None, unchecked: bool) -> str:
    """The full answer for /chat, with every citation in brackets."""
    seen, rows = set(), []
    for name, r in map_["routes"].items():
        if r["route_ref"] in seen:
            continue
        seen.add(r["route_ref"])
        names = " / ".join(LABELS[n] for n, x in map_["routes"].items() if x["route_ref"] == r["route_ref"])
        risk = r["relative_risk_vs_fastest"]
        compared = "baseline" if name == "fastest" else f"{pct(risk['assumed'])} ({pct(risk['half'])} to {pct(risk['double'])})"
        rows.append(f"| {names} (via {r['via']}) | {r['minutes']} | {round(r['extra_minutes_vs_fastest'] * 2, 1)} | "
                    f"{compared} | [{r['route_ref']}] |")
    table = ("| Route | Minutes | Extra minutes a day, round trip | Risk vs. Fastest (assumed; half to double) | Source |\n"
             "|---|---:|---:|---|---|\n" + "\n".join(rows))
    spots = [f"{h['number']}. {h['street']}: {h['crashes']['n']} crashes since 2021 (range {h['crashes']['low']} to "
             f"{h['crashes']['high']}), against about {h['similar_streets_expected']} on similar streets. Main cause: "
             f"{h['main_cause'] or 'not recorded'}. It stands out compared to similar streets. "
             f"[edge {h['edge_id']}; collisions {', '.join(h['collision_ids'][:3])}]" for h in map_["hotspots"]]
    spots = spots or ["No single stretch stands out from similar streets."]
    changes = [f"- {r['project']} ({r['year']}, {r['kind']}): {r['finding']}{cite(r['route_refs'])}"
               for r in map_["before_after"] if not r["too_sparse"]]
    sparse = sum(r["too_sparse"] for r in map_["before_after"])
    if sparse and changes:
        changes.append(f"- {sparse} more project{'s' if sparse > 1 else ''} had too few crashes to compare before and after.")
    elif sparse:
        changes.append(f"- {'All ' + str(sparse) + ' projects' if sparse > 1 else 'The one project'} on these routes had too few crashes to compare before and after.")
    changes = changes or ["- No lane or project on these routes since 2021 can be compared before and after."]
    parts = [render_sentences(Answer(summary=written.summary, tradeoff=[], notes=[])),
             f"### Time versus safety\n\n{table}\n\n" + " ".join(f"{x.text}{cite(x.sources)}" for x in written.tradeoff),
             "### Stretches that stand out\n\n" + "\n".join(spots),
             "### Bike lanes and redesigns\n\n" + "\n".join(changes)]
    if written.notes:
        parts.append("### Other findings\n\n" + " ".join(f"{x.text}{cite(x.sources)}" for x in written.notes))
    parts.append("### Limits\n\n" + "\n".join(f"- {line}" for line in limits(map_)))
    if unverified:
        parts.append("**The number check flagged these points, and there was no time to fix them:**\n" + "\n".join(f"- {p}" for p in unverified))
    if unchecked:
        parts.append("_The automatic number check did not finish in time for this answer._")
    return "\n\n".join(parts)


def limits(map_: dict) -> list[str]:
    lines = ["Police-reported crashes only, so this measures serious crash risk, not every incident.",
             "Rider counts come from Citi Bike trips and a few city counters, so they don't cover every rider."]
    unsure = [r["label"] for r in map_["routes"].values() if r["lower_confidence"]]
    if unsure:
        lines.append(f"Part of the {', '.join(dict.fromkeys(unsure))} route has no Citi Bike data nearby, so its estimate is lower confidence.")
    lines.append("These are not personal odds. A change before and after a new lane doesn't prove the lane caused it.")
    if all(r["crashes_5yr"]["n"] < tools.SPARSE_CRASHES for r in map_["routes"].values()):
        lines.append("Too few crashes for conclusions about single stretches, so compare the routes as a whole.")
    return lines


# --- FastAPI app ---

HERE = Path(__file__).parent

# session_id -> every agent's context (for the context panel) and the conversation so far
sessions: dict[str, AgentContexts] = {}
conversations: dict[str, list[str]] = {}

app = FastAPI()
add_context_panel(app, sessions)


# --- Rate limit: the app is public and every question costs Vertex AI calls ---

QUESTIONS_PER_HOUR = 30  # per visitor
QUESTIONS_PER_DAY = 300  # across all visitors
MAX_INSTANCES = 2  # matches --max-instances on Cloud Run: counts live in each instance's memory
asked: dict[str, list[float]] = {}  # ip -> times of recent questions
day_count = {"day": "", "n": 0}  # questions this instance has taken today (New York date)
VISITOR_MESSAGE = (f"You've asked {QUESTIONS_PER_HOUR} questions in the last hour, which is this demo's limit per visitor. "
                   "Please try again later.")
DAILY_MESSAGE = ("This demo has answered its limit of questions for today across all visitors. "
                 "Please try again tomorrow.")


def client_ip(request: Request) -> str:
    # Cloud Run puts the visitor's address first in X-Forwarded-For
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def over_limit(request: Request) -> str | None:
    """The message to show if this question is over a limit, else None (and the question is counted).
    The daily cap is split across instances, since they don't share memory: 150 each, 300 in all.
    An instance that restarts starts its counts over."""
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now - 5 * 3600))  # New York date, near enough
    if day_count["day"] != today:
        day_count.update(day=today, n=0)
    if day_count["n"] >= QUESTIONS_PER_DAY // MAX_INSTANCES:
        return DAILY_MESSAGE
    ip = client_ip(request)
    recent = [t for t in asked.get(ip, []) if now - t < 3600]
    asked[ip] = recent
    if len(recent) >= QUESTIONS_PER_HOUR:
        return VISITOR_MESSAGE
    asked[ip].append(now)
    day_count["n"] += 1
    return None


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


def with_history(session_id: str, message: str) -> str:
    """A reply to a clarifying question only makes sense next to the question it answers."""
    earlier = conversations.setdefault(session_id, [])
    earlier.append(message)
    if len(earlier) == 1:
        return message
    return "Earlier messages from the user:\n" + "\n".join(f"- {m}" for m in earlier[:-1]) + f"\n\nTheir latest message: {message}"


@app.get("/")
def index():
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/chat")
async def chat(request: ChatRequest, http: Request):
    if refusal := over_limit(http):
        return JSONResponse({"response": refusal, "session_id": request.session_id, "tool_calls": [], "map": None,
                             "verification": None}, status_code=429)
    session_id = request.session_id or str(uuid.uuid4())
    sessions[session_id] = AgentContexts()
    hooks = TraceHooks(sessions[session_id], lambda event: None)
    try:
        result = await answer(with_history(session_id, request.message), hooks)
    except Exception as e:
        # Auth, quota, a model that won't stop: say so as the reply, like any other answer
        result = {"response": f"Something went wrong on our side ({type(e).__name__}: {str(e)[:300]}). Please try again."}
    return {"response": result["response"], "session_id": session_id, "tool_calls": hooks.tool_calls,
            "answer": result.get("answer"), "clarify": result.get("clarify", False), "options": result.get("options", []),
            "map": result.get("map"), "verification": result.get("verification")}


@app.get("/run")
async def run(message: str, http: Request, session_id: str | None = None):
    """The same pipeline as /chat, streamed as Server-Sent Events for the page."""
    if refusal := over_limit(http):
        async def refused():
            yield f"data: {json.dumps({'type': 'error', 'message': refusal})}\n\n"
        return StreamingResponse(refused(), media_type="text/event-stream")
    session_id = session_id or str(uuid.uuid4())
    sessions[session_id] = AgentContexts()
    queue: asyncio.Queue = asyncio.Queue()
    hooks = TraceHooks(sessions[session_id], queue.put_nowait)

    async def work():
        try:
            result = await answer(with_history(session_id, message), hooks)
            queue.put_nowait({"type": "done", **result})
        except Exception as e:
            queue.put_nowait({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"})

    task = asyncio.create_task(work())

    async def events():
        try:
            yield f"data: {json.dumps({'type': 'start', 'session_id': session_id})}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("done", "error"):
                    break
        finally:
            task.cancel()  # the page closed the stream: stop spending tokens

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/route")
def route(start: str, end: str, lam: float):
    """One route for the λ slider: 0 is the fastest, higher trades minutes for lower risk."""
    result = tools.route_for_lambda(start, end, max(0.0, min(lam, 20.0)))
    return {k: result[k] for k in ("line", "turns", "minutes", "miles", "relative_risk_vs_fastest", "error", "next_step") if k in result}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
