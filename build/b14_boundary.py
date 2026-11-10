"""Writes data/nyc_boundary.json, the city line for "Pick on map": two outlines from NYC Open Data's
Borough Boundaries, simplified so the app can test a point with plain numpy.

  city: the five boroughs with their water areas (the state line runs down the middle of the Hudson)
  land: the five boroughs clipped to the shoreline, widened by about 60 m so a pier or a click on the
        water's edge still counts as land

Run from the build/ folder: uv run --group build python b14_boundary.py
"""
import json

import requests
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from common import DATA

SOURCES = {"city": "wh2p-dxnf", "land": "gthc-hcne"}  # Borough Boundaries, with and without water areas
SIMPLIFY_DEGREES = 0.0002  # about 20 m
LAND_MARGIN_DEGREES = 0.0006  # about 60 m


def outline(dataset: str):
    response = requests.get(f"https://data.cityofnewyork.us/resource/{dataset}.geojson", timeout=120)
    response.raise_for_status()
    return unary_union([shape(f["geometry"]) for f in response.json()["features"]])


def rings(geometry) -> list[list[list[float]]]:
    """Every ring, outer and inner, as [lon, lat] points. The app counts crossings over all of them
    (the even-odd rule), so holes need no special case."""
    out = []
    for polygon in mapping(geometry)["coordinates"] if geometry.geom_type == "MultiPolygon" else [mapping(geometry)["coordinates"]]:
        for ring in polygon:
            out.append([[round(x, 5), round(y, 5)] for x, y in ring])
    return out


def main() -> None:
    city = outline(SOURCES["city"]).simplify(SIMPLIFY_DEGREES)
    land = outline(SOURCES["land"]).buffer(LAND_MARGIN_DEGREES).simplify(SIMPLIFY_DEGREES)
    data = {"source": "NYC Open Data, Borough Boundaries (wh2p-dxnf with water areas, gthc-hcne clipped to the shoreline)",
            "city": rings(city), "land": rings(land)}
    path = DATA / "nyc_boundary.json"
    path.write_text(json.dumps(data, separators=(",", ":")))
    print(f"nyc_boundary.json: {sum(map(len, data['city']))} + {sum(map(len, data['land']))} points, {path.stat().st_size / 1e3:.0f} KB")


if __name__ == "__main__":
    main()
