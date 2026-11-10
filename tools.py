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
# Common places, so landmarks work on the first try without a network call. The page's autocomplete
# offers the same list (GET /places). Coordinates are from OpenStreetMap.
PLACES = [
    ("Columbia University", "Manhattan", 40.80775, -73.96249, []),
    ("Union Square", "Manhattan", 40.73607, -73.99019, []),
    ("Times Square", "Manhattan", 40.75701, -73.98597, []),
    ("Grand Central", "Manhattan", 40.75269, -73.97725, ["grand central terminal", "grand central station"]),
    ("NYU", "Manhattan", 40.72943, -73.99722, ["new york university", "nyu bobst library"]),
    ("Washington Square Park", "Manhattan", 40.73090, -73.99760, ["washington square"]),
    ("Penn Station", "Manhattan", 40.75129, -73.99036, ["pennsylvania station"]),
    ("Columbus Circle", "Manhattan", 40.76807, -73.98189, []),
    ("Bryant Park", "Manhattan", 40.75375, -73.98354, []),
    ("Hudson Yards", "Manhattan", 40.75591, -74.00053, []),
    ("World Trade Center", "Manhattan", 40.71190, -74.01253, ["wtc"]),
    ("Wall Street", "Manhattan", 40.70764, -74.01147, []),
    ("Harlem", "Manhattan", 40.80786, -73.94547, []),
    ("DUMBO", "Brooklyn", 40.70291, -73.99012, []),
    ("Prospect Park", "Brooklyn", 40.66177, -73.97109, []),
    ("Barclays Center", "Brooklyn", 40.68251, -73.97525, []),
    ("Prospect Heights", "Brooklyn", 40.67787, -73.96847, []),
    ("Park Slope", "Brooklyn", 40.67010, -73.98597, []),
    ("Williamsburg", "Brooklyn", 40.71462, -73.95345, []),
    ("Brooklyn Bridge Park", "Brooklyn", 40.69829, -73.99993, []),
    ("Astoria", "Queens", 40.77201, -73.93027, []),
    ("Long Island City", "Queens", 40.74553, -73.94850, ["lic"]),
    ("Yankee Stadium", "Bronx", 40.82958, -73.92652, []),
]
BOROUGH_WORDS = {"manhattan": "Manhattan", "new york": "Manhattan", "brooklyn": "Brooklyn", "queens": "Queens",
                 "bronx": "Bronx", "the bronx": "Bronx", "staten island": "Staten Island", "ny": None, "nyc": None,
                 "new york city": None, "new york ny": None, "usa": None}


def _split_borough(address: str) -> tuple[str, str | None]:
    """'Union Square, Manhattan' -> ('union square', 'Manhattan'). Trailing city or borough words come off."""
    parts = [x.strip() for x in address.lower().replace(".", "").split(",")]
    borough = None
    while len(parts) > 1 and parts[-1] in BOROUGH_WORDS:
        borough = borough or BOROUGH_WORDS[parts.pop()]
    name = re.sub(r"^the\s+", "", " ".join(", ".join(parts).split()))
    return name, borough


_suggested: dict[str, list[dict]] = {}


def suggest(text: str, limit: int = 5) -> list[dict]:
    """Autocomplete for the page: built-in places first (names starting with the text before names that
    contain it), then NYC GeoSearch with a short timeout. If GeoSearch is slow or down, the places still answer."""
    name, _ = _split_borough(text)
    if len(name) < 2:
        return []
    hits = [(not title.lower().startswith(name), title, home) for title, home, *_ , aliases in PLACES
            if name in title.lower() or any(name in a for a in aliases)]
    out = [{"label": title, "detail": home, "value": f"{title}, {home}"} for _, title, home in sorted(hits)]
    if len(out) < limit and len(name) >= 3:
        key = text.strip().lower()
        if key not in _suggested:
            try:
                response = httpx.get("https://geosearch.planninglabs.nyc/v2/autocomplete", params={"text": text}, timeout=2.5)
                response.raise_for_status()
                _suggested[key] = [(_tidy(f["properties"].get("label", "")), f["properties"].get("borough") or "")
                                   for f in response.json().get("features", []) if f["properties"].get("label")]
                if len(_suggested) > 2000:
                    _suggested.pop(next(iter(_suggested)))
            except (httpx.HTTPError, ValueError):
                return out[:limit]
        seen = {o["value"].lower() for o in out}
        for label, borough in _suggested[key]:
            street = label.split(", ")[0]
            borough = borough or label.partition(", ")[2]  # GeoSearch labels Manhattan "New York", so prefer its borough field
            value = f"{street}, {borough}" if borough else street
            if value.lower() not in seen:
                seen.add(value.lower())
                out.append({"label": street, "detail": borough, "value": value})
    return out[:limit]


def _tidy(label: str) -> str:
    """'851 UNION STREET, Brooklyn, New York, NY, USA' -> '851 Union Street, Brooklyn'."""
    for tail in (", New York, NY, USA", ", NY, USA", ", USA", ", NY"):
        label = label.removesuffix(tail)
    parts = [re.sub(r"'S\b", "'s", p.title()) if p.isupper() else p for p in label.split(", ")]
    parts[0] = re.sub(r"\bB'Way\b", "Broadway", parts[0])  # the city's short spelling, so it dedupes
    return re.sub(r"\b(\d+)(St|Nd|Rd|Th)\b", lambda m: m[1] + m[2].lower(), ", ".join(parts))


def _place(address: str) -> dict | None:
    """A built-in place, matched on its name or alias. A borough that disagrees means it's something else."""
    name, borough = _split_borough(address)
    for title, home, lat, lon, aliases in PLACES:
        if name in [title.lower(), *aliases] and borough in (None, home):
            return {"label": f"{title}, {home}", "lat": lat, "lon": lon, "source": "built-in place"}
    return None


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


def _first(address: str, services: tuple) -> dict | None:
    """The first service that finds the place. A service that times out or fails is skipped, so one slow
    geocoder doesn't fail the whole question; the error is raised only if every service failed."""
    failure = None
    for service in services:
        try:
            place = service(address)
        except httpx.HTTPError as e:
            failure = e
            continue
        if place:
            return place
    if failure:
        raise failure
    return None


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
        order = (_geosearch, _nominatim) if HOUSE_NUMBER.match(address) else (_nominatim, _geosearch)
        place = _place(address) or _intersection(address) or _first(address, order)
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


# Two paths can cost exactly the same, and then Linux and macOS pick different ones. A fixed offset of at
# most a millionth of a minute per segment makes every path's cost unique, so routes match everywhere.
TIE_BREAK = np.random.default_rng(0).random(len(edges)) * 1e-6


def _matrix(cost: np.ndarray) -> csr_matrix:
    """Cheapest arc per (from, to) pair for one per-edge cost."""
    cells = np.full(len(pair_from), np.inf)
    np.minimum.at(cells, arc_pair, cost[arc_edge] + TIE_BREAK[arc_edge])
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
    order = np.argsort(pair_key, kind="stable")  # the default sort orders ties differently on Linux and macOS
    sorted_keys = pair_key[order]
    positions = []
    for a, b in zip(path, path[1:]):
        key = a * len(nodes) + b
        lo, hi = np.searchsorted(sorted_keys, [key, key + 1])
        options = arc_edge[order[lo:hi]]
        positions.append(int(options[np.argmin(cost[options] + TIE_BREAK[options])]))
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
        "streets": _streets(rows),
        "line": _line(rows),
        "segment_risk": _segment_risk(positions, fastest),
        "turns": _turns(rows),
    }


def _streets(rows: pd.DataFrame) -> list[dict]:
    """Map data: every street on the route in riding order, skipping bits under 0.05 miles."""
    runs: list[dict] = []
    for name, borough, miles in zip(rows["name"].fillna("unnamed path"), rows.borough, rows.length_mi):
        if runs and runs[-1]["name"] == name:
            runs[-1]["miles"] += miles
        else:
            runs.append({"name": name, "borough": borough, "miles": miles})
    kept: list[dict] = []
    for run in runs:
        if run["miles"] < 0.05:
            continue
        if kept and kept[-1]["name"] == run["name"]:
            kept[-1]["miles"] += run["miles"]
        else:
            kept.append(dict(run))
    return [{**r, "miles": round(float(r["miles"]), 2)} for r in kept]


def _segment_risk(positions: list[int], fastest: list[int]) -> list[float]:
    """Map data: each segment's risk per mile against the fastest route's average per mile, assumed setting."""
    per_mile = RISK["assumed"][positions] / np.maximum(edges.length_mi.to_numpy()[positions], 1e-6)
    baseline = RISK["assumed"][fastest].sum() / max(edges.length_mi.to_numpy()[fastest].sum(), 1e-6)
    return [round(float(v), 2) for v in per_mile / max(baseline, 1e-9)]


def _turns(rows: pd.DataFrame, limit: int = 8) -> list[list[float]]:
    """Map data: a point where the route moves to a new street, for directions links. At most 8, spread out."""
    lines = _line(rows)
    names = rows["name"].tolist()
    points = [lines[i][0] for i in range(1, len(names)) if names[i] != names[i - 1] and names[i] != "unnamed path"]
    if len(points) > limit:
        points = [points[round(i * (len(points) - 1) / (limit - 1))] for i in range(limit)]
    return points


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
    routes = {"fastest": _describe(fastest, fastest), "balanced": _describe(balanced, fastest), "lowest_risk": _describe(lowest, fastest)}
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
def route_for_lambda(start: str, end: str, lam: float, avoid: list[str] | None = None) -> dict:
    """One route for the page's λ slider, or one that avoids some segments ("Avoid this stretch"). Not an
    agent tool: it compares nothing, it just redraws. Risk is still compared with the usual fastest route."""
    from_place, to_place = geocode(start), geocode(end)
    a, b = _nearest_node(from_place), _nearest_node(to_place)
    fastest = _shortest(MINUTES, a, b)
    if not fastest:
        return {"error": "No route.", "next_step": "Pick two connected places."}
    cost = _path_cost(lam, "assumed", _scale("assumed", fastest)).copy()
    if avoid:
        blocked = edges.index.get_indexer(list(avoid))
        if (blocked < 0).any():
            raise ValueError("Unknown segment ID.")
        cost[blocked] = np.inf
    positions = _shortest(cost, a, b)
    if not positions or (avoid and set(edges.edge_id.iloc[positions]) & set(avoid)):
        return {"error": "There's no route around this stretch.", "next_step": "The start or end is on it."}
    return {**_describe(positions, fastest), "avoided": list(avoid or [])}


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
    """Busiest segments. A stretch stands out only if its whole range sits above the average for
    similar streets. That check uses every crash in the window, since the averages do too."""
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
                      "above_average": bool(spread["low"] > class_expected),
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
        # The same rule as a hotspot's: the whole range above the similar-street (class) average
        "above_average": bool(total["low"] > expected),
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


def _per_year(value: float) -> str:
    return f"{value:.1f}"


def _did(group: pd.DataFrame, noun: str) -> dict:
    """Crashes per year on the changed street, before and after, next to similar streets that weren't
    changed (scaled to the same length). The finding states the raw change first, then the comparison."""
    before, after = int(group.crashes_pre.sum()), int(group.crashes_post.sum())
    miles = group.length_mi.sum()
    pre_years, post_years = group.length_mi * group.pre_years, group.length_mi * group.post_years
    comparison_before = float(np.average(group.control_pre_rate, weights=pre_years))
    comparison_after = float(np.average(group.control_post_rate, weights=post_years))
    yearly = {"before": before / (pre_years.sum() / miles), "after": after / (post_years.sum() / miles),
              "comparison_before": comparison_before * miles, "comparison_after": comparison_after * miles}
    result = {"segments": group.edge_id.nunique(), "miles": round(miles, 2),
              "crashes_before": count_range(before), "crashes_after": count_range(after),
              "years_before": round(float(pre_years.sum() / miles), 1), "years_after": round(float(post_years.sum() / miles), 1),
              "crashes_per_year": {k: round(v, 1) for k, v in yearly.items()},
              "comparison_note": "Comparison: similar streets in the same borough that weren't changed, scaled to this length."}
    sparse = before == 0 or after == 0 or before + after < SPARSE_CRASHES * 2
    if sparse:
        return {**result, "too_sparse": True,
                "finding": f"Too few crashes to compare before and after ({before} before, {after} after)."}
    ratio = (yearly["after"] / yearly["before"]) / (comparison_after / comparison_before)
    spread = 2 * math.sqrt(1 / before + 1 / after)
    low, high = ratio * math.exp(-spread), ratio * math.exp(spread)
    raw = _change("Crashes", yearly["before"], yearly["after"])
    similar = _change("", comparison_before * miles, comparison_after * miles)  # "fell from 3.7 to 3.6 a year"
    same_direction = similar.split()[0] == raw.split()[1]
    if low <= 1 <= high and same_direction:
        compare = f"{raw}, but similar streets that weren't changed {similar.split()[0]} by about as much."
    elif low <= 1 <= high:
        compare = (f"{raw}. Similar streets that weren't changed {similar}, but a change this size is within "
                   f"normal year-to-year swings.")
    elif high < 1:
        compare = f"{raw}, a bigger drop than on similar streets that weren't changed, which {similar}."
    else:
        compare = f"{raw}, a worse trend than on similar streets that weren't changed, which {similar}."
    finding = f"{compare} Ridership likely rose too, so the data can't tell whether the {noun} changed risk per rider."
    return {**result, "did_ratio": round(ratio, 2), "did_range": [round(low, 2), round(high, 2)], "too_sparse": False,
            "finding": finding}


def _change(subject: str, before: float, after: float) -> str:
    """'Crashes fell from 8.2 to 6.1 a year', or 'rose', or 'stayed about the same'."""
    if abs(after - before) <= 0.05 * max(before, 1e-9) or _per_year(before) == _per_year(after):
        return f"{subject} stayed about the same, near {_per_year(before)} a year".strip()
    verb = "fell" if after < before else "rose"
    return f"{subject} {verb} from {_per_year(before)} to {_per_year(after)} a year".strip()


def _project_name(project: str, edge_id: str) -> str:
    """SIPs keep their name. A lane has only a DOT record number, so name it by its street."""
    if project.startswith("bike route"):
        return f"Lane on {edges.at[edge_id, 'name']}"
    return project.split(": ", 1)[-1] or project


@safe
def before_after(edge_ids) -> dict:
    """Before and after, one result per project: each new lane (by street) or Street Improvement Project
    since 2021, against similar streets that weren't changed."""
    ids = resolve(edge_ids)
    found = treated[treated.edge_id.isin(ids)]
    if found.empty:
        return {"treated_segments": 0, "results": [], "finding": "No lane or project on this stretch can be compared before and after."}
    found = found.assign(name=[_project_name(p, e) for p, e in zip(found.project, found.edge_id)])
    results = []
    for (treatment, name), group in found.groupby(["treatment", "name"]):
        noun = "redesign" if treatment == "SIP" else "lane"
        kind = "Street Improvement Project" if treatment == "SIP" else f"new {treatment.removeprefix('lane: ')} lane"
        results.append({"project": name, "year": int(group.treated_on.min().year), "kind": kind, **_did(group, noun)})
    results.sort(key=lambda r: (r["too_sparse"], -(r["crashes_before"]["n"] + r["crashes_after"]["n"])))
    return {"treated_segments": found.edge_id.nunique(), "results": results,
            "wording": "Quote each finding as written. Never say a lane worked or failed. Causation can't be shown here.",
            "range_note": "The range reflects crash-count noise on the changed streets only, so it is too narrow."}


# One plain tip per police-reported main cause. General advice, no odds. First keyword that matches wins.
TIPS = [
    ("door", "Ride a door's width away from parked cars."),
    ("oversized", "Large trucks use this street. Stay out of their blind spots, and don't pass a turning truck on the right."),
    ("yield", "Drivers here often fail to yield. At crossings, slow down and don't assume a turning car will stop."),
    ("inattention", "Distracted driving is the most common cause here. Be visible, use lights, and leave room to stop."),
    ("distraction", "Distracted driving is the most common cause here. Be visible, use lights, and leave room to stop."),
    ("traffic control", "Drivers here have run lights and signs. Check cross traffic before you go, even on a green."),
    ("unsafe speed", "Cars here often go too fast. Ride predictably and give yourself extra room at intersections."),
    ("turning", "Watch for vehicles turning across your path at corners."),
    ("passing", "Vehicles here pass too closely or use the lane badly. Hold a steady line and take the lane when it's too narrow to share."),
    ("lane changing", "Vehicles here change lanes unsafely. Hold a steady line and watch for merging cars."),
    ("following too closely", "Vehicles here follow too closely. Signal early and avoid sudden stops."),
    ("backing", "Watch for vehicles backing out of driveways and loading zones."),
    ("view obstructed", "Sight lines are poor here. Slow down where parked vans or trucks block the view."),
    ("pedestrian/bicyclist", "Reports here often cite rider or pedestrian error. Signal clearly and ride predictably."),
    ("inexperience", "Reports here often cite driver inexperience. Ride predictably and make yourself easy to see."),
    ("alcohol", "Some crashes here involved alcohol. Take extra care late at night."),
    ("aggressive", "Reports here cite aggressive driving. Give drivers room and don't engage."),
    ("pavement", "The pavement here is a reported factor. Watch the road surface, especially when wet."),
    ("obstruction", "Debris or obstructions are a reported factor. Watch the road ahead."),
    ("uninvolved vehicle", "Sudden moves by other vehicles are a reported factor here. Keep space around you and scan ahead."),
]
DEFAULT_TIP = "Ride predictably, use lights, and watch for turning vehicles at corners."


def watch_out_tip(cause: str | None) -> str:
    text = (cause or "").lower()
    return next((tip for key, tip in TIPS if key in text), DEFAULT_TIP)


@safe
def standout_segments(edge_ids, limit: int = 5) -> dict:
    """Segments whose whole crash range (n ± 2 × √n, since 2021) sits above the average for similar
    streets, busiest first. These are the only stretches the answer may single out."""
    ids = resolve(edge_ids)
    segments = edges.loc[list(dict.fromkeys(ids))]
    expected = segments.crash_class_rate * segments.mile_years
    low = segments.crashes - 2 * np.sqrt(segments.crashes)
    picked = segments[low > expected].sort_values("crashes", ascending=False).head(limit)
    every = crashes[crashes.edge_id.isin(picked.index)]
    spots = []
    for number, row in enumerate(picked.itertuples(), start=1):
        here = every[every.edge_id == row.edge_id]
        causes = here.contributing_factor_vehicle_1[~here.contributing_factor_vehicle_1.isin(NOT_A_CAUSE)].dropna()
        spots.append({"number": number, "edge_id": row.edge_id, "street": row.name, "crashes": count_range(row.crashes),
                      "similar_streets_expected": round(float(row.crash_class_rate * row.mile_years), 1),
                      "main_cause": next(iter(_top(causes, 1)), None), **edge_point(row.edge_id),
                      "tip": watch_out_tip(next(iter(_top(causes, 1)), None)),
                      "collision_ids": here.sort_values("crash_date", ascending=False).collision_id.head(5).tolist()})
    return {"hotspots": spots,
            "finding": None if spots else "No single stretch stands out from similar streets."}


# --- Area questions ("Is biking in Queens safe?") ---

AREAS = {"manhattan": "Manhattan", "brooklyn": "Brooklyn", "queens": "Queens", "bronx": "Bronx", "the bronx": "Bronx",
         "staten island": "Staten Island", "new york city": None, "nyc": None, "citywide": None, "new york": None}
RIDERS_CAVEAT = ("A lower crash rate per mile can partly reflect fewer people riding there, not only safer streets. "
                 "Rider counts are thin outside Manhattan, so risk per rider can't be compared between boroughs.")
AREA_LIMITS = ["Police-reported crashes that hurt a cyclist, since 2021, so this measures serious crash risk, not every incident.",
               RIDERS_CAVEAT,
               "These are crashes per mile of street, not anyone's personal odds.",
               "A corridor that stands out had more crashes than similar streets (same lane type, road type, and borough), "
               "even allowing for chance."]


@safe
def area_summary(area: str) -> dict:
    """Borough-level answer for a question about an area with no trip: crashes per mile of street per year
    against the citywide figure, the share of street miles with protected or painted lanes, and the three
    corridors that stand out most compared to similar streets. area is a borough or 'New York City'."""
    key = area.lower().strip().removesuffix(", ny").removesuffix(", new york")
    if key not in AREAS:
        raise ValueError(f"'{area}' isn't a borough. Pass Manhattan, Brooklyn, Queens, Bronx, Staten Island, or New York City.")
    borough = AREAS[key]
    rows = edges if borough is None else edges[edges.borough == borough]
    rate = float(rows.crashes.sum() / rows.mile_years.sum())
    city = float(edges.crashes.sum() / edges.mile_years.sum())
    lanes = rows.groupby("lane_type").length_mi.sum() / rows.length_mi.sum()
    named = rows[rows["name"].notna()]
    # A street name is one corridor per borough: Broadway in Manhattan and Broadway in Queens are different streets
    streets = named.assign(expected=named.crash_class_rate * named.mile_years).groupby(["name", "borough"]).agg(
        crashes=("crashes", "sum"), expected=("expected", "sum"), miles=("length_mi", "sum"))
    streets = streets[streets.crashes - 2 * np.sqrt(streets.crashes) > streets.expected]
    top = streets.assign(excess=streets.crashes - streets.expected).sort_values("excess", ascending=False).head(3)
    change = round((rate / city - 1) * 100)
    return {
        "area": borough or "New York City",
        "crashes_per_mile_per_year": round(rate, 2),
        "citywide_crashes_per_mile_per_year": round(city, 2),
        "compared_with_citywide": None if borough is None else ("about the same" if change == 0 else f"{abs(change)}% {'higher' if change > 0 else 'lower'}"),
        "street_miles": round(float(rows.length_mi.sum())),
        "share_protected_lanes": round(float(lanes.get("protected", 0) + lanes.get("path", 0)), 3),
        "share_painted_lanes": round(float(lanes.get("painted", 0)), 3),
        "corridors_that_stand_out": [{"street": name if borough else f"{name}, {home}", "borough": home, "crashes": int(r.crashes),
                                      "similar_streets_expected": round(float(r.expected), 1), "miles": round(float(r.miles), 1)}
                                     for (name, home), r in top.iterrows()],
        "limits": AREA_LIMITS,
    }


def area_text(stats: dict) -> str:
    """The area answer, written in code so every number is exact."""
    lines = [f"Biking in {stats['area']}: {stats['crashes_per_mile_per_year']} crashes that hurt a cyclist per mile of "
             f"street per year since 2021"
             + (f", {stats['compared_with_citywide']} than the citywide {stats['citywide_crashes_per_mile_per_year']}." if stats["compared_with_citywide"] else ".")]
    lines.append(f"{stats['share_protected_lanes']:.0%} of its {stats['street_miles']:,} street miles have protected lanes or paths, "
                 f"and {stats['share_painted_lanes']:.0%} have painted lanes.")
    lines.append(RIDERS_CAVEAT)
    if stats["corridors_that_stand_out"]:
        lines.append("Corridors that stand out compared to similar streets: " + "; ".join(
            f"{c['street']} ({c['crashes']} crashes against about {c['similar_streets_expected']} expected)" for c in stats["corridors_that_stand_out"]) + ".")
    return " ".join(lines)


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
