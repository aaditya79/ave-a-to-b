"""Simplified citywide layers for the map's explore mode, written to data/explore.json.

Layers: corridors that stand out from similar streets (with their numbers), protected and painted lanes,
Street Improvement Projects, and truck routes. Each street's segments are merged into lines, simplified to
about 5 m, and rounded to 5 decimals, so the page loads one small file.

Run from the build/ folder after b09: ../.venv/bin/python b10_explore.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
from shapely import wkt as shapely_wkt

DATA = Path(__file__).resolve().parent.parent / "data"
TOLERANCE = 0.00005  # degrees, about 5 m
TOP_CORRIDORS = 60  # the ones that stand out most; more turns the map into a red web


def lines(geoms) -> list[list[list[float]]]:
    """Merge touching segments, simplify, and return [[lat, lon], ...] per line."""
    merged = shapely.line_merge(shapely.union_all(list(geoms)))
    parts = getattr(merged, "geoms", [merged])
    out = []
    for part in parts:
        coords = np.asarray(part.simplify(TOLERANCE).coords)
        if len(coords) >= 2:
            out.append([[round(lat, 5), round(lon, 5)] for lon, lat in coords])
    return out


def main() -> None:
    edges = pd.read_parquet(DATA / "edges.parquet")
    # One row per street segment, not per direction
    pair = np.where(edges.u < edges.v, edges.u.astype(str) + "-" + edges.v.astype(str), edges.v.astype(str) + "-" + edges.u.astype(str))
    edges = edges.assign(pair=pair).drop_duplicates("pair")
    edges["geom"] = edges.wkt.map(shapely_wkt.loads)
    edges["street"] = edges["name"].fillna("unnamed")

    layers = {}
    layers["lanes"] = {kind: [line for _, g in edges[edges.lane_type.isin(kinds)].groupby("street") for line in lines(g.geom)]
                       for kind, kinds in {"protected": ["protected", "path"], "painted": ["painted"]}.items()}
    layers["trucks"] = [line for _, g in edges[edges.on_truck_route.astype(bool)].groupby("street") for line in lines(g.geom)]

    sips = pd.read_parquet(DATA / "sip.parquet").set_index("sip_id")
    projects = []
    for sip_id, g in edges[edges.sip_id.notna()].groupby("sip_id"):
        info = sips.loc[sip_id] if sip_id in sips.index else None
        projects.append({"name": str(info.pjct_name).title() if info is not None else "Street Improvement Project",
                         "year": int(info.sip_year) if info is not None and pd.notna(info.sip_year) else None,
                         "lines": lines(g.geom)})
    layers["projects"] = projects

    # Corridors: a street name within a borough whose crash range (tools.range_bounds) sits above similar streets
    named = edges[edges["name"].notna()].assign(expected=lambda d: d.crash_class_rate * d.mile_years)
    corridors = named.groupby(["name", "borough"]).agg(crashes=("crashes", "sum"), severe=("severe_crashes", "sum"),
                                                       expected=("expected", "sum"), miles=("length_mi", "sum"))
    corridors = corridors[tools.range_bounds(corridors.crashes.to_numpy())[0] > corridors.expected.to_numpy()]
    corridors = corridors.assign(excess=corridors.crashes - corridors.expected).sort_values("excess", ascending=False).head(TOP_CORRIDORS)
    layers["corridors"] = []
    for (name, borough), r in corridors.iterrows():
        g = named[(named["name"] == name) & (named.borough == borough)]
        layers["corridors"].append({
            "street": name, "borough": borough, "crashes": int(r.crashes), "severe": int(r.severe),
            "range_low": round(tools.range_bounds(r.crashes)[0], 1), "range_high": round(tools.range_bounds(r.crashes)[1], 1),
            "similar_streets_expected": round(float(r.expected), 1), "miles": round(float(r.miles), 1),
            "lines": lines(g.geom)})

    out = DATA / "explore.json"
    out.write_text(json.dumps(layers, separators=(",", ":")))
    counts = {"protected lines": len(layers["lanes"]["protected"]), "painted lines": len(layers["lanes"]["painted"]),
              "truck lines": len(layers["trucks"]), "projects": len(projects), "corridors": len(layers["corridors"])}
    print(counts, f"{out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
