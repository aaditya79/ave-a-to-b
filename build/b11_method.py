"""Writes method.html, the "How we calculate risk" page: one worked example on one street segment,
with every number computed here from data/edges.parquet.

Run from the build/ folder after b08: ../.venv/bin/python b11_method.py
"""
import html
import json
from pathlib import Path

import sys

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tools  # noqa: E402
SEGMENT = "42430394-4347534767-0"  # Central Park South, the top stretch on the Astoria to Times Square example
LANE_WORDS = {"none": "no bike lane", "painted": "a painted bike lane", "protected": "a protected bike lane",
              "shared": "shared-lane markings", "path": "an off-street path", "link": "a short connector"}
ROAD_WORDS = {"arterial": "a main avenue", "collector": "a collector street", "local": "a local street", "path": "a path"}


def numbers() -> dict:
    """Everything the page shows, from the data. tests/test_method.py checks the page against this."""
    edges = pd.read_parquet(ROOT / "data" / "edges.parquet").set_index("edge_id", drop=False)
    r = edges.loc[SEGMENT]
    assert r.crashes >= 10, "the page shows the n ± 2 × √n range, which applies from 10 crashes up"
    similar = edges[edges.risk_class == r.risk_class]
    years = float(r.mile_years / r.length_mi)
    own_rate = float(r.crashes / r.mile_years)
    per_bike_mile = {s: float(r[f"route_risk_{s}"] / r.length_mi) for s in ["half", "assumed", "double"]}
    return {
        "street": r["name"], "borough": r.borough, "lane": LANE_WORDS.get(r.lane_type, r.lane_type),
        "road": ROAD_WORDS.get(r.road_class, r.road_class), "segment": SEGMENT,
        "length_mi": round(float(r.length_mi), 2), "years": round(years, 1), "mile_years": round(float(r.mile_years), 2),
        "crashes": int(r.crashes), "severe": int(r.severe_crashes),
        "low": round(tools.range_bounds(r.crashes)[0], 1), "high": round(tools.range_bounds(r.crashes)[1], 1),
        "own_rate": round(own_rate, 1),
        "similar_segments": int(len(similar)), "similar_miles": round(float(similar.length_mi.sum()), 1),
        "class_rate": round(float(r.crash_class_rate), 1),
        "expected": round(float(r.crash_class_rate * r.mile_years), 1),
        "own_weight_pct": round(float(r.crash_own_weight) * 100),
        "blended_rate": round(float(r.crash_blended_rate), 1),
        "riders": int(round(float(r.exposure_daily), -1)),
        "risk_assumed": round(per_bike_mile["assumed"], 1), "risk_half": round(per_bike_mile["half"], 1),
        "risk_double": round(per_bike_mile["double"], 1),
    }


def page(n: dict) -> str:
    e = {k: html.escape(str(v)) for k, v in n.items()}
    f = lambda x: f"{x:,}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>How we calculate risk</title>
<meta name="description" content="One worked example, on {e['street']}, of how Ave A to B turns crash counts and rider estimates into risk per mile ridden." />
<link rel="icon" href="/favicon.svg" type="image/svg+xml" />
<link rel="stylesheet" href="/static/header.css" />
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
<style>
  :root {{ --blue: #0039a6; --orange: #ff6319; --navy: #0b1f3a; --muted: #4b5767; --bg: #f1f2f4; --card: #fff; --line: #dce0e5; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; }}
  body {{ font-family: Inter, system-ui, -apple-system, sans-serif; font-size: 17px; line-height: 1.6; color: var(--navy); background: var(--bg); }}
  main {{ max-width: 760px; margin: 0 auto; padding: 32px 20px 64px; }}
  h1 {{ font-size: 32px; line-height: 1.2; margin: 0 0 8px; letter-spacing: -0.02em; }}
  .lede {{ color: var(--muted); margin: 0 0 24px; }}
  section {{ background: var(--card); border-radius: 12px; box-shadow: 0 1px 2px rgba(11,31,58,.06), 0 4px 16px rgba(11,31,58,.06); padding: 18px 22px; margin: 0 0 14px; }}
  h2 {{ font-size: 19px; margin: 0 0 6px; display: flex; gap: 10px; align-items: baseline; }}
  h2 span {{ flex: none; width: 26px; height: 26px; border-radius: 50%; background: var(--navy); color: #fff; font-size: 14px; display: inline-grid; place-items: center; }}
  p {{ margin: 6px 0; }}
  .num {{ font-weight: 700; font-variant-numeric: tabular-nums; }}
  .calc {{ font-family: ui-monospace, Menlo, monospace; font-size: 15px; background: #f6f7f9; border-radius: 8px; padding: 8px 12px; margin: 8px 0 2px; overflow-x: auto; white-space: nowrap; }}
  .note {{ font-size: 15px; color: var(--muted); }}
  details.sources {{ margin-top: 18px; font-size: 15px; color: var(--muted); }}
  details.sources summary {{ cursor: pointer; font-weight: 600; color: var(--navy); }}
  details.sources ul {{ margin: 8px 0 0; padding-left: 20px; }}
  code {{ font-family: ui-monospace, Menlo, monospace; font-size: 14px; }}
  @media (max-width: 640px) {{ .calc {{ white-space: normal; overflow-wrap: anywhere; }} h1 {{ font-size: 26px; }} section {{ padding: 16px; }} }}
</style>
</head>
<body>
<!--#header-->
<script src="/static/header.js"></script>
<main>
<h1>How we calculate risk</h1>
<p class="lede">One worked example, on one street segment from the data: {e['street']} in {e['borough']}, a {e['length_mi']}-mile stretch of {e['road']} with {e['lane']}. Every number below comes from the same data the agents use.</p>

<section>
<h2><span>1</span>Count the crashes</h2>
<p>Police reported <span class="num">{e['crashes']}</span> crashes that hurt a cyclist on this segment since January 2021, over about {e['years']} years. {e['severe']} left a cyclist seriously hurt or killed.</p>
<p>A count this small moves a lot by chance, so we give it a 95% range: n ± 2 × √n from 10 crashes up, and the exact range for a count of rare events below 10 (0 crashes gives 0 to 3.7).</p>
<div class="calc">{e['crashes']} ± 2 × √{e['crashes']} = {e['low']} to {e['high']} crashes</div>
<p>Its own rate is {e['crashes']} crashes over {e['mile_years']} mile-years ({e['length_mi']} miles × {e['years']} years), about <span class="num">{e['own_rate']}</span> crashes per mile per year.</p>
</section>

<section>
<h2><span>2</span>Compare with similar streets</h2>
<p>"Similar streets" are the same kind of street in the same borough: {e['road']} with {e['lane']} in {e['borough']}. There are {f(n['similar_segments'])} such segments, {e['similar_miles']} miles in all.</p>
<p>Together they average <span class="num">{e['class_rate']}</span> crashes per mile per year. At that rate this segment would have about <span class="num">{e['expected']}</span> crashes since 2021, against the {e['crashes']} it had.</p>
<div class="calc">{e['class_rate']} × {e['mile_years']} mile-years = {e['expected']} expected</div>
<p>Even the low end of its range, {e['low']}, is above {e['expected']}, so this segment stands out from similar streets beyond chance. That is the test every "stretch to watch out for" passes.</p>
</section>

<section>
<h2><span>3</span>Blend the two</h2>
<p>A short segment has little data of its own, so its rate is pulled toward the similar-street average. How far depends on how much data it has and how much true rates vary between similar streets. Here the segment's own data gets <span class="num">{e['own_weight_pct']}%</span> of the weight.</p>
<div class="calc">{e['own_weight_pct']}% × {e['own_rate']} + {100 - n['own_weight_pct']}% × {e['class_rate']} ≈ {e['blended_rate']} crashes per mile per year</div>
<p>This blended estimate, <span class="num">{e['blended_rate']}</span>, is what the routes use. A quiet one-block street with one crash stays close to its similar streets, and a long one with plenty of crashes keeps close to its own count.</p>
</section>

<section>
<h2><span>4</span>Estimate how many people ride it</h2>
<p>Crashes alone favor empty streets, so we divide by an estimate of riders. Citi Bike trips nearby, scaled by the city's bike counters, give about <span class="num">{f(n['riders'])}</span> riders a day for this segment.</p>
<p>Nobody counts riders on most streets without bike lanes, so the share of riders a street like this carries is an assumption. We redo the math at half and at double that assumed gap between streets with and without lanes.</p>
</section>

<section>
<h2><span>5</span>Risk per mile ridden, with its range</h2>
<p>Divide the blended crashes by the miles people ride here in a year:</p>
<div class="calc">{e['blended_rate']} ÷ ({f(n['riders'])} riders × 365 days) × 1,000,000 ≈ {e['risk_assumed']} crashes per million miles ridden</div>
<p>At the other two rider settings it is <span class="num">{e['risk_half']}</span> and <span class="num">{e['risk_double']}</span> per million miles ridden. Routes add this up along every segment, and a route is only called the safest of these three routes when it has the lowest risk at all three settings and its range doesn't overlap the others.</p>
<p class="note">These numbers compare streets and routes. They are not anyone's personal odds, and they count only crashes the police recorded.</p>
</section>

<details class="sources">
<summary>Sources</summary>
<ul>
<li>Street segment <code>{e['segment']}</code> in <code>data/edges.parquet</code>: crashes, mile-years, the similar-streets rate, the weight, and riders a day.</li>
<li>Crashes: NYPD Motor Vehicle Collisions on NYC Open Data, since January 2021, matched to the nearest street segment.</li>
<li>Riders: Citi Bike trip data, scaled by NYC DOT bike counters.</li>
<li>The full method, with the formulas, is in the project README.</li>
</ul>
</details>
</main>
</body>
</html>
"""


def main() -> None:
    n = numbers()
    (ROOT / "method.html").write_text(page(n))
    print(json.dumps(n))


if __name__ == "__main__":
    main()
