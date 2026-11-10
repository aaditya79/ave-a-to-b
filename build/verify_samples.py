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
                        "Rule: 'safest' needs the lowest-risk route's range (risk × (n ± 2 × √n) / n) to sit below every other "
                        "route's range at all three settings; overlapping ranges mean 'about equally safe', and the faster of "
                        "the tied routes is recommended. The query shows the assumed setting; swap in route_risk_half and "
                        "route_risk_double for the other two.",
                "expected": label, "files": "docs/sample_routes.csv, data/edges.parquet (route_risk_*, crash_blended_rate, mile_years)",
                "sql": sql, "check": None})

    if run["map"]["hotspots"]:
        h = run["map"]["hotspots"][0]
        sql = f"""SELECT count(*) AS crashes_since_2021,
       round(count(*) - 2 * sqrt(count(*)), 1) AS range_low,
       round(count(*) + 2 * sqrt(count(*)), 1) AS range_high,
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
        sql = f"""SELECT sum(b.crashes_pre) AS crashes_before, sum(b.crashes_post) AS crashes_after,
       round(sum(b.crashes_pre) / (sum(b.length_mi * b.pre_years) / sum(b.length_mi)), 1) AS per_year_before,
       round(sum(b.crashes_post) / (sum(b.length_mi * b.post_years) / sum(b.length_mi)), 1) AS per_year_after,
       round(sum(b.control_pre_rate * b.length_mi * b.pre_years) / sum(b.length_mi * b.pre_years) * sum(b.length_mi), 1) AS similar_streets_before,
       round(sum(b.control_post_rate * b.length_mi * b.post_years) / sum(b.length_mi * b.post_years) * sum(b.length_mi), 1) AS similar_streets_after
FROM 'data/before_after.parquet' b JOIN 'data/edges.parquet' e USING (edge_id)
WHERE b.treatment = '{treatment}' AND {match}
  AND b.edge_id IN (SELECT edge_id FROM 'docs/sample_routes.csv' WHERE query = {number});"""
        y = r["crashes_per_year"]
        out.append({"what": f"Before and after: {r['project']} ({r['year']}, {r['kind']}). Crashes per year on the changed street, "
                            "and on similar unchanged streets scaled to the same length.",
                    "expected": f"{y['before']} → {y['after']} a year; similar streets {y['comparison_before']} → {y['comparison_after']}",
                    "files": "data/before_after.parquet (crashes_pre, crashes_post, pre_years, post_years, control_pre_rate, control_post_rate), data/edges.parquet (name)",
                    "sql": sql, "check": lambda df, y=y: abs(df.per_year_before[0] - y["before"]) < 0.051 and abs(df.per_year_after[0] - y["after"]) < 0.051})
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
             "A range is n ± 2 × √n, floored at 0.\n"]
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
