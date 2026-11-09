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
    return {**result, "routes": {k: {f: v for f, v in r.items() if f != "line"} for k, r in result["routes"].items()}}


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
        ctx.context.hooks.emit({"type": "routes", "data": map_routes(result)})
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
- Call a stretch dangerous only when the tool marks it above_average. Otherwise
  describe it as within the usual range for similar streets.
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
   that ref and report its finding word for word, with the counts, rates, and projects (name and year
   only). Never say a lane worked or failed.
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
    run.hooks.emit({"type": "researcher", "agent": agent.name, "status": "running", "task": task, "round": run.rounds})
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
    run.hooks.emit({"type": "researcher", "agent": agent.name, "status": status, "summary": text[:400]})
    return text


# --- The Lead: checks the question, gets routes, fans out, and fills gaps ---

lead = Agent(
    name="Lead",
    instructions=f"""\
You lead a team that compares bike commute routes in New York City for safety.

1. Check the question. If it doesn't name both a start and an end place, reply with exactly one line:
   CLARIFY: <one short question asking for the start, the end, and the time of day they ride>
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

# --- The Writer: one answer from the routes and the findings ---

WRITER_RULES = """\
Write the answer in markdown for a New York cyclist choosing a commute. Use only the numbers in the
route options and findings you're given.

Structure:
1. **Recommendation** (2 to 4 sentences). Name routes by their main streets. Follow safety_label exactly:
   - safest: call that route "the safest of these three routes" (always that phrase, never "the
     safest route" alone, since it only beats the other two), and say how much time it adds.
   - about_equally_safe: say the routes in routes_about_equally_safe are about equally safe and
     recommend recommended_route, the faster of them. Don't call any route safest.
   Routes with the same route_ref are one route: say so instead of describing it twice.
   - depends_on_assumption: say which route has lower risk depends on an assumption about where people
     ride that the data can't settle. Lay out the tradeoff and don't call any route safest.
2. **Time versus safety**: a small markdown table, one row per distinct route: minutes, extra minutes
   per day for a round trip (extra minutes × 2), and risk relative to the fastest at the half, assumed,
   and double rider-share settings (e.g. "38% lower"). One sentence after it on the tradeoff in daily
   terms. Never a rate.
3. **Hotspots**: up to 3 segments, each with its crash count and range since 2021, main cause, edge ID,
   and two collision IDs. Call one dangerous only if its findings say it is above average; otherwise
   say it is within the usual range for similar streets.
4. **Bike lanes and redesigns**: each before/after finding word for word, then its counts in plain words,
   e.g. "41 crashes before (range 28 to 54) and 29 after (range 18 to 40)", and its projects by name and
   year.
5. **Limits**: always include these points:
   - Police-reported crashes only, so this is serious crash risk, not every incident.
   - Citi Bike trips set the rider estimate and don't cover all riders; say if an answer is lower
     confidence because a route has miles without Citi Bike coverage.
   - These are not personal odds, and a before/after change doesn't prove a lane caused it.
   - If the data is too sparse for segment-level conclusions, say so and give the route-level
     comparison only.

Citations: end each sentence, bullet, or table row that has numbers with their source in brackets: the
route_ref, corridor_ref, or edge ID, plus collision IDs for crashes, e.g. [route:ab12cd34] or
[edge 4273-9903-0; collisions 1f0e..., 9a3b...]. Don't repeat a citation after each number in the same
sentence. Every number must be covered by a citation. Never give per-rider rates or a rider's odds.
Never say a lane worked or failed. Never say painted lanes are more dangerous than no lane: compare lane
types only within similar streets. If there is no route and the note mentions the Staten Island Ferry,
explain that option. Write plain, short sentences."""

# Default reasoning spent 16,000 thinking tokens and up to 150 s on one answer; low writes the same answer in about 20 s.
WRITER_SETTINGS = SETTINGS.resolve(ModelSettings(extra_args={**SETTINGS.extra_args, "reasoning_effort": "low"}))
writer = Agent(name="Writer", instructions=WRITER_RULES, model=MODEL, model_settings=WRITER_SETTINGS)


# --- The Verifier: re-runs the tools for every number the Writer cites ---


class Check(BaseModel):
    ok: bool
    problems: list[str]


verifier = Agent(
    name="Verifier",
    instructions="""\
You check a draft answer about bike routes against fresh tool results. The code has already re-run every
tool call behind the refs and edge IDs the draft cites; their results follow the draft. The route options
cover minutes and relative risks. Compare every number in the draft with them: crash counts, ranges,
minutes, relative risks, project counts and years, before/after results.

Also flag: a number with no citation, a number that no result supports, a stretch called dangerous that
the results don't mark above average, a lane said to have worked or failed, a per-rider rate or personal
odds, painted lanes called more dangerous than no lane, a route called safest when safety_label isn't
'safest', a route called safest without the words "of these three routes", a before/after finding not
quoted as written, or a missing limits section.

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
        return {"response": lead_said.removeprefix("CLARIFY:").strip(), "clarify": True, "map": None}
    if run.routes is None and not run.findings:
        # No route: the Lead's own reply, or the tool's note, is the answer
        notes = [c["result"] for c in hooks.tool_calls if c["name"] == "route_options"]
        prompt = f"Question: {question}\n\nRoute search result:\n{notes[-1] if notes else lead_said}"
        report = await Runner.run(writer, prompt, hooks=hooks)
        return {"response": str(report.final_output), "clarify": False, "map": None}

    # 2. Write: one call. The Writer sees only the question, the routes, and the findings.
    findings = "\n\n---\n\n".join(f"{f['agent']} (task: {f['task']}; refs: {', '.join(f['refs'])})\n{f['text']}" for f in run.findings)
    material = f"Question: {question}\n\nRoute options:\n{route_brief(run.routes)}\n\nFindings:\n\n{findings or 'None came back.'}"
    draft = str((await Runner.run(writer, material, hooks=hooks)).final_output)

    # 3. Verify, then fix once
    check = None
    try:
        hooks.activity("Verifier", "started")
        fresh = recheck(draft, hooks)
        prompt = f"Route options:\n{route_brief(run.routes)}\n\nDraft:\n{draft}\n\nFresh tool results:\n\n{fresh}"
        verified = await asyncio.wait_for(Runner.run(verifier, prompt, hooks=hooks, max_turns=2), VERIFY_SECONDS)
        check = verified.final_output
    except Exception as e:  # out of time or a bad reply: say the check didn't finish, don't block the answer
        hooks.activity("Verifier", f"could not finish: {type(e).__name__}")
    late = time.monotonic() - started > FIX_DEADLINE_SECONDS
    if check is not None and not check.ok and check.problems and late:
        # No time for a rewrite: show the flags rather than hide them
        draft += "\n\n**The number check flagged these points:**\n" + "\n".join(f"- {p}" for p in check.problems)
    elif check is not None and not check.ok and check.problems:
        problems = "\n".join(f"- {p}" for p in check.problems)
        fix = f"{material}\n\nYour draft:\n{draft}\n\nA verifier re-ran the tools and found these problems. Fix them and return the full corrected answer:\n{problems}"
        draft = str((await Runner.run(writer, fix, hooks=hooks)).final_output)
    if check is None:
        draft += "\n\n_The automatic number check did not finish in time for this answer._"
    hooks.activity("Pipeline", f"finished in {time.monotonic() - started:.0f} s")
    verification = check.model_dump() if check is not None else None
    return {"response": draft, "clarify": False, "map": map_data(run), "verification": verification}


# --- Map data for the page ---


def map_routes(routes: dict) -> dict:
    return {"start": routes["start"], "end": routes["end"], "safety_label": routes["safety_label"],
            "recommended_route": routes["recommended_route"],
            "routes": {k: {"line": r["line"], "minutes": r["minutes"], "miles": r["miles"],
                           "relative_risk_vs_fastest": r["relative_risk_vs_fastest"], "route_ref": r["route_ref"]}
                       for k, r in routes["routes"].items()}}


def map_data(run: Research) -> dict | None:
    if run.routes is None:
        return None
    hotspots, before_after_rows = {}, []
    for call in run.hooks.tool_calls:
        try:
            result = json.loads(call["result"])
        except ValueError:
            continue
        for spot in result.get("hotspots", []):
            if spot["edge_id"] not in hotspots:
                hotspots[spot["edge_id"]] = {**{k: spot[k] for k in ("edge_id", "street", "crashes", "main_cause", "above_average")},
                                             **tools.edge_point(spot["edge_id"])}
        for row in result.get("results", []):
            if "rate_before" in row:
                before_after_rows.append({k: row.get(k) for k in ("treatment", "rate_before", "rate_after", "comparison_rate_before",
                                                                   "comparison_rate_after", "finding", "segments", "miles")})
    unique = {json.dumps(r, sort_keys=True): r for r in before_after_rows}
    return {**map_routes(run.routes), "hotspots": list(hotspots.values()), "before_after": list(unique.values())}


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
    return {k: result[k] for k in ("line", "minutes", "miles", "relative_risk_vs_fastest", "error", "next_step") if k in result}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
