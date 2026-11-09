"""Build the street table: one row per OSM street segment, with lanes, SIPs, truck routes, and
snapped crashes and 311 complaints.

A two-way street is two directed OSM edges, but one physical street, so crashes and lanes attach
to an undirected segment (edge_id). graph_arcs.parquet keeps direction for routing.

Writes: edges_base, graph_arcs, graph_nodes, crash_locations, blocked_edges, edge_lanes, edge_sips.
"""

import geopandas as gpd
import h3
import numpy as np
import osmnx as ox
import pandas as pd
import shapely

from common import DATA, RAW, report
from streets import street_key

METRIC = 32618  # UTM 18N, so distances are in meters
SNAP_M = 25
ANGLE_DEG = 30  # a lane only matches a street running the same way
H3_RES = 9
BIKE_MPH = 9.5  # flat-ground average; OSM has no grades or signal delays here
MILE_M = 1609.34

PLACES = {"Manhattan": "Manhattan, New York, USA", "Brooklyn": "Brooklyn, New York, USA",
          "Queens": "Queens, New York, USA", "Bronx": "The Bronx, New York, USA",
          "Staten Island": "Staten Island, New York, USA"}
ROAD_CLASS = {"trunk": "arterial", "trunk_link": "arterial", "primary": "arterial", "primary_link": "arterial",
              "secondary": "collector", "secondary_link": "collector", "tertiary": "collector", "tertiary_link": "collector",
              "residential": "local", "living_street": "local", "unclassified": "local", "service": "local",
              "cycleway": "path", "path": "path", "footway": "path", "pedestrian": "path", "track": "path", "steps": "path"}
LANE_TYPE = {"Protected": "protected", "Conventional": "painted", "Conventional Buffered": "painted", "Curbside": "painted",
             "Curbside Buffered": "painted", "Shared": "shared", "Signed Route": "shared", "Wide Parking Lane": "shared",
             "Sidewalk": "path", "Boardwalk": "path", "Unpaved": "path", "Ped Plaza": "path", "Link": "link"}
# Which lane wins when a segment has more than one. Higher is more separated from traffic.
LANE_RANK = {"none": 0, "shared": 1, "link": 1, "painted": 2, "protected": 3, "path": 3}


def first(value):
    return value[0] if isinstance(value, list) else value


def load_edges() -> tuple[gpd.GeoDataFrame, pd.DataFrame, pd.DataFrame]:
    graph = ox.load_graphml(RAW / "nyc_bike.graphml")
    nodes, arcs = ox.graph_to_gdfs(graph)
    arcs = arcs.reset_index()
    arcs["edge_id"] = [f"{min(u, v)}-{max(u, v)}-{k}" for u, v, k in zip(arcs.u, arcs.v, arcs.key)]
    arcs["highway"] = arcs.highway.map(lambda h: str(first(h)))
    arcs["name"] = arcs.name.map(lambda n: first(n) if isinstance(n, (list, str)) else None)
    arcs["road_class"] = arcs.highway.map(ROAD_CLASS).fillna("local")
    arcs["oneway"] = arcs.oneway.astype(str) == "True"

    segments = arcs.sort_values("length").drop_duplicates("edge_id")
    segments = segments[["edge_id", "u", "v", "name", "highway", "road_class", "oneway", "length", "geometry"]]
    segments = segments.rename(columns={"length": "length_m"}).set_crs(4326, allow_override=True)
    segments["length_mi"] = segments.length_m / MILE_M
    segments["travel_min"] = segments.length_mi / BIKE_MPH * 60

    node_table = nodes.reset_index()[["osmid", "y", "x"]].rename(columns={"osmid": "node", "y": "lat", "x": "lon"})
    arc_table = pd.DataFrame(arcs[["u", "v", "edge_id"]])
    print(f"{len(arcs):,} directed arcs -> {len(segments):,} street segments, {segments.length_mi.sum():,.0f} miles")
    return gpd.GeoDataFrame(segments, geometry="geometry", crs=4326), arc_table, node_table


def add_borough(segments: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Midpoint's borough. Bridges sit over water, so take the nearest borough."""
    path = RAW / "boroughs.parquet"
    if path.exists():
        boroughs = gpd.read_parquet(path)
    else:
        boroughs = ox.geocode_to_gdf(list(PLACES.values()))[["geometry"]].assign(borough=list(PLACES))
        boroughs.to_parquet(path)
    mids = gpd.GeoDataFrame(segments[["edge_id"]], geometry=segments.to_crs(METRIC).interpolate(0.5, normalized=True), crs=METRIC)
    joined = gpd.sjoin_nearest(mids, boroughs.to_crs(METRIC)).drop_duplicates("edge_id")
    return segments.merge(joined[["edge_id", "borough"]], on="edge_id", how="left")


def bearing_at(lines: np.ndarray, distances: np.ndarray, step: float = 3.0) -> np.ndarray:
    """Direction of each line near a point, in degrees from 0 to 180 (lanes have no direction here)."""
    a = shapely.line_interpolate_point(lines, np.maximum(distances - step, 0))
    b = shapely.line_interpolate_point(lines, distances + step)
    return np.degrees(np.arctan2(shapely.get_y(b) - shapely.get_y(a), shapely.get_x(b) - shapely.get_x(a))) % 180


def match_lines(segments: gpd.GeoDataFrame, lines: gpd.GeoDataFrame) -> pd.DataFrame:
    """Pairs of (segment, line) where the segment's midpoint is within SNAP_M of the line and they run
    the same way. The angle check stops a cross street from picking up the avenue's lane."""
    lines = lines.to_crs(METRIC).explode(index_parts=False).reset_index(drop=True)
    lines["line_row"] = lines.index
    metric = segments.to_crs(METRIC)
    length = metric.geometry.length.to_numpy()
    mids = gpd.GeoDataFrame({"edge_id": segments.edge_id.to_numpy(), "seg_row": np.arange(len(segments))},
                            geometry=metric.geometry.interpolate(0.5, normalized=True).to_numpy(), crs=METRIC)
    pairs = gpd.sjoin(mids, gpd.GeoDataFrame({"line_row": lines.line_row}, geometry=lines.buffer(SNAP_M), crs=METRIC))
    seg_geoms = metric.geometry.to_numpy()[pairs.seg_row]
    line_geoms = lines.geometry.to_numpy()[pairs.line_row_right if "line_row_right" in pairs else pairs.line_row]
    points = pairs.geometry.to_numpy()
    seg_angle = bearing_at(seg_geoms, length[pairs.seg_row] / 2)
    line_angle = bearing_at(line_geoms, shapely.line_locate_point(line_geoms, points))
    diff = np.abs(seg_angle - line_angle)
    diff = np.minimum(diff, 180 - diff)
    keep = pairs.assign(angle=diff, dist=shapely.distance(points, line_geoms))
    keep = keep[keep.angle <= ANGLE_DEG]
    return keep.merge(lines.drop(columns="geometry"), left_on=keep.columns[keep.columns.str.startswith("line_row")][0], right_on="line_row")


def attach_lanes(segments: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    routes = pd.read_parquet(DATA / "bike_routes.parquet")
    routes = gpd.GeoDataFrame(routes, geometry=gpd.GeoSeries.from_wkt(routes.wkt), crs=4326).drop(columns="wkt")
    ranks = lambda col: routes[col].map(LANE_TYPE).fillna("none")
    routes["lane_ft"], routes["lane_tf"] = ranks("ft_facilit"), ranks("tf_facilit")
    routes["lane_type"] = [a if LANE_RANK[a] >= LANE_RANK[b] else b for a, b in zip(routes.lane_ft, routes.lane_tf)]
    pairs = match_lines(segments, routes)
    history = pairs[["edge_id", "bikeid", "status", "lane_type", "instdate", "ret_date", "street", "dist"]].drop_duplicates()
    print(f"lane matches: {len(history):,} pairs on {history.edge_id.nunique():,} segments")

    current = history[history.status == "Current"].assign(rank=lambda d: d.lane_type.map(LANE_RANK))
    best = current.sort_values(["rank", "dist"], ascending=[False, True]).drop_duplicates("edge_id")
    segments = segments.merge(best[["edge_id", "lane_type", "instdate"]].rename(columns={"instdate": "lane_installed"}),
                              on="edge_id", how="left")
    # Off-street paths the city doesn't list as routes (park paths, for example) still count as paths.
    off_street = segments.lane_type.isna() & (segments.road_class == "path")
    segments.loc[off_street, "lane_type"] = "path"
    segments["lane_type"] = segments.lane_type.fillna("none")
    print(segments.groupby("lane_type").length_mi.sum().round(0).to_string())
    return segments, history


def attach_flags(segments: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    trucks = pd.read_parquet(DATA / "truck_routes.parquet")
    trucks = gpd.GeoDataFrame(trucks, geometry=gpd.GeoSeries.from_wkt(trucks.wkt), crs=4326).drop(columns="wkt")
    on_truck = match_lines(segments, trucks)
    segments["on_truck_route"] = segments.edge_id.isin(on_truck.edge_id)
    print(f"truck: {segments.on_truck_route.sum():,} segments, {segments[segments.on_truck_route].length_mi.sum():,.0f} miles")

    sips = pd.read_parquet(DATA / "sip.parquet")
    sips = gpd.GeoDataFrame(sips, geometry=gpd.GeoSeries.from_wkt(sips.wkt), crs=4326).drop(columns="wkt")
    # end_date is when the work finished. A few have none, so fall back to mid-year.
    sips["sip_date"] = sips.end_date.fillna(pd.to_datetime(sips.sip_year.astype("Int64").astype(str) + "-07-01", errors="coerce"))
    pairs = match_lines(segments, sips)
    edge_sips = pairs[["edge_id", "sip_id", "pjct_name", "sip_year", "sip_date", "dist"]].drop_duplicates(["edge_id", "sip_id"])
    latest = edge_sips.sort_values("sip_date").drop_duplicates("edge_id", keep="last")
    segments = segments.merge(latest[["edge_id", "sip_id", "sip_date"]], on="edge_id", how="left")
    print(f"SIP: {edge_sips.sip_id.nunique()} of {len(sips)} projects on {edge_sips.edge_id.nunique():,} segments")
    return segments, edge_sips


def snap_points(points: pd.DataFrame, segments: gpd.GeoDataFrame, lat: str, lon: str, label: str) -> pd.DataFrame:
    """Nearest segment within SNAP_M, plus how far along the segment the point is from a corner."""
    gdf = gpd.GeoDataFrame(points, geometry=gpd.points_from_xy(points[lon], points[lat]), crs=4326).to_crs(METRIC)
    metric = segments[["edge_id", "geometry"]].to_crs(METRIC)
    joined = gpd.sjoin_nearest(gdf, metric, max_distance=SNAP_M, distance_col="snap_m").drop_duplicates(subset=points.columns[0])
    lines = metric.set_index("edge_id").geometry.reindex(joined.edge_id).to_numpy()
    along = shapely.line_locate_point(lines, joined.geometry.to_numpy())
    joined["near_corner"] = np.minimum(along, shapely.length(lines) - along) < 15
    print(f"{label}: {len(joined):,} of {len(points):,} snapped ({len(joined) / len(points) * 100:.1f}%), "
          f"median {joined.snap_m.median():.1f} m, {joined.near_corner.mean() * 100:.0f}% within 15 m of a corner")
    return pd.DataFrame(joined.drop(columns=["geometry", "index_right"]))


def snap_crashes(crashes: pd.DataFrame, segments: gpd.GeoDataFrame) -> pd.DataFrame:
    """Police place most crashes exactly on an intersection, so several streets tie for nearest.
    Among segments within SNAP_M, prefer the street the report names, then the nearest."""
    gdf = gpd.GeoDataFrame(crashes, geometry=gpd.points_from_xy(crashes.longitude, crashes.latitude), crs=4326).to_crs(METRIC)
    metric = segments[["edge_id", "name", "geometry"]].to_crs(METRIC)
    pairs = gdf.sjoin(metric, predicate="dwithin", distance=SNAP_M)
    lines = metric.geometry.to_numpy()[pairs.index_right.to_numpy()]
    pairs["snap_m"] = shapely.distance(pairs.geometry.to_numpy(), lines)
    along = shapely.line_locate_point(lines, pairs.geometry.to_numpy())
    pairs["near_corner"] = np.minimum(along, shapely.length(lines) - along) < 15
    keys = pairs.name.map(street_key)
    pairs["name_match"] = np.where(keys == pairs.on_street_name.map(street_key), 2, np.where(keys == pairs.off_street_name.map(street_key), 1, 0))
    pairs.loc[keys == "", "name_match"] = 0
    best = pairs.sort_values(["collision_id", "name_match", "snap_m"], ascending=[True, False, True]).drop_duplicates("collision_id")
    print(f"crashes: {len(best):,} of {len(crashes):,} snapped ({len(best) / len(crashes) * 100:.1f}%), "
          f"{(best.name_match == 2).mean() * 100:.0f}% on the named street, {(best.name_match == 1).mean() * 100:.0f}% on the cross street, "
          f"{(best.name_match == 0).mean() * 100:.0f}% by distance only; {best.near_corner.mean() * 100:.0f}% within 15 m of a corner")
    return pd.DataFrame(best.drop(columns=["geometry", "index_right"]))


def main() -> None:
    segments, arcs, nodes = load_edges()
    segments = add_borough(segments)
    segments, lane_history = attach_lanes(segments)
    segments, edge_sips = attach_flags(segments)

    mid = segments.geometry.interpolate(0.5, normalized=True)
    segments["lat"], segments["lon"] = mid.y.round(6), mid.x.round(6)
    segments["h3"] = [h3.latlng_to_cell(a, b, H3_RES) for a, b in zip(segments.lat, segments.lon)]

    crashes = pd.read_parquet(DATA / "crashes.parquet", columns=["collision_id", "latitude", "longitude", "has_coords", "severe",
                                                                   "on_street_name", "off_street_name"])
    located = crashes[crashes.has_coords].drop(columns="has_coords")
    snapped = snap_crashes(located, segments)
    snapped["h3"] = [h3.latlng_to_cell(a, b, H3_RES) for a, b in zip(snapped.latitude, snapped.longitude)]

    blocked = pd.read_parquet(DATA / "blocked_lanes.parquet")
    blocked_snapped = snap_points(blocked, segments, "latitude", "longitude", "311")
    blocked_snapped["h3"] = [h3.latlng_to_cell(a, b, H3_RES) for a, b in zip(blocked_snapped.latitude, blocked_snapped.longitude)]

    counts = snapped.groupby("edge_id").agg(crashes=("collision_id", "count"), severe_crashes=("severe", "sum"))
    segments = segments.merge(counts, on="edge_id", how="left")
    segments = segments.merge(blocked_snapped.groupby("edge_id").size().rename("blocked_complaints"), on="edge_id", how="left")
    for col in ["crashes", "severe_crashes", "blocked_complaints"]:
        segments[col] = segments[col].fillna(0).astype(int)

    segments["wkt"] = shapely.to_wkt(segments.geometry.to_numpy(), rounding_precision=5)
    base = pd.DataFrame(segments.drop(columns="geometry"))
    report("edges_base", base)
    report("graph_arcs", arcs)
    report("graph_nodes", nodes)
    report("crash_locations", snapped[["collision_id", "edge_id", "snap_m", "near_corner", "name_match", "h3"]])
    report("blocked_edges", blocked_snapped[["unique_key", "created_date", "edge_id", "h3"]])
    report("edge_lanes", lane_history)
    report("edge_sips", edge_sips)


if __name__ == "__main__":
    main()
