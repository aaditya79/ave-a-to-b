"""Hand-check list for the README's sample queries.

Reads the live answers in docs/traces.json, rebuilds each route from the same start and end, and writes:
  docs/sample_routes.csv     every segment of every sample route, in order
  docs/expected_answers.md   each expected number, the file it comes from, and a DuckDB query that
                             recomputes it from the Parquet files without any project code
Then it runs every query and checks the result against the agent's answer.

Run from the repo root: .venv/bin/python build/verify_samples.py
"""

import json
import math
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tools  # noqa: E402

DOCS = ROOT / "docs"
NOT_A_CAUSE = "('Unspecified', 'Does Not Apply')"
# Exact Poisson 95% bounds for counts 0 to 9 (tools.range_bounds), as DuckDB lists: DuckDB has no chi-square quantile
EXACT_LOW = "[" + ", ".join(f"{tools.range_bounds(k)[0]:.1f}" for k in range(10)) + "]"
EXACT_HIGH = "[" + ", ".join(f"{tools.range_bounds(k)[1]:.1f}" for k in range(10)) + "]"


def range_sql(count: str, name: str) -> str:
    """tools.range_bounds as DuckDB: the exact range below 10 crashes, n ± 2 × √n (floored at 0) from 10 up."""
    return (f"CASE WHEN {count} < 10 THEN {EXACT_LOW}[{count} + 1] ELSE round(greatest(0, {count} - 2 * sqrt({count})), 1) END AS {name}_low,\n"
            f"       CASE WHEN {count} < 10 THEN {EXACT_HIGH}[{count} + 1] ELSE round({count} + 2 * sqrt({count}), 1) END AS {name}_high")


def ranges_in(value, path="map"):
    """Every {n, low, high} in an answer's map, with where it is."""
    if isinstance(value, dict):
        if set(value) >= {"n", "low", "high"}:
            yield path, value
        for k, v in value.items():
            yield from ranges_in(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from ranges_in(v, f"{path}[{i}]")


def routed(runs: list[dict]) -> list[dict]:
    """The runs that answered with routes; a clarifying question has none."""
    return [run for run in runs if run.get("map")]


def route_rows(runs: list[dict]) -> pd.DataFrame:
    rows = []
    for number, run in enumerate(runs, start=1):
        call = [c for c in run["tool_calls"] if c["name"] == "route_options" and '"routes"' in c["result"]][-1]
        live = json.loads(call["result"])
        local = tools.route_options(**call["args"])
        for name, route in local["routes"].items():
            assert route["route_ref"] == live["routes"][name]["route_ref"], "local route differs from the live one"
            for position, edge_id in enumerate(tools.resolve(route["route_ref"])):
                rows.append({"query": number, "route": name, "route_ref": route["route_ref"], "position": position, "edge_id": edge_id})
    return pd.DataFrame(rows)


def checks(number: int, run: dict) -> list[dict]:
    """(what, expected value, source files, SQL) for one query."""
    out = []
    routes = run["map"]["routes"]
    sql = f"""SELECT r.route,
       round(sum(e.travel_min), 1) AS minutes,
       round(sum(e.route_risk_half) / max(f.half), 2) AS risk_vs_fastest_half,
       round(sum(e.route_risk_assumed) / max(f.assumed), 2) AS risk_vs_fastest_assumed,
       round(sum(e.route_risk_double) / max(f.double), 2) AS risk_vs_fastest_double
FROM 'docs/sample_routes.csv' r
JOIN 'data/edges.parquet' e USING (edge_id)
CROSS JOIN (SELECT sum(route_risk_half) AS half, sum(route_risk_assumed) AS assumed, sum(route_risk_double) AS double
            FROM 'docs/sample_routes.csv' JOIN 'data/edges.parquet' USING (edge_id)
            WHERE query = {number} AND route = 'fastest') f
WHERE r.query = {number}
GROUP BY r.route ORDER BY minutes;"""
    expected = {k: (r["minutes"], r["relative_risk_vs_fastest"]["half"], r["relative_risk_vs_fastest"]["assumed"],
                    r["relative_risk_vs_fastest"]["double"]) for k, r in routes.items()}
    out.append({"what": "Minutes and risk compared with the Fastest route, at the half, assumed, and double rider-share settings",
                "expected": "; ".join(f"{k}: {v[0]} min, {v[1]} / {v[2]} / {v[3]}" for k, v in expected.items()),
                "files": "docs/sample_routes.csv, data/edges.parquet (travel_min, route_risk_half, route_risk_assumed, route_risk_double)",
                "sql": sql,
                "check": lambda df: all(abs(df.set_index("route").loc[k, "minutes"] - v[0]) < 0.11 and
                                        abs(df.set_index("route").loc[k, "risk_vs_fastest_assumed"] - v[2]) < 0.011 for k, v in expected.items())})

    label = run["map"]["safety_label"]
    sql = f"""SELECT route,
       round(sum(e.route_risk_assumed), 2) AS risk,
       round(sum(e.crash_blended_rate * e.mile_years), 1) AS n,
       round(sum(e.route_risk_assumed) * greatest(0, n - 2 * sqrt(n)) / n, 2) AS risk_low,
       round(sum(e.route_risk_assumed) * (n + 2 * sqrt(n)) / n, 2) AS risk_high
FROM 'docs/sample_routes.csv' JOIN 'data/edges.parquet' e USING (edge_id)
WHERE query = {number}
GROUP BY route ORDER BY risk;"""
    out.append({"what": f"Safety label '{label}' and recommended route '{run['map']['recommended_route']}'. "
                        "Rule: 'safest' needs the lowest-risk route's range (risk × range(n) / n, with n the expected crash count) to sit below every other "
                        "route's range at all three settings; overlapping ranges mean 'about equally safe', and the faster of "
                        "the tied routes is recommended. The query shows the assumed setting; swap in route_risk_half and "
                        "route_risk_double for the other two.",
                "expected": label, "files": "docs/sample_routes.csv, data/edges.parquet (route_risk_*, crash_blended_rate, mile_years)",
                "sql": sql, "check": None})

    if run["map"]["hotspots"]:
        h = run["map"]["hotspots"][0]
        sql = f"""SELECT count(*) AS crashes_since_2021,
       {range_sql("count(*)", "range")},
       (SELECT round(crash_class_rate * mile_years, 1) FROM 'data/edges.parquet' WHERE edge_id = '{h['edge_id']}') AS similar_streets_expected,
       (SELECT contributing_factor_vehicle_1 FROM 'data/crash_locations.parquet' l2 JOIN 'data/crashes.parquet' c2 USING (collision_id)
        WHERE l2.edge_id = '{h['edge_id']}' AND contributing_factor_vehicle_1 NOT IN {NOT_A_CAUSE}
        GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1) AS main_cause
FROM 'data/crash_locations.parquet' l JOIN 'data/crashes.parquet' c USING (collision_id)
WHERE l.edge_id = '{h['edge_id']}';"""
        out.append({"what": f"Stretch 1 that stands out: {h['street']} (segment {h['edge_id']}). It stands out because range_low "
                            "is above similar_streets_expected.",
                    "expected": f"{h['crashes']['n']} crashes (range {h['crashes']['low']} to {h['crashes']['high']}), about "
                                f"{h['similar_streets_expected']} expected on similar streets, main cause {h['main_cause']}",
                    "files": "data/crash_locations.parquet (edge_id), data/crashes.parquet (contributing_factor_vehicle_1), data/edges.parquet (crash_class_rate, mile_years)",
                    "sql": sql, "check": lambda df, h=h: int(df.crashes_since_2021[0]) == h["crashes"]["n"] and
                                                       abs(df.range_low[0] - h["crashes"]["low"]) < 0.051 and
                                                       abs(df.range_high[0] - h["crashes"]["high"]) < 0.051 and
                                                       abs(df.similar_streets_expected[0] - h["similar_streets_expected"]) < 0.051})
        first = h["collision_ids"][0]
        out.append({"what": f"One police report behind stretch 1, in the city's own copy of the crash table. Its street names "
                            f"should match {h['street']}.",
                    "expected": f"collision_id {first}",
                    "files": "NYC Open Data, Motor Vehicle Collisions - Crashes (h9gi-nx95)",
                    "sql": f"-- open in a browser:\n-- https://data.cityofnewyork.us/resource/h9gi-nx95.json?collision_id={first}",
                    "check": None})

    studied = [r for r in run["map"]["before_after"] if not r["too_sparse"]]
    if studied:
        r = studied[0]
        treatment = "SIP" if r["kind"] == "Street Improvement Project" else "lane: " + r["kind"].removeprefix("new ").removesuffix(" lane")
        if treatment == "SIP":
            match = f"b.project LIKE '%: {r['project']}'"
        else:
            match = f"b.project LIKE 'bike route%' AND e.name = '{r['project'].removeprefix('Lane on ')}'"
        sql = f"""WITH b AS (SELECT b.*, e.name FROM 'data/before_after.parquet' b JOIN 'data/edges.parquet' e USING (edge_id)
           WHERE b.treatment = '{treatment}' AND {match}
             AND b.edge_id IN (SELECT edge_id FROM 'docs/sample_routes.csv' WHERE query = {number})),
     totals AS (SELECT sum(crashes_pre)::INT AS crashes_before, sum(crashes_post)::INT AS crashes_after FROM b)
SELECT crashes_before, {range_sql("crashes_before", "before")},
       crashes_after, {range_sql("crashes_after", "after")},
       (SELECT round(sum(crashes_pre) / (sum(length_mi * pre_years) / sum(length_mi)), 1) FROM b) AS per_year_before,
       (SELECT round(sum(crashes_post) / (sum(length_mi * post_years) / sum(length_mi)), 1) FROM b) AS per_year_after,
       (SELECT round(sum(control_pre_rate * length_mi * pre_years) / sum(length_mi * pre_years) * sum(length_mi), 1) FROM b) AS similar_streets_before,
       (SELECT round(sum(control_post_rate * length_mi * post_years) / sum(length_mi * post_years) * sum(length_mi), 1) FROM b) AS similar_streets_after
FROM totals;"""
        y, cb, ca = r["crashes_per_year"], r["crashes_before"], r["crashes_after"]
        out.append({"what": f"Before and after: {r['project']} ({r['year']}, {r['kind']}). Crashes on the changed street, with their "
                            "ranges, and crashes per year there and on similar unchanged streets scaled to the same length.",
                    "expected": f"{cb['n']} crashes before (range {cb['low']} to {cb['high']}) and {ca['n']} after (range {ca['low']} to "
                                f"{ca['high']}); {y['before']} → {y['after']} a year; similar streets {y['comparison_before']} → {y['comparison_after']}",
                    "files": "data/before_after.parquet (crashes_pre, crashes_post, pre_years, post_years, control_pre_rate, control_post_rate), data/edges.parquet (name)",
                    "sql": sql, "check": lambda df, y=y, cb=cb, ca=ca: abs(df.per_year_before[0] - y["before"]) < 0.051 and
                                                                     abs(df.per_year_after[0] - y["after"]) < 0.051 and
                                                                     (int(df.crashes_before[0]), int(df.crashes_after[0])) == (cb["n"], ca["n"]) and
                                                                     all(abs(df[f"{w}_{e}"][0] - c[e]) < 0.051 for w, c in (("before", cb), ("after", ca)) for e in ("low", "high"))})

    # Every range anywhere in the answer's map (route table, stretches, before and after) against tools.count_range
    stale = [f"{where}: {c['n']} as {c['low']} to {c['high']}" for where, c in ranges_in(run["map"])
             if (c["low"], c["high"]) != tuple(tools.count_range(c["n"])[k] for k in ("low", "high"))]
    def all_current(df, stale=stale) -> bool:
        for line in stale:
            print("   stale range:", line)
        return not stale
    out.append({"what": "Every crash range in the answer (route table, stretches, before and after) uses today's range function",
                "expected": f"{sum(1 for _ in ranges_in(run['map']))} ranges, each tools.count_range(n)",
                "files": "tools.py (range_bounds); the same rule as the SQL above",
                "sql": "SELECT 1 AS checked_in_python;", "check": all_current})
    return out


def main():
    runs = routed(json.loads((DOCS / "traces.json").read_text()))
    routes = route_rows(runs)
    routes.to_csv(DOCS / "sample_routes.csv", index=False)
    database = duckdb.connect()
    lines = ["# Expected answers for the sample queries\n",
             "Each number below is what the agent answered on the live site (docs/traces.json). Under it is the file it comes",
             "from and a DuckDB query that recomputes it from the raw Parquet files, without any project code. Run the queries",
             "from the repo root, in the DuckDB command line (`duckdb`) or in Python with `duckdb.sql(query).df()`.",
             "`docs/sample_routes.csv` lists every segment of each route, in order, as routed on the live site.\n",
             "A range is the 95% range of a crash count: the exact Poisson range below 10 crashes (0 gives 0 to 3.7, 7 gives",
             "2.8 to 14.4) and n ± 2 × √n, floored at 0, from 10 up. The ranges code builds were recomputed with that rule by",
             "`build/refresh_ranges.py`; see the note at the top of docs/traces.md.\n"]
    failures = 0
    for number, run in enumerate(runs, start=1):
        lines.append(f"## Query {number}: {run['question']}\n")
        for item in checks(number, run):
            status = ""
            if item["check"] is not None:
                ok = item["check"](database.sql(item["sql"]).df())
                failures += not ok
                status = " Recomputed by the query: matches." if ok else " Recomputed by the query: DOES NOT MATCH."
                print(f"Query {number} | {'match   ' if ok else 'MISMATCH'} | {item['what']}")
            lines += [f"**{item['what']}**\n", f"- Agent's answer: {item['expected']}.{status}", f"- Source: {item['files']}\n",
                      "```sql", item["sql"], "```\n"]
    (DOCS / "expected_answers.md").write_text("\n".join(lines))
    print(f"{len(routes)} route segments written; {failures} mismatches")


if __name__ == "__main__":
    main()
