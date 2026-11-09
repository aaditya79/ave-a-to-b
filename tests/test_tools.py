"""Unit tests on known examples. Counts are checked against the Parquet files, not against the tools' own math.
Places are given as lat,lon so these run offline."""

import json

import numpy as np
import pandas as pd
import pytest

import tools

COLUMBIA, UNION_SQUARE = "40.8075,-73.9626", "40.7359,-73.9911"
PROSPECT, DUMBO = "40.6602,-73.9690", "40.7033,-73.9881"
ST_GEORGE = "40.6437,-74.0776"


@pytest.fixture(scope="module")
def trip():
    return tools.route_options(COLUMBIA, UNION_SQUARE)


def first_route(trip):
    return next(iter(trip["routes"].values()))


def test_count_range():
    assert tools.count_range(25) == {"n": 25, "low": 15.0, "high": 35.0}
    assert tools.count_range(1)["low"] == 0.0  # floored
    assert tools.count_range(0) == {"n": 0, "low": 0.0, "high": 0.0}


def test_geocode_accepts_lat_lon_and_rejects_far_away():
    assert tools.geocode(COLUMBIA)["lat"] == 40.8075
    assert "error" in tools.geocode("34.05,-118.24")


def test_three_routes_trade_time_for_risk(trip):
    routes = list(trip["routes"].values())
    assert len(routes) == 3 and "fastest" in trip["routes"] and "balanced" in trip["routes"]
    fastest, balanced, low = routes
    assert fastest["minutes"] <= balanced["minutes"] <= low["minutes"]
    assert fastest["relative_risk_vs_fastest"]["assumed"] == 1.0
    assert low["relative_risk_vs_fastest"]["assumed"] <= balanced["relative_risk_vs_fastest"]["assumed"] <= 1.0
    assert 4 < fastest["miles"] < 8  # about 6 miles down the west side


def test_every_trip_gets_one_of_three_labels(trip):
    assert trip["safety_label"] in tools.LABEL_NOTES
    assert list(trip["routes"]) == ["fastest", "balanced", "lowest_risk"]
    if trip["safety_label"] == "depends_on_assumption":
        assert trip["recommended_route"] is None
    else:
        assert trip["recommended_route"] in trip["routes"]
    for route in trip["routes"].values():
        assert set(route["relative_risk_vs_fastest"]) == {"half", "assumed", "double"}


def test_overlapping_ranges_are_about_equally_safe_and_faster_wins():
    slow, quick = [0, 1, 2], [3]  # fake routes, as edge positions
    near = {"half": 1.0, "assumed": 1.0, "double": 1.0}
    with patched(risk={0: 0.30, 1: 0.30, 2: 0.30, 3: 1.0}, counts={0: 10, 1: 10, 2: 10, 3: 30}, minutes={0: 5, 1: 5, 2: 5, 3: 3}):
        verdict = tools._verdict([slow, quick])  # 0.9 vs 1.0 with wide ranges: a tie
    assert verdict["label"] == "about_equally_safe" and verdict["recommended"] == 1


def test_clear_win_everywhere_is_safest():
    with patched(risk={0: 1.0, 1: 5.0}, counts={0: 400, 1: 400}, minutes={0: 5, 1: 3}):
        verdict = tools._verdict([[0], [1]])
    assert verdict["label"] == "safest" and verdict["recommended"] == 0


def test_clear_reversal_depends_on_assumption():
    risk = {"half": {0: 1.0, 1: 5.0}, "assumed": {0: 1.0, 1: 5.0}, "double": {0: 5.0, 1: 1.0}}
    with patched(risk=risk, counts={0: 400, 1: 400}, minutes={0: 5, 1: 3}):
        verdict = tools._verdict([[0], [1]])
    assert verdict["label"] == "depends_on_assumption" and verdict["recommended"] is None


class patched:
    """Swap the routing arrays for tiny fakes. risk is {position: value}, or {setting: {position: value}}."""
    def __init__(self, risk, counts, minutes):
        size = 5
        def array(values):
            out = np.zeros(size)
            for k, v in values.items():
                out[k] = v
            return out
        by_setting = risk if "half" in risk else {s: risk for s in tools.SETTINGS}
        self.new = {"RISK": {s: array(v) for s, v in by_setting.items()}, "EXPECTED_COUNT": array(counts), "MINUTES": array(minutes)}

    def __enter__(self):
        self.old = {k: getattr(tools, k) for k in self.new}
        for k, v in self.new.items():
            setattr(tools, k, v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(tools, k, v)


def test_route_miles_match_its_segments(trip):
    route = trip["routes"]["fastest"]
    ids = tools.resolve(route["route_ref"])
    assert abs(tools.edges.loc[ids].length_mi.sum() - route["miles"]) < 0.01
    corridor_ids = [e for c in route["corridors"] for e in tools.resolve(c["corridor_ref"])]
    assert corridor_ids == ids  # corridors cover the route exactly once, in order
    assert len(route["corridors"]) <= tools.MAX_CORRIDORS


def test_staten_island_mentions_the_ferry():
    result = tools.route_options(ST_GEORGE, UNION_SQUARE)
    assert result["no_route"] is True and "Staten Island Ferry carries bikes" in result["note"]


def test_routes_inside_staten_island_work():
    assert "routes" in tools.route_options(ST_GEORGE, "40.5795,-74.1502")


def test_uncovered_route_is_lower_confidence():
    trip = tools.route_options(ST_GEORGE, "40.5795,-74.1502")
    route = first_route(trip)
    assert route["lower_confidence"] is True and route["share_of_miles_without_citibike_coverage"] == 1.0


def test_crash_summary_matches_parquet():
    edge = pd.read_parquet("data/edges.parquet", columns=["edge_id", "crashes", "severe_crashes"]).nlargest(1, "crashes").iloc[0]
    result = tools.crash_summary([edge.edge_id])
    located = pd.read_parquet("data/crash_locations.parquet")
    assert result["all_crashes"]["n"] == edge.crashes == (located.edge_id == edge.edge_id).sum()
    assert result["severe_matching_filters"]["n"] == edge.severe_crashes
    assert len(result["collision_ids"]) == min(edge.crashes, tools.MAX_IDS)
    assert set(result["collision_ids"]) <= set(located[located.edge_id == edge.edge_id].collision_id)


def test_crash_filters(trip):
    ref = trip["routes"]["fastest"]["route_ref"]
    everything = tools.crash_summary(ref)["crashes_matching_filters"]["n"]
    pieces = sum(tools.crash_summary(ref, {"rider_type": t})["crashes_matching_filters"]["n"] for t in ("pedal", "e-bike"))
    assert pieces == everything
    assert tools.crash_summary(ref, {"severe_only": True})["crashes_matching_filters"]["n"] < everything
    assert "error" in tools.crash_summary(ref, {"color": "red"})


def test_dangerous_needs_the_whole_range_above_average():
    edges = tools.edges
    low = edges[(edges.crashes == 2)].iloc[0]
    here = tools.crashes[tools.crashes.edge_id == low.edge_id]
    spot = tools._hotspots(edges.loc[[low.edge_id]], here, here)[0]
    assert spot["crashes"]["low"] == 0.0 and spot["above_average"] is False


def test_infrastructure_shows_sip_name_and_year_only():
    with_sip = tools.sips.iloc[0].edge_id
    sip = tools.infrastructure_summary([with_sip])["sips"][0]
    assert set(sip) == {"sip_id", "project", "year"}


def test_exposure_reports_calibration_spread(trip):
    result = tools.exposure_summary(trip["routes"]["fastest"]["route_ref"])
    calibration = result["counter_calibration"]
    assert calibration["lowest_ratio"] == 3.7 and calibration["highest_ratio"] == 45.9
    assert calibration["median_ratio"] == 12.43


def test_similar_streets_adds_up(trip):
    ref = trip["routes"]["fastest"]["route_ref"]
    assert sum(c["crashes"]["n"] for c in tools.similar_streets(ref)["classes"]) == trip["routes"]["fastest"]["crashes_5yr"]["n"]


def test_before_after_wording_never_claims_a_cause():
    busiest = tools.treated[tools.treated.treatment == "lane: protected"].nlargest(40, "crashes_pre").edge_id.unique().tolist()
    result = tools.before_after(busiest)
    text = str(result).lower().replace("never say a lane worked or failed", "")
    assert "worked" not in text and "failed" not in text
    for r in result["results"]:
        assert "changed risk per rider" in r["finding"] or r["finding"].startswith("Too few")


def test_before_after_states_the_raw_change_then_the_comparison():
    def row(pre, post, control_post=1.0):
        return pd.DataFrame({"edge_id": ["x"], "crashes_pre": [pre], "crashes_post": [post], "length_mi": [1.0], "pre_years": [2.0],
                             "post_years": [2.0], "control_pre_rate": [1.0], "control_post_rate": [control_post]})
    big_drop = tools._did(row(100, 20), "lane")["finding"]
    assert big_drop.startswith("Crashes fell from 50.0 to 10.0 a year, a bigger drop than on similar streets")
    both_fell = tools._did(row(100, 80, control_post=0.8), "lane")["finding"]
    assert both_fell.startswith("Crashes fell from 50.0 to 40.0 a year, but similar streets that weren't changed fell by about as much")
    rose = tools._did(row(100, 110), "lane")["finding"]
    assert rose.startswith("Crashes rose from 50.0 to 55.0 a year")
    for text in (big_drop, both_fell, rose):
        assert "did not fall" not in text and "can't tell whether the lane changed risk per rider" in text
    assert tools._did(row(2, 1), "lane")["finding"].startswith("Too few")
    assert tools._did(row(5, 0), "lane")["too_sparse"] is True


def test_standouts_have_their_whole_range_above_similar_streets(trip):
    ids = [e for r in trip["routes"].values() for e in tools.resolve(r["route_ref"])]
    result = tools.standout_segments(ids)
    assert len(result["hotspots"]) <= 5
    for spot in result["hotspots"]:
        assert spot["crashes"]["low"] > spot["similar_streets_expected"]
    assert [s["number"] for s in result["hotspots"]] == list(range(1, len(result["hotspots"]) + 1))
    quiet = tools.edges[tools.edges.crashes == 0].index[:20].tolist()
    assert tools.standout_segments(quiet)["finding"] == "No single stretch stands out from similar streets."


def test_no_treated_segments():
    untouched = tools.edges[~tools.edges.index.isin(tools.treated.edge_id)].index[0]
    assert tools.before_after([untouched])["treated_segments"] == 0


def test_sql_reads_and_refuses_writes():
    assert tools.run_sql("SELECT count(*) AS n FROM crashes")["rows"][0]["n"] == 34465  # includes crashes with no coordinates
    for bad in ["DROP TABLE crashes", "SELECT 1; SELECT 2", "COPY crashes TO '/tmp/x.csv'", "INSERT INTO crashes SELECT * FROM crashes"]:
        assert "error" in tools.run_sql(bad), bad
    assert "error" in tools.run_sql("SELECT * FROM read_csv('/etc/passwd')")


def test_sql_cannot_see_per_rider_columns():
    assert "error" in tools.run_sql("SELECT route_risk_assumed FROM edges")


def test_bad_refs_give_a_next_step():
    result = tools.crash_summary("route:nope")
    assert "error" in result and "next_step" in result


def test_every_tool_returns_strict_json(trip):
    ref = trip["routes"]["fastest"]["route_ref"]
    for result in (trip, tools.crash_summary(ref), tools.infrastructure_summary(ref), tools.exposure_summary(ref),
                   tools.similar_streets(ref), tools.before_after(ref), tools.run_sql("SELECT * FROM edges LIMIT 3")):
        json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("text, lat, lon", [
    ("116th St and Broadway, Manhattan", 40.8078, -73.9640),
    ("W 14th St & 8th Ave", 40.7400, -74.0025),
    ("Flatbush Ave and Atlantic Ave, Brooklyn", 40.6840, -73.9777),
])
def test_intersections_land_on_the_corner(text, lat, lon):
    place = tools.geocode(text)
    assert abs(place["lat"] - lat) < 0.003 and abs(place["lon"] - lon) < 0.003, place


def test_streets_that_never_meet_are_an_error():
    assert "error" in tools.geocode("Broadway and Atlantic Avenue, Queens at nowhere")
    assert "error" in tools.geocode("Bleecker Street and Flatbush Avenue, Brooklyn")


def test_place_names_find_the_neighborhood_not_a_namesake():
    astoria = tools.geocode("Astoria, Queens")  # GeoSearch alone returns a power plant
    assert "Astoria" in astoria["label"] and astoria["lat"] < 40.775 and astoria["lon"] < -73.92


def test_time_filters_never_change_the_average_check(trip):
    ref = trip["routes"]["fastest"]["route_ref"]
    morning = tools.crash_summary(ref, {"hour_from": 7, "hour_to": 10})
    every = tools.crash_summary(ref)
    assert morning["above_average"] == every["above_average"]
    assert [s["above_average"] for s in morning["hotspots"]] == [s["above_average"] for s in every["hotspots"]]
    assert morning["crashes_matching_filters"]["n"] < every["all_crashes"]["n"]


def test_projects_have_names_not_record_numbers(trip):
    for route in trip["routes"].values():
        for result in tools.before_after(route["route_ref"]).get("results", []):
            assert "bike route" not in result["project"] and not result["project"].startswith("sip-")


def test_one_segment_gets_the_same_verdict_both_ways(trip):
    for spot in tools.crash_summary(trip["routes"]["fastest"]["route_ref"])["hotspots"]:
        assert tools.crash_summary([spot["edge_id"]])["above_average"] == spot["above_average"]
