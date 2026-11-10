"""Popular rides: every number on a card comes from the data, each ride follows its OSM path, and no card ranks
safety."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))

import b12_popular  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402

RIDES = json.loads((ROOT / "data" / "popular_rides.json").read_text())


def test_the_rides_are_the_hand_picked_ones_in_order():
    picked = [r[0] for r in b12_popular.RIDES]
    names = [r["name"] for r in RIDES]
    assert names == [n for n in picked if n in names] and len(names) >= 6
    assert TestClient(app.app).get("/popular").json() == RIDES


@pytest.mark.parametrize("ride", RIDES, ids=lambda r: r["name"])
def test_the_numbers_come_from_the_data(ride):
    line = ride["line"][0]
    a = b12_popular.xy(*zip(*line))
    meters = float(np.hypot(*np.diff(a, axis=0).T).sum())
    assert ride["miles"] == round(meters / 1609.344, 1)
    assert ride["minutes"] == round(meters / 1609.344 / b12_popular.BIKE_MPH * 60)
    # Crashes within 40 m of the drawn line, recounted from the crash table
    tree = cKDTree(b12_popular.xy(app.tools.crashes.latitude.to_numpy(), app.tools.crashes.longitude.to_numpy()))
    near = set()
    for hits in tree.query_ball_point(b12_popular.densify([line]), ride["beside_m"]):
        near.update(hits)
    assert ride["crashes_beside"] == len(near)
    low, high = app.tools.range_bounds(len(near))
    assert (ride["range_low"], ride["range_high"]) == (round(low, 1), round(high, 1))
    assert 0 <= ride["protected_share"] <= 1 and ride["minutes"] > 0


@pytest.mark.parametrize("ride", RIDES, ids=lambda r: r["name"])
def test_each_ride_follows_its_own_osm_path(ride):
    osm = b12_popular.fetch_osm() if b12_popular.OSM_CACHE.exists() else None
    if osm is None:
        pytest.skip("the OSM download is a build input, not in the repo")
    path = cKDTree(b12_popular.densify([w["points"] for w in osm[ride["name"]]]))
    d, _ = path.query(b12_popular.densify(ride["line"]))
    assert d.max() < 1.0  # every point of the drawn ride is on its OSM ways


def test_badges_only_for_rides_on_streets_and_only_when_the_range_supports_them():
    for r in RIDES:
        if not r["mostly_streets"]:
            assert r["label"] is None, r["name"]  # off-street paths show plain facts, no comparison
        elif r["label"]:
            assert r["label"] == b12_popular.label(r["segment_crashes"], r["similar_streets_expected"])


def test_no_card_ranks_safety():
    page = (ROOT / "index.html").read_text()
    text = json.dumps(RIDES).lower() + page.split("--- Popular rides: precomputed")[1].split("// Header links:")[0].lower()
    for banned in ["safest", "tourist friendly", "tourist-friendly", "safer", "safe ride"]:
        assert banned not in text, banned
    for r in RIDES:  # the caveat that path crashes are often recorded on the street beside it
        assert any("recorded on the street beside it" in t for t in r["limits"])


def test_ride_here_names_resolve_offline():
    for r in RIDES:
        for place in (r["start"], r["end"]):
            assert app.tools._place(place) or app.tools._intersection(place), place


def test_loops_are_closed_and_counterclockwise():
    loops = [r for r in RIDES if r["kind"] == "loop"]
    assert {r["name"] for r in loops} == {"Central Park loop", "Prospect Park loop"}
    for r in loops:
        line = r["line"][0]
        assert line[0] == line[-1], r["name"]
        assert b12_popular.signed_area(line) > 0 and r["direction"] == "counterclockwise", r["name"]
