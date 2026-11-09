"""The agents' tools: plain typed functions over data/*.parquet that return JSON-ready dicts.

Tools take edge_ids as a list, or as a ref like "route:ab12cd34" or "corridor:ab12cd34" that
route_options hands out, so the model never has to copy hundreds of IDs. Refs are hashes of their
contents, so the Verifier can re-run a tool and get the same answer.
"""

import functools
import hashlib
import math
import re
import threading
from pathlib import Path

import duckdb
import httpx
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

from streets import street_key

DATA = Path(__file__).parent / "data"
SETTINGS = ["half", "assumed", "double"]  # rider-share settings; see build/b08_risk.py
BALANCED_LAMBDA, SAFEST_LAMBDA = 1.0, 6.0
MIN_ROUTE_MILES = 0.1
MAX_SNAP_METERS = 1000
MIN_CORRIDOR_MILES = 0.3
MAX_CORRIDORS = 5
MAX_IDS = 25  # citations returned per answer; the total is always given
NOT_A_CAUSE = ["Unspecified", "Does Not Apply"]  # NYPD placeholders, not causes
SPARSE_CRASHES = 10  # below this a stretch is too sparse for segment-level conclusions
FERRY_NOTE = ("Staten Island has no bike connection to the other boroughs in this street network. "
              "The Staten Island Ferry carries bikes, so one option is to ride to the ferry terminal "
              "and cross by ferry. Ask again for each side of the trip separately.")
LIMITS = ("Police-reported crashes only, so this measures serious crash risk, not every incident. "
          "Citi Bike exposure doesn't cover all riders. These are not personal odds.")
BEFORE_AFTER_NOTE = "Ridership likely rose after these changes, and the data can't tell whether risk per rider changed."


# --- Loading ---

EDGE_COLS = ["edge_id", "u", "v", "name", "road_class", "lane_type", "lane_installed", "borough", "length_mi", "travel_min",
             "on_truck_route", "sip_id", "sip_date", "crashes", "severe_crashes", "blocked_complaints", "mile_years",
             "exposure_daily", "exposure_source", "risk_class", "crash_class_rate", "crash_own_weight", "crash_blended_rate",
             "route_risk_half", "route_risk_assumed", "route_risk_double", "wkt"]
edges = pd.read_parquet(DATA / "edges.parquet", columns=EDGE_COLS).set_index("edge_id", drop=False)
crashes = pd.read_parquet(DATA / "crashes.parquet").merge(
    pd.read_parquet(DATA / "crash_locations.parquet", columns=["collision_id", "edge_id", "near_corner"]), on="collision_id")
sips = pd.read_parquet(DATA / "edge_sips.parquet")
lanes = pd.read_parquet(DATA / "edge_lanes.parquet")
treated = pd.read_parquet(DATA / "before_after.parquet")
nodes = pd.read_parquet(DATA / "graph_nodes.parquet")
arcs = pd.read_parquet(DATA / "graph_arcs.parquet")
calibration = pd.read_parquet(DATA / "counter_calibration.parquet")

BOROUGH_RATE = edges.groupby("borough").crashes.sum() / edges.groupby("borough").mile_years.sum()
# NaN is not valid JSON, and unnamed paths are common, so give them a name.
edges["name"] = edges.name.where(edges.name.notna(), "unnamed path")
street_keys = edges.name.map(street_key)

# --- The routing graph. Parallel arcs share one matrix cell, so keep the cheapest per cell. ---

node_index = pd.Series(np.arange(len(nodes)), index=nodes.node)
arc_from = node_index.reindex(arcs.u).to_numpy()
arc_to = node_index.reindex(arcs.v).to_numpy()
arc_edge = edges.index.get_indexer(arcs.edge_id)
pair_key = arc_from.astype(np.int64) * len(nodes) + arc_to
unique_keys, arc_pair = np.unique(pair_key, return_inverse=True)
pair_from, pair_to = unique_keys // len(nodes), unique_keys % len(nodes)
MINUTES = edges.travel_min.to_numpy()
RISK = {s: edges[f"route_risk_{s}"].to_numpy() for s in SETTINGS}
# Each segment's blended expected crash count over the window, for route risk ranges.
EXPECTED_COUNT = (edges.crash_blended_rate * edges.mile_years).to_numpy()

tree = cKDTree(np.column_stack([nodes.lat * 111_000, nodes.lon * 84_000]))  # meters, near enough at NYC's latitude
_, strong = connected_components(csr_matrix((np.ones(len(pair_from)), (pair_from, pair_to)), shape=(len(nodes), len(nodes))),
                                 directed=True, connection="strong")
_, weak = connected_components(csr_matrix((np.ones(len(pair_from)), (pair_from, pair_to)), shape=(len(nodes), len(nodes))),
                               directed=True, connection="weak")
component_size = np.bincount(strong)
routable = np.flatnonzero(component_size[strong] >= 1000)  # skip tiny islands of one-way or broken streets
routable_tree = cKDTree(np.column_stack([nodes.lat.iloc[routable] * 111_000, nodes.lon.iloc[routable] * 84_000]))

# ref -> list of edge_ids
_refs: dict[str, list[str]] = {}


def remember(kind: str, edge_ids: list[str]) -> str:
    ref = f"{kind}:{hashlib.sha1(','.join(edge_ids).encode()).hexdigest()[:8]}"
    _refs[ref] = edge_ids
    return ref


def resolve(edge_ids) -> list[str]:
    """A ref or a list -> known edge IDs. Raises ValueError with what to do instead."""
    if isinstance(edge_ids, str):
        if edge_ids not in _refs:
            raise ValueError(f"Unknown ref {edge_ids}. Use a route_ref or corridor_ref from route_options.")
        return _refs[edge_ids]
    unknown = [e for e in edge_ids if e not in edges.index]
    if unknown:
        raise ValueError(f"Unknown edge IDs: {unknown[:3]}. Use IDs or refs from route_options.")
    return list(edge_ids)


def safe(tool):
    """The model can't see an exception, so every failure becomes JSON that says what to do next."""
    @functools.wraps(tool)
    def wrapper(*args, **kwargs):
        try:
            return plain(tool(*args, **kwargs))
        except (ValueError, LookupError) as e:
            return {"error": str(e), "next_step": "Fix the arguments and try again."}
        except httpx.HTTPError as e:
            return {"error": f"The geocoder failed: {type(e).__name__}", "next_step": "Try again, or use a nearby address or 'lat,lon'."}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(e)[:200]}", "next_step": "Try a different approach, or say this could not be checked."}
    return wrapper


def plain(value):
    """numpy numbers -> Python numbers, so results are ordinary JSON."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return str(value.date())
    if isinstance(value, np.generic):
        value = value.item()
    return None if isinstance(value, float) and math.isnan(value) else value


def count_range(n: float) -> dict:
    """The approximate 95% range for a count: n ± 2 × √n, floored at 0."""
    spread = 2 * math.sqrt(n)
    return {"n": int(n), "low": round(max(0.0, n - spread), 1), "high": round(n + spread, 1)}


# --- Geocoding ---

_geocoded: dict[str, dict] = {}
_nominatim_lock = threading.Lock()
LAT_LON = re.compile(r"^\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*$")
BOX = (40.45, 40.95, -74.30, -73.65)
GEOSEARCH_CONFIDENCE = 0.8
HOUSE_NUMBER = re.compile(r"^\s*\d+[A-Za-z]?(-\d+)?\s+\S")


def _intersection(address: str) -> dict | None:
    """'116th St and Broadway' -> the corner, found in our own street network. GeoSearch has no
    intersections and answers with an unrelated address instead. Returns None if this isn't a
    two-street query, and raises if the streets don't meet or meet in several places."""
    parts = re.split(r"\s+(?:and|at)\s+|\s*&\s*|\s*/\s*", address, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    second, _, area = parts[1].partition(",")
    area = area.strip().title().replace("The ", "")
    area = {"New York": "Manhattan", "Nyc": ""}.get(area, area)
    keys = street_key(parts[0]), street_key(second)
    if not all(keys):
        return None
    pools = []
    for key in keys:
        hit = street_keys.str.endswith(key) if key[0].isdigit() else street_keys == key
        pools.append(edges[hit.to_numpy() & (edges.borough.eq(area).to_numpy() if area in BOROUGH_RATE.index else True)])
    corners = (set(pools[0].u) | set(pools[0].v)) & (set(pools[1].u) | set(pools[1].v))
    if not corners:
        raise ValueError(f"Couldn't find where {parts[0].strip()} meets {second.strip()}. Add the borough, or give a street address.")
    found = nodes[nodes.node.isin(corners)]
    if found.lat.max() - found.lat.min() > 0.01 or found.lon.max() - found.lon.min() > 0.012:  # about a kilometer apart
        raise ValueError(f"{parts[0].strip()} and {second.strip()} meet in more than one place. Add the borough.")
    return {"label": f"{parts[0].strip()} and {second.strip()}", "lat": float(found.lat.mean()), "lon": float(found.lon.mean()), "source": "street network"}


def _geosearch(address: str) -> dict | None:
    """NYC Planning's GeoSearch: free, no key, and the most accurate for NYC addresses. It always
    returns its best guess, so a weak match counts as no match."""
    response = httpx.get("https://geosearch.planninglabs.nyc/v2/search", params={"text": address, "size": 1}, timeout=15)
    response.raise_for_status()
    found = response.json().get("features", [])
    if not found or found[0]["properties"].get("confidence", 0) < GEOSEARCH_CONFIDENCE:
        return None
    lon, lat = found[0]["geometry"]["coordinates"]
    return {"label": found[0]["properties"].get("label", address), "lat": lat, "lon": lon, "source": "NYC GeoSearch"}


def _nominatim(address: str) -> dict | None:
    """Places GeoSearch doesn't know, like a campus name. Nominatim allows one request a second."""
    with _nominatim_lock:
        response = httpx.get("https://nominatim.openstreetmap.org/search", timeout=15,
                             params={"q": address, "format": "jsonv2", "limit": 1, "bounded": 1,
                                     "viewbox": f"{BOX[2]},{BOX[1]},{BOX[3]},{BOX[0]}"},
                             headers={"User-Agent": "nyc-bike-commute-agent/0.1 (class project)"})
        threading.Event().wait(1.0)
    response.raise_for_status()
    found = response.json()
    if not found:
        return None
    return {"label": found[0]["display_name"], "lat": float(found[0]["lat"]), "lon": float(found[0]["lon"]), "source": "Nominatim"}


@safe
def geocode(address: str) -> dict:
    """Turn an NYC address or place name, or a 'lat,lon' string, into coordinates."""
    if address in _geocoded:
        return _geocoded[address]
    match = LAT_LON.match(address)
    if match:
        place = {"label": address.strip(), "lat": float(match[1]), "lon": float(match[2]), "source": "given"}
    else:
        # GeoSearch is best on street addresses but guesses wildly on place names ("Astoria" finds a
        # power plant), so a name without a house number goes to OpenStreetMap first.
        if HOUSE_NUMBER.match(address):
            place = _intersection(address) or _geosearch(address) or _nominatim(address)
        else:
            place = _intersection(address) or _nominatim(address) or _geosearch(address)
    if place is None:
        return {"error": f"No match for '{address}'.", "next_step": "Ask the user for a street address with a borough."}
    if not (BOX[0] <= place["lat"] <= BOX[1] and BOX[2] <= place["lon"] <= BOX[3]):
        return {"error": f"'{address}' is outside New York City.", "next_step": "Ask for a start and end inside the five boroughs."}
    _geocoded[address] = place
    return place


# --- Routing ---


def _nearest_node(place: dict) -> int:
    distance, i = routable_tree.query([place["lat"] * 111_000, place["lon"] * 84_000])
    if distance > MAX_SNAP_METERS:
        raise ValueError(f"{place['label']} is {distance:.0f} m from the nearest street in the bike network.")
    return int(routable[i])


def _matrix(cost: np.ndarray) -> csr_matrix:
    """Cheapest arc per (from, to) pair for one per-edge cost."""
    cells = np.full(len(pair_from), np.inf)
    np.minimum.at(cells, arc_pair, cost[arc_edge])
    return csr_matrix((cells, (pair_from, pair_to)), shape=(len(nodes), len(nodes)))


def _shortest(cost: np.ndarray, start: int, end: int) -> list[int]:
    """Edge positions along the cheapest path, or [] if there is none."""
    distances, previous = dijkstra(_matrix(cost), indices=start, return_predecessors=True)
    if not np.isfinite(distances[end]):
        return []
    path = [end]
    while path[-1] != start:
        path.append(int(previous[path[-1]]))
    path.reverse()
    order = np.argsort(pair_key)
    sorted_keys = pair_key[order]
    positions = []
    for a, b in zip(path, path[1:]):
        key = a * len(nodes) + b
        lo, hi = np.searchsorted(sorted_keys, [key, key + 1])
        options = arc_edge[order[lo:hi]]
        positions.append(int(options[np.argmin(cost[options])]))
    return positions


def _path_cost(lam: float, setting: str, scale: float) -> np.ndarray:
    return MINUTES + lam * RISK[setting] / scale


def _scale(setting: str, fastest: list[int]) -> float:
    """Puts risk on the same footing as minutes for this trip, so a given λ means the same thing for a
    short ride and a long one, and under each rider-share setting."""
    return max(RISK[setting][fastest].sum() / MINUTES[fastest].sum(), 1e-9)


def _corridors(positions: list[int]) -> list[dict]:
    """Split a route into runs along the same street, folding short links into their neighbor."""
    runs: list[dict] = []
    for p in positions:
        row = edges.iloc[p]
        if runs and (row["name"] == runs[-1]["name"] or row.length_mi < 0.05):
            runs[-1]["ids"].append(row.edge_id)
            runs[-1]["miles"] += row.length_mi
        else:
            runs.append({"name": row["name"], "ids": [row.edge_id], "miles": row.length_mi})
    merged: list[dict] = []
    for run in runs:
        if merged and (run["miles"] < MIN_CORRIDOR_MILES or merged[-1]["miles"] < MIN_CORRIDOR_MILES):
            merged[-1]["ids"] += run["ids"]
            merged[-1]["miles"] += run["miles"]
            if run["miles"] > merged[-1].get("longest", 0):
                merged[-1]["longest"], merged[-1]["name"] = run["miles"], run["name"]
        else:
            merged.append({**run, "longest": run["miles"]})
    while len(merged) > MAX_CORRIDORS:  # join the shortest neighbors until the Lead has few enough to fan out
        i = min(range(len(merged) - 1), key=lambda k: merged[k]["miles"] + merged[k + 1]["miles"])
        keep = merged[i] if merged[i]["miles"] >= merged[i + 1]["miles"] else merged[i + 1]
        merged[i:i + 2] = [{"name": keep["name"], "ids": merged[i]["ids"] + merged[i + 1]["ids"],
                            "miles": merged[i]["miles"] + merged[i + 1]["miles"]}]
    return [{"corridor_ref": remember("corridor", c["ids"]), "street": c["name"], "miles": round(c["miles"], 2),
             "segments": len(c["ids"])} for c in merged]


def _describe(positions: list[int], fastest: list[int]) -> dict:
    rows = edges.iloc[positions]
    ids = rows.edge_id.tolist()
    miles = rows.length_mi.sum()
    uncovered = rows.length_mi[rows.exposure_source != "citibike_x_counter_median"].sum() / miles
    return {
        "route_ref": remember("route", ids),
        "minutes": round(rows.travel_min.sum(), 1),
        "miles": round(miles, 2),
        "extra_minutes_vs_fastest": round(rows.travel_min.sum() - MINUTES[fastest].sum(), 1),
        "relative_risk_vs_fastest": {s: round(RISK[s][positions].sum() / RISK[s][fastest].sum(), 2) for s in SETTINGS},
        "crashes_5yr": count_range(rows.crashes.sum()),
        "severe_crashes_5yr": count_range(rows.severe_crashes.sum()),
        "miles_by_lane": rows.groupby("lane_type").length_mi.sum().round(2).to_dict(),
        "share_of_miles_without_citibike_coverage": round(float(uncovered), 2),
        "lower_confidence": bool(uncovered > 0),
        "corridors": _corridors(positions),
        "line": _line(rows),
    }


def _line(rows: pd.DataFrame) -> list[list[list[float]]]:
    """Map data: one [[lat, lon], ...] per segment. The model doesn't need it; see for_the_model in app.py."""
    return [[[float(y), float(x)] for x, y in (p.split() for p in w[w.index("(") + 1:-1].split(", "))] for w in rows.wkt]


@safe
def route_options(start: str, end: str) -> dict:
    """Three routes between two NYC places: fastest, balanced, and lowest risk, compared at all three
    rider-share settings. Each route lists corridors to research."""
    from_place, to_place = geocode(start), geocode(end)
    for place in (from_place, to_place):
        if "error" in place:
            return place
    a, b = _nearest_node(from_place), _nearest_node(to_place)
    if a == b:
        return {"error": "Start and end are at the same spot.", "next_step": "Ask for two different places."}
    if weak[a] != weak[b] or strong[a] != strong[b]:
        return {"no_route": True, "start": from_place["label"], "end": to_place["label"],
                "note": FERRY_NOTE if _touches_staten_island(a, b) else "These two places aren't connected in the bike network."}
    fastest = _shortest(MINUTES, a, b)
    if not fastest:
        return {"no_route": True, "note": "No path found in the bike network."}
    scales = {s: _scale(s, fastest) for s in SETTINGS}
    balanced = _shortest(_path_cost(BALANCED_LAMBDA, "assumed", scales["assumed"]), a, b)
    # The lowest-risk route can differ by setting, so each setting nominates one.
    lowest = _shortest(_path_cost(SAFEST_LAMBDA, "assumed", scales["assumed"]), a, b)
    shown = {"fastest": fastest, "balanced": balanced, "lowest_risk": lowest}
    verdict = _verdict(list(shown.values()))
    key = "safest" if verdict["label"] == "safest" else "lowest_risk_candidate"
    routes = {"fastest": _describe(fastest, fastest), "balanced": _describe(balanced, fastest), key: _describe(lowest, fastest)}
    names = list(routes)
    recommended = None if verdict["recommended"] is None else names[verdict["recommended"]]
    return {
        "start": from_place, "end": to_place,
        "routes": routes,
        "safety_label": verdict["label"],
        "routes_about_equally_safe": [names[i] for i in verdict["tied"]],
        "recommended_route": recommended,
        "safety_note": LABEL_NOTES[verdict["label"]],
        "lane_note": LANE_NOTE,
        "limits": LIMITS,
    }


LABEL_NOTES = {
    "safest": "One route has the lowest risk at all three rider-share settings, and its range doesn't overlap the "
              "others. Call it 'the safest of these three routes', never just 'the safest route'.",
    "about_equally_safe": "The routes' risk ranges overlap under at least one rider-share setting, so they are about "
                          "equally safe. Recommend the faster route. Don't call any route safest.",
    "depends_on_assumption": "Which route has lower risk reverses between rider-share settings, beyond the ranges. "
                             "It depends on an assumption about where people ride that the data can't settle. "
                             "Don't call any route safest.",
}
LANE_NOTE = ("Compare lane types only within similar streets. Painted lanes sit mostly on busy arterials, so never "
             "say painted lanes are more dangerous than no lane.")


def _risk_range(positions: list[int], setting: str) -> tuple[float, float]:
    """A route's risk with the n ± 2 × √n range of its expected crash count over the window, scaled."""
    risk = RISK[setting][positions].sum()
    n = EXPECTED_COUNT[positions].sum()
    if n <= 0:
        return risk, risk
    spread = 2 * math.sqrt(n)
    return risk * max(0.0, n - spread) / n, risk * (n + spread) / n


def _verdict(routes: list[list[int]]) -> dict:
    """Which of the shown routes is safest, by index. Identical routes count once.
    safest: one route wins at every setting and its range sits below every other route's.
    depends_on_assumption: two routes swap places between settings, with ranges apart both times.
    about_equally_safe: anything else; the quickest of the routes tied with the lowest is recommended."""
    distinct = [i for i, p in enumerate(routes) if p not in routes[:i]]
    ranges = {s: {i: _risk_range(routes[i], s) for i in distinct} for s in SETTINGS}
    winners = {s: min(distinct, key=lambda i: RISK[s][routes[i]].sum()) for s in SETTINGS}
    clear = lambda s, i, j: ranges[s][i][1] < ranges[s][j][0]  # i's whole range below j's
    lowest = winners["assumed"]
    if len(set(winners.values())) == 1 and all(clear(s, lowest, j) for s in SETTINGS for j in distinct if j != lowest):
        return {"label": "safest", "recommended": lowest, "tied": []}
    for s1 in SETTINGS:
        for s2 in SETTINGS:
            i, j = winners[s1], winners[s2]
            if i != j and clear(s1, i, j) and clear(s2, j, i):
                return {"label": "depends_on_assumption", "recommended": None, "tied": []}
    # Routes no setting puts clearly behind its winner are tied with it; take the quickest of those.
    tied = [i for i in distinct if not any(clear(s, winners[s], i) for s in SETTINGS)]
    faster = min(tied, key=lambda i: MINUTES[routes[i]].sum())
    return {"label": "about_equally_safe", "recommended": faster, "tied": tied}


def _touches_staten_island(a: int, b: int) -> bool:
    for node in (a, b):
        row = nodes.iloc[node]
        if 40.48 <= row.lat <= 40.65 and row.lon <= -74.05:
            return True
    return False


@safe
def route_for_lambda(start: str, end: str, lam: float) -> dict:
    """One route for the page's λ slider. Not an agent tool: it compares nothing, it just redraws."""
    from_place, to_place = geocode(start), geocode(end)
    a, b = _nearest_node(from_place), _nearest_node(to_place)
    fastest = _shortest(MINUTES, a, b)
    if not fastest:
        return {"error": "No route.", "next_step": "Pick two connected places."}
    positions = _shortest(_path_cost(lam, "assumed", _scale("assumed", fastest)), a, b)
    return _describe(positions, fastest)


# --- Evidence tools ---


def _top(series: pd.Series, k: int = 5) -> dict:
    return {str(a): int(b) for a, b in series.value_counts().head(k).items()}


def _filtered(rows: pd.DataFrame, filters: dict | None) -> pd.DataFrame:
    filters = filters or {}
    allowed = {"year_from", "year_to", "severe_only", "rider_type", "hour_from", "hour_to"}
    if set(filters) - allowed:
        raise ValueError(f"Unknown filters {sorted(set(filters) - allowed)}. Allowed: {sorted(allowed)}.")
    if "year_from" in filters:
        rows = rows[rows.year >= filters["year_from"]]
    if "year_to" in filters:
        rows = rows[rows.year <= filters["year_to"]]
    if filters.get("severe_only"):
        rows = rows[rows.severe]
    if "rider_type" in filters:
        if filters["rider_type"] not in ("pedal", "e-bike"):
            raise ValueError("rider_type must be 'pedal' or 'e-bike'.")
        rows = rows[rows.rider_type == filters["rider_type"]]
    if "hour_from" in filters:
        rows = rows[rows.hour >= filters["hour_from"]]
    if "hour_to" in filters:
        rows = rows[rows.hour < filters["hour_to"]]
    return rows


def _expected(rows: pd.DataFrame) -> float:
    return float((rows.crash_class_rate * rows.mile_years).sum())


def _hotspots(segments: pd.DataFrame, every: pd.DataFrame, found: pd.DataFrame) -> list[dict]:
    """Busiest segments. A stretch is only above average if its whole range sits above the class or
    borough average. That check uses every crash in the window, since the averages do too."""
    counts = every.groupby("edge_id").size().sort_values(ascending=False).head(5)
    spots = []
    for edge_id, n in counts.items():
        row = segments.loc[edge_id]
        here = every[every.edge_id == edge_id]
        factors = here.contributing_factor_vehicle_1[~here.contributing_factor_vehicle_1.isin(NOT_A_CAUSE)].dropna()
        spread = count_range(n)
        class_expected = row.crash_class_rate * row.mile_years
        borough_expected = BOROUGH_RATE.get(row.borough, 0) * row.mile_years
        spots.append({"edge_id": edge_id, "street": row["name"], "crashes": spread,
                      "crashes_matching_filters": int((found.edge_id == edge_id).sum()), "main_cause": _top(factors, 1),
                      "class_expected": round(class_expected, 1), "borough_expected": round(borough_expected, 1),
                      "above_average": bool(spread["low"] > min(class_expected, borough_expected)),
                      "collision_ids": here.sort_values("crash_date", ascending=False).collision_id.head(5).tolist()})
    return spots


@safe
def crash_summary(edge_ids, filters: dict | None = None) -> dict:
    """Crashes on a route, corridor, or list of segments, with causes, timing, and collision IDs.
    Filters narrow the counts, causes, and IDs. Averages and hotspots always use every crash, 2021 on."""
    ids = resolve(edge_ids)
    segments = edges.loc[ids]
    every = crashes[crashes.edge_id.isin(ids)]
    found = _filtered(every, filters)
    factors = pd.concat([found.contributing_factor_vehicle_1, found.contributing_factor_vehicle_2]).dropna()
    hours = pd.cut(found.hour, [-1, 5, 9, 15, 19, 23], labels=["overnight 0-5", "morning 6-9", "midday 10-15", "evening 16-19", "night 20-23"])
    expected = _expected(segments)
    borough_expected = float((segments.borough.map(BOROUGH_RATE).fillna(0) * segments.mile_years).sum())
    total = count_range(len(every))
    return {
        "edge_count": len(ids), "miles": round(segments.length_mi.sum(), 2),
        "filters": filters or {},
        "crashes_matching_filters": count_range(len(found)), "severe_matching_filters": count_range(found.severe.sum()),
        "killed_matching_filters": int(found.killed.sum()),
        "all_crashes": total, "class_expected_all_crashes": round(expected, 1),
        "borough_expected_all_crashes": round(borough_expected, 1),
        # The same rule as a hotspot's: the whole range above the class or the borough average
        "above_average": bool(total["low"] > min(expected, borough_expected)),
        "too_sparse_for_segment_conclusions": len(every) < SPARSE_CRASHES,
        "by_cause": _top(factors[~factors.isin(NOT_A_CAUSE)]),
        "by_time_of_day": {str(k): int(v) for k, v in hours.value_counts().sort_index().items()},
        "by_rider_type": _top(found.rider_type),
        "rider_type_note": "E-bike counts are not comparable across years: NYPD changed how it labels these riders in 2024 and 2025.",
        "hotspots": _hotspots(segments, every, found),
        "collision_ids": found.sort_values("crash_date", ascending=False).collision_id.head(MAX_IDS).tolist(),
        "collision_ids_total": len(found),
    }


@safe
def infrastructure_summary(edge_ids) -> dict:
    """Lanes, install dates, SIPs, truck routes, and blocked-lane complaints on a stretch."""
    ids = resolve(edge_ids)
    segments = edges.loc[ids]
    installed = segments[segments.lane_installed >= "2021-01-01"]
    projects = sips[sips.edge_id.isin(ids)].drop_duplicates("sip_id")
    return {
        "miles": round(segments.length_mi.sum(), 2),
        "miles_by_lane": segments.groupby("lane_type").length_mi.sum().round(2).to_dict(),
        "truck_route_miles": round(segments.length_mi[segments.on_truck_route].sum(), 2),
        "blocked_lane_complaints": count_range(segments.blocked_complaints.sum()),
        "lanes_installed_in_window": [{"edge_id": r.edge_id, "street": r.name, "lane_type": r.lane_type, "installed": str(r.lane_installed.date())}
                                      for r in installed.head(MAX_IDS).itertuples()],
        "lanes_installed_total": len(installed),
        "sips": [{"sip_id": r.sip_id, "project": r.pjct_name, "year": int(r.sip_year)} for r in projects.sort_values("sip_year").itertuples()],
    }


@safe
def exposure_summary(edge_ids) -> dict:
    """Estimated riders on a stretch, and how far that estimate can be trusted."""
    ids = resolve(edge_ids)
    segments = edges.loc[ids]
    covered = segments.exposure_source == "citibike_x_counter_median"
    share = segments.length_mi[covered].sum() / segments.length_mi.sum()
    return {
        "estimated_riders_per_day": round(float(np.average(segments.exposure_daily, weights=segments.length_mi))),
        "source": "Citi Bike trips near each segment, scaled by the median DOT counter, then an assumed share by street type.",
        "share_of_miles_with_citibike_coverage": round(float(share), 2),
        "lower_confidence": bool(share < 1),
        "fallback": None if share == 1 else "Uncovered segments use the median of their similar-street class.",
        "counter_calibration": {"sensors": len(calibration), "median_ratio": round(float(calibration.ratio.median()), 2),
                                "lowest_ratio": round(float(calibration.ratio.min()), 1), "highest_ratio": round(float(calibration.ratio.max()), 1)},
        "use": "Rough estimate of how busy this is. Compare routes; never turn this into a rider's odds.",
    }


@safe
def similar_streets(edge_ids) -> dict:
    """How a stretch's crashes compare with the expected count for similar streets, class by class."""
    ids = resolve(edge_ids)
    segments = edges.loc[ids]
    rows = []
    for name, group in segments.groupby("risk_class"):
        lane, road, area = name.split("|")
        observed = count_range(group.crashes.sum())
        expected = _expected(group)
        rows.append({"class": name, "lane_type": lane, "road_class": road, "area": area, "miles": round(group.length_mi.sum(), 2),
                     "crashes": observed, "class_expected": round(expected, 1), "class_rate_per_mile_year": round(float(group.crash_class_rate.iloc[0]), 2),
                     "blended_rate_per_mile_year": round(float(np.average(group.crash_blended_rate, weights=group.length_mi)), 2),
                     "range_above_class": bool(observed["low"] > expected), "range_below_class": bool(observed["high"] < expected)})
    return {"classes": sorted(rows, key=lambda r: -r["miles"]),
            "note": "Class rates are per mile, not per rider. A busy class is not automatically a risky one."}


def _did(group: pd.DataFrame) -> dict:
    before, after = int(group.crashes_pre.sum()), int(group.crashes_post.sum())
    pre_years, post_years = group.length_mi * group.pre_years, group.length_mi * group.post_years
    result = {"segments": group.edge_id.nunique(), "miles": round(group.length_mi.sum(), 2),
              "crashes_before": count_range(before), "crashes_after": count_range(after),
              "rate_before": round(before / pre_years.sum(), 2), "rate_after": round(after / post_years.sum(), 2),
              "comparison_rate_before": round(float(np.average(group.control_pre_rate, weights=pre_years)), 2),
              "comparison_rate_after": round(float(np.average(group.control_post_rate, weights=post_years)), 2)}
    if before == 0 or after == 0:
        return {**result, "finding": "Too few crashes on this stretch to compare before and after.", "too_sparse": True}
    ratio = (after / post_years.sum() / (before / pre_years.sum())) / (result["comparison_rate_after"] / result["comparison_rate_before"])
    spread = 2 * math.sqrt(1 / before + 1 / after)
    low, high = ratio * math.exp(-spread), ratio * math.exp(spread)
    sparse = before + after < SPARSE_CRASHES * 2
    if high < 1:
        finding = "Crash counts fell by more than on comparison streets."
    else:
        finding = "Crash counts did not fall relative to comparison streets."
    return {**result, "did_ratio": round(ratio, 2), "did_range": [round(low, 2), round(high, 2)], "too_sparse": sparse,
            "finding": "Too few crashes on this stretch to compare before and after." if sparse else f"{finding} {BEFORE_AFTER_NOTE}"}


def _project_name(project: str, edge_id: str) -> str:
    """SIPs keep their name. A lane has only a DOT record number, so name it by its street."""
    if project.startswith("bike route"):
        return f"Lane on {edges.at[edge_id, 'name']}"
    return project.split(": ", 1)[-1] or project


@safe
def before_after(edge_ids) -> dict:
    """Difference-in-differences for segments that got a lane or SIP in the window."""
    ids = resolve(edge_ids)
    found = treated[treated.edge_id.isin(ids)]
    if found.empty:
        return {"treated_segments": 0, "finding": "No lane or SIP on this stretch can be compared before and after."}
    results = []
    for name, group in found.groupby("treatment"):
        named = group.assign(name=[_project_name(p, e) for p, e in zip(group.project, group.edge_id)])
        projects = named.drop_duplicates("name").sort_values("treated_on")
        results.append({"treatment": name, **_did(group),
                        "projects": [{"project": p.name, "year": int(p.treated_on.year)} for p in projects.head(8).itertuples()]})
    return {"treated_segments": found.edge_id.nunique(), "results": results,
            "wording": "Report the finding as written. Never say a lane worked or failed. Causation can't be shown here.",
            "range_note": "The range reflects crash-count noise on treated streets only, so it is too narrow."}


# --- SQL ---

SQL_TABLES = ["crashes", "crash_people", "crash_locations", "edge_lanes", "edge_sips", "before_after", "citibike_h3",
              "citibike_stations", "counter_daily", "counter_sensors", "blocked_edges"]
SQL_LIMIT = 100
SQL_SECONDS = 20
_database = duckdb.connect(":memory:")
for table in SQL_TABLES:
    _database.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{DATA / (table + '.parquet')}')")
# Per-rider columns stay out on purpose: routes are compared, and those numbers are never quoted.
_database.execute(f"""CREATE TABLE edges AS SELECT * EXCLUDE (wkt, route_risk_half, route_risk_assumed, route_risk_double, exposure_scale)
                      FROM read_parquet('{DATA / 'edges.parquet'}')""")
_database.execute("SET enable_external_access = false; SET lock_configuration = true")


@safe
def run_sql(query: str) -> dict:
    """Read-only DuckDB over the project's tables, for anything the other tools don't cover."""
    statements = duckdb.extract_statements(query)
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        return {"error": "Only one SELECT statement is allowed.", "next_step": "Rewrite it as a single SELECT."}
    cursor = _database.cursor()
    timer = threading.Timer(SQL_SECONDS, cursor.interrupt)
    timer.start()
    try:
        frame = cursor.execute(query).fetch_df()
    finally:
        timer.cancel()
    return {"rows": frame.head(SQL_LIMIT).astype(object).where(frame.head(SQL_LIMIT).notna(), None).to_dict("records"),
            "row_count": len(frame), "truncated": len(frame) > SQL_LIMIT}


def edge_point(edge_id: str) -> dict:
    """A segment's middle point, for a map marker. Not an agent tool."""
    line = _line(edges.loc[[edge_id]])[0]
    lat, lon = line[len(line) // 2]
    return {"lat": lat, "lon": lon}
