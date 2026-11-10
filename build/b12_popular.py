"""Popular rides: a short hand-picked list, written to data/popular_rides.json so the page's Popular rides
section loads at once.

Each ride follows its own path as mapped in OpenStreetMap: a bicycle route relation where one exists, or the
ways named for the path. The route between the ride's ends runs on our street network, with any segment more
than 30 m from that OSM geometry made 40 times as costly, so it stays on the path. A ride is kept only if at
least 90% of its length lies within 30 m of the OSM path; a ride the network can't follow is left out rather
than shown on nearby streets.

Numbers, all from the data:
- length, minutes, and the share on protected lanes or off-street paths, from the street segments;
- crashes that hurt a cyclist since 2021 within 40 m of the ride, on the path itself or the streets along or
  beside it, with the 95% range (tools.range_bounds). Crashes on a path are often recorded on the street beside it,
  which is why the count includes those streets;
- for a ride mostly on streets (under half protected or off-street), the similar-streets comparison the routes
  use, shown as a badge only when the whole range sits above or below the expectation.

Run from the build/ folder after b08: ../.venv/bin/python b12_popular.py   (fetches OSM once into data/raw/)
"""
import json
import sys
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tools  # noqa: E402

OSM_CACHE = ROOT / "data" / "raw" / "popular_rides_osm.json"
NEAR_PATH_M = 30  # a segment within this of the OSM path counts as on it
BESIDE_M = 40  # crashes this close to the ride count as on it or the streets beside it
BIKE_MPH = 9.5  # the speed the street network assumes (build/b07_edges.py)
YEARS = float((tools.crashes.crash_date.max() - pd.Timestamp("2021-01-01")).days / 365.25)

# name, kind, From and To for "Ride here", the OSM source (relation ids, or way names in a box), and the points the
# ride passes, in order. A loop starts and ends at the same point.
RIDES = [
    ("Hudson River Greenway", "one way", "Battery Park", "George Washington Bridge",
     {"relations": [6729284]},  # the Manhattan Waterfront Greenway; the Hudson side is the part between these ends
     [(40.7045, -74.0175), (40.7570, -74.0060), (40.8505, -73.9478)]),
    ("Central Park loop", "loop", "Columbus Circle", "Harlem Meer",
     {"relations": [3005307]},
     [(40.7672, -73.9775), (40.7880, -73.9560), (40.7975, -73.9535), (40.7840, -73.9690), (40.7672, -73.9775)]),
    ("Prospect Park loop", "loop", "Grand Army Plaza, Brooklyn", "Park Circle, Brooklyn",
     {"relations": [3016815]},
     [(40.6703, -73.9703), (40.6655, -73.9762), (40.6522, -73.9718), (40.6600, -73.9628), (40.6703, -73.9703)]),
    ("Brooklyn Bridge Park Greenway", "one way", "Brooklyn Bridge Park", "Atlantic Avenue and Columbia Street",
     {"relations": [3017379]},
     [(40.7030, -73.9930), (40.6975, -73.9995), (40.6905, -74.0005)]),
    ("East River Greenway", "one way", "South Street Seaport", "East 34th Street Ferry Landing",
     {"relations": [6729284]},  # the same Manhattan Waterfront Greenway, its East River side
     [(40.7065, -74.0020), (40.7445, -73.9715)]),
    ("Shore Parkway Greenway, Bay Ridge", "one way", "Bay Ridge 69th Street Pier", "Verrazzano-Narrows Bridge",
     {"names": ["Shore Parkway Greenway", "Shore Road Greenway", "Shore Parkway Path"], "box": (40.595, -74.05, 40.645, -74.02)},
     [(40.6370, -74.0360), (40.6085, -74.0385)]),
    ("Ocean Parkway path", "one way", "Ocean Parkway and Church Avenue", "Brighton Beach",
     {"relations": [3015975]},
     [(40.6440, -73.9745), (40.6200, -73.9710), (40.5950, -73.9690), (40.5790, -73.9680)]),
    ("Williamsburg Bridge bike path", "one way", "Delancey Street and Clinton Street", "Williamsburg",
     {"relations": [3007381]},
     [(40.7172, -73.9850), (40.7110, -73.9625)]),
]
ON_PATH = {"protected", "path", "link"}
LIMITS = ["Police-reported crashes only, so this measures serious crash risk, not every incident.",
          "Crashes on an off-street path are often recorded on the street beside it, so the count includes crashes "
          f"within {BESIDE_M} m of the ride, on the path or the streets along or beside it.",
          "Crashes per mile, not per rider: a busy path has more people on it, which this doesn't adjust for.",
          "These are not personal odds."]


def fetch_osm() -> dict:
    """Every ride's OSM ways, with geometry and tags, cached in data/raw/."""
    if OSM_CACHE.exists():
        return json.loads(OSM_CACHE.read_text())
    out = {}
    for name, _, _, _, source, _ in RIDES:
        if "relations" in source:
            q = "".join(f"relation({r});way(r);" for r in source["relations"])
            q = f"[out:json][timeout:120];({q});out geom tags;"
        else:
            s_, w, n, e = source["box"]
            names = "|".join(source["names"])
            q = f'[out:json][timeout:120];way["name"~"^({names})$"]({s_},{w},{n},{e});out geom tags;'
        reply = httpx.post("https://overpass-api.de/api/interpreter", data={"data": q}, timeout=180,
                           headers={"User-Agent": "ave-a-to-b-build/1.0"})
        reply.raise_for_status()
        out[name] = [{"tags": el.get("tags", {}), "points": [(p["lat"], p["lon"]) for p in el["geometry"]]}
                     for el in reply.json()["elements"] if el.get("geometry")]
    OSM_CACHE.parent.mkdir(parents=True, exist_ok=True)
    OSM_CACHE.write_text(json.dumps(out))
    return out


def xy(lat, lon):
    """Meters on a flat local grid, good enough at this scale."""
    return np.column_stack([np.asarray(lat) * 111_000, np.asarray(lon) * 84_000])


def densify(lines, step=5.0) -> np.ndarray:
    pts = []
    for line in lines:
        a = xy(*zip(*line))
        for p, q in zip(a, a[1:]):
            k = max(1, int(np.hypot(*(q - p)) // step))
            pts.extend(p + (q - p) * t for t in np.linspace(0, 1, k, endpoint=False))
        pts.append(a[-1])
    return np.array(pts)


OFF_STREET = {"cycleway", "path", "footway", "pedestrian", "track", "bridleway", "steps"}


def separated(tags: dict) -> bool:
    """Off-street, or a lane separated from traffic, from the way's OSM tags."""
    if tags.get("highway") in OFF_STREET:
        return True
    if tags.get("motor_vehicle") == "no" or tags.get("access") == "no":  # a park drive closed to cars
        return True
    return any(tags.get(k) in ("track", "separate") for k in ("cycleway", "cycleway:both", "cycleway:left", "cycleway:right"))


def osm_route(ways: list[dict], points) -> list[tuple[float, float, bool]] | None:
    """The ride along its own OSM ways: the shortest path through them between the points, in order, as
    (lat, lon, the step into this point is separated). None when the ways have a gap between two points.
    Direction is ignored: it changes neither the length nor the crashes nearby."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra
    key, coords, rows, cols, lengths, sep_of = {}, [], [], [], [], {}
    for way in ways:
        sep = separated(way["tags"])
        ids = []
        for lat, lon in way["points"]:
            k = (round(lat, 7), round(lon, 7))
            if k not in key:
                key[k] = len(coords)
                coords.append(k)
            ids.append(key[k])
        for u, v in zip(ids, ids[1:]):
            d = float(np.hypot(*(xy([coords[u][0]], [coords[u][1]]) - xy([coords[v][0]], [coords[v][1]]))[0])) or 0.01
            rows += [u, v]; cols += [v, u]; lengths += [d, d]
            sep_of[(u, v)] = sep_of[(v, u)] = sep_of.get((u, v), False) or sep
    graph = coo_matrix((lengths, (rows, cols)), shape=(len(coords), len(coords))).tocsr()
    tree = cKDTree(xy(*zip(*coords)))
    stops = [int(tree.query(xy([a], [b]))[1][0]) for a, b in points]
    nodes = [stops[0]]
    for u, v in zip(stops, stops[1:]):
        dist, prev = dijkstra(graph, indices=u, return_predecessors=True)
        if not np.isfinite(dist[v]):
            return None
        leg = [v]
        while leg[-1] != u:
            leg.append(int(prev[leg[-1]]))
        nodes += leg[::-1][1:]
    return [(*coords[nodes[0]], True)] + [(*coords[v], sep_of[(u, v)]) for u, v in zip(nodes, nodes[1:])]


def label(n: int, expected: float) -> str | None:
    """The routes' test: a label only when the whole 95% range sits on one side of the expectation."""
    low, high = tools.range_bounds(n)
    if low > expected:
        return "more crashes than similar streets"
    if high < expected:
        return "fewer crashes than similar streets"
    return None


CRASH_TREE = cKDTree(xy(tools.crashes.latitude.to_numpy(), tools.crashes.longitude.to_numpy()))
EDGE_LINES = tools._line(tools.edges)
EDGE_POINTS = np.array([p for line in EDGE_LINES for p in line])
EDGE_OWNER = np.repeat(np.arange(len(EDGE_LINES)), [len(line) for line in EDGE_LINES])


def thumbnail(line, w=120, h=72) -> str:
    """The ride as a small SVG path for its card: its shape, north up, scaled to fit."""
    pts = np.array(line)
    lat0, lat1, lon0, lon1 = pts[:, 0].min(), pts[:, 0].max(), pts[:, 1].min(), pts[:, 1].max()
    sx, sy = (lon1 - lon0) * 0.757 or 1e-6, (lat1 - lat0) or 1e-6
    scale = min((w - 8) / sx, (h - 8) / sy)
    ox, oy = (w - sx * scale) / 2, (h - sy * scale) / 2
    return "M" + " L".join(f"{ox + (lon - lon0) * 0.757 * scale:.1f} {oy + (lat1 - lat) * scale:.1f}" for lat, lon in line[::2] + [line[-1]])


def describe(name, kind, start, end, source, points, osm) -> dict | None:
    steps = osm_route(osm[name], points)
    if not steps:
        print(f"  left out {name}: its OSM path has a gap between the ride's ends")
        return None
    line = [[lat, lon] for lat, lon, _ in steps]
    a = xy(*zip(*line))
    seg = np.hypot(*np.diff(a, axis=0).T)
    meters = float(seg.sum())
    sep_m = float(seg[[s for _, _, s in steps[1:]]].sum())
    miles = meters / 1609.344
    # Crashes within BESIDE_M of the ride: the path and the streets along or beside it
    ride_pts = densify([line])
    near = set()
    for hits in CRASH_TREE.query_ball_point(ride_pts, BESIDE_M):
        near.update(hits)
    beside = tools.crashes.iloc[sorted(near)]
    n = int(len(beside))
    low, high = tools.range_bounds(n)
    on_line = miles
    mostly_streets = sep_m / meters < 0.5
    ride = {
        "name": name, "kind": kind, "start": start, "end": end,
        "miles": round(miles, 1), "minutes": round(miles / BIKE_MPH * 60),
        "protected_share": round(sep_m / meters, 2),
        "crashes_beside": n, "range_low": round(low, 1), "range_high": round(high, 1),
        "crashes_beside_per_mile_year": round(n / (on_line * YEARS), 1),
        "beside_m": BESIDE_M, "years": round(YEARS, 1),
        "mostly_streets": mostly_streets, "label": None,
        "collision_ids": beside.sort_values("crash_date", ascending=False).collision_id.head(5).tolist(),
        "line": [line], "thumb": thumbnail(line),
        "source": f"OpenStreetMap: {', '.join(f'relation {r}' for r in source['relations']) if 'relations' in source else 'ways named ' + ', '.join(source['names'])}",
        "limits": LIMITS,
    }
    if mostly_streets:
        # The routes' similar-streets test, on our street segments within 30 m of the ride
        d, _ = cKDTree(ride_pts).query(xy(EDGE_POINTS[:, 0], EDGE_POINTS[:, 1]), distance_upper_bound=1000)
        far = pd.Series(np.minimum(d, 1000)).groupby(EDGE_OWNER).max().to_numpy()
        unique = tools.edges[far <= NEAR_PATH_M]
        crashes = int(unique.crashes.sum())
        expected = float((unique.crash_class_rate * unique.mile_years).sum())
        ride.update(label=label(crashes, expected), segment_crashes=crashes, similar_streets_expected=round(expected, 1),
                    edge_ids=unique.edge_id.tolist())
    return ride


def main() -> None:
    osm = fetch_osm()
    rides = [r for r in (describe(*spec, osm) for spec in RIDES) if r]
    out = ROOT / "data" / "popular_rides.json"
    out.write_text(json.dumps(rides, separators=(",", ":")))
    for r in rides:
        print(f"{r['name']}: {r['miles']} mi, {r['minutes']} min, {r['protected_share']:.0%} protected or off-street, "
              f"{r['crashes_beside']} crashes within {BESIDE_M} m "
              f"({r['range_low']} to {r['range_high']}), label {r['label']}")
    print(f"{out.name}: {len(rides)} rides, {out.stat().st_size / 1e3:.0f} KB")


if __name__ == "__main__":
    main()
