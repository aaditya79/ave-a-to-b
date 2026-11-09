"""Exposure and Empirical Bayes risk. Writes edges.parquet, the project's main table.

Exposure is an approximation with three steps:
1. Citi Bike activity in a segment's H3 cell and its six neighbors says how busy the area is.
2. A ratio from DOT street counters turns that into riders per day. Nearly all of those counters
   sit on bike lanes, so the result estimates a street with a good lane.
3. STREET_SHARE scales that down for other streets. This step is an ASSUMPTION, not a measurement:
   no public data counts riders on ordinary side streets. Without it every street in a cell gets the
   same riders, and streets with lanes look dangerous simply because that is where people ride.
   So route_risk comes in three versions, at half, assumed, and double the gap between streets.

Risk: crashes on a segment are Poisson, and each "similar street" class has a Gamma spread of true
rates. Then a segment's blended rate is (k + crashes) / (beta + mile_years): a short or quiet
segment stays near its class rate, and a long one with plenty of data keeps close to its own.
"""

import h3
import numpy as np
import pandas as pd

from common import DATA, report

MIN_CLASS_MILES = 5  # smaller classes borrow from the same lane and road type citywide
MIN_RIDERS = 10  # a floor, so a near-zero estimate can't produce an enormous per-rider rate
# Share of a lane street's riders that each kind of street carries. Assumed; see the note above.
STREET_SHARE = {"protected": 1.0, "path": 1.0, "link": 1.0, "painted": 0.75, "shared": 0.5}
NO_LANE_SHARE = {"arterial": 0.25, "collector": 0.25, "local": 0.125, "path": 0.25}
# Routing runs at all three. A route is only called safest if it wins at every one.
SETTINGS = {"half": 0.5, "assumed": 1.0, "double": 2.0}


def citi_activity(cells: pd.DataFrame) -> dict:
    """Mean trips per day across a cell and its neighbors, for every cell near a station."""
    trips = dict(zip(cells.h3, cells.trips_per_day))
    nearby = {n for cell in trips for n in h3.grid_disk(cell, 1)}
    return {cell: sum(trips.get(n, 0) for n in h3.grid_disk(cell, 1)) / 7 for cell in nearby}


def counter_scale(activity: dict, edges: pd.DataFrame) -> float:
    """Median ratio of counted riders to Citi Bike activity across DOT street sensors. One number for
    the whole city: the sensors range from about 3.7 to 45.9, so finer splits would rest on two or
    three sensors each."""
    sensors = pd.read_parquet(DATA / "counter_sensors.parquet").drop_duplicates("sensor_id")
    # Bridges collect riders from a wide area, and ped paths barely count bikes. Neither says much
    # about the street around them.
    sensors = sensors[~sensors.name.str.contains("Bridge|Ped Path", case=False)]
    daily = pd.read_parquet(DATA / "counter_daily.parquet")
    recent = daily[daily.day >= daily.day.max() - pd.Timedelta(days=365)]
    mean = recent.groupby("sensor_id").bikes.agg(["mean", "count"]).reset_index()
    mean = mean[mean["count"] >= 180].merge(sensors, on="sensor_id")
    mean["h3"] = [h3.latlng_to_cell(a, b, 9) for a, b in zip(mean.lat, mean.lon)]
    mean["activity"] = mean.h3.map(activity)
    # One place can have several sensor records with identical totals.
    usable = mean[mean.activity > 0].assign(rounded=lambda d: d["mean"].round()).drop_duplicates(["h3", "rounded"]).copy()
    usable["ratio"] = usable["mean"] / usable.activity
    boroughs = edges.groupby("h3").borough.first()
    usable["borough"] = usable.h3.map(boroughs)
    usable[["sensor_id", "name", "mean", "activity", "ratio", "borough"]].to_parquet(DATA / "counter_calibration.parquet", index=False)
    print(f"{len(mean)} active sensors, {len(usable)} inside Citi Bike coverage")
    print(usable[["name", "mean", "activity", "ratio", "borough"]].round(2).sort_values("ratio").to_string(index=False))
    return float(usable.ratio.median())


def gamma_prior(crashes: np.ndarray, mile_years: np.ndarray) -> tuple[float, float, float]:
    """Method of moments for rate, and for the spread of true rates beyond Poisson noise.
    Returns (rate, k, beta). k is None-like (inf) when the class shows no extra spread."""
    rate = crashes.sum() / mile_years.sum()
    if rate == 0:
        return 0.0, np.inf, np.inf
    expected = rate * mile_years
    spread = (((crashes - expected) ** 2).sum() - expected.sum()) / (mile_years**2).sum()
    if spread <= 0:
        return rate, np.inf, np.inf
    return rate, rate**2 / spread, rate / spread


def blend(edges: pd.DataFrame, count: str, out: str) -> pd.DataFrame:
    """Add class rate, weight on the segment's own data, and blended rate per mile-year."""
    rows = []
    for key, group in edges.groupby("risk_class"):
        rate, k, beta = gamma_prior(group[count].to_numpy(float), group.mile_years.to_numpy())
        if np.isinf(beta):
            frame = pd.DataFrame({"own": 0.0, "blended": rate}, index=group.index)
        else:
            frame = pd.DataFrame({"own": group.mile_years / (group.mile_years + beta),
                                  "blended": (k + group[count]) / (beta + group.mile_years)}, index=group.index)
        frame["class_rate"] = rate
        rows.append(frame)
    result = pd.concat(rows)
    edges[f"{out}_class_rate"] = result.class_rate
    edges[f"{out}_own_weight"] = result.own
    edges[f"{out}_blended_rate"] = result.blended
    return edges


def riders_per_day(edges: pd.DataFrame, power: float) -> pd.Series:
    """Estimated riders per day. power stretches the assumed street shares: 0.5 halves the gap between
    streets and 2 doubles it. Segments outside Citi Bike's area take the median of their similar-street
    class, or of their lane and road type where the whole class is uncovered (all of Staten Island);
    exposure_source says similar_streets on those, and answers flag them as lower confidence."""
    share = edges.lane_type.map(STREET_SHARE).fillna(edges.road_class.map(NO_LANE_SHARE)) ** power
    riders = (edges.citi_activity * edges.exposure_scale * share).where(edges.citi_activity > 0)
    similar = riders.groupby(edges.risk_class).transform("median")
    broader = riders.groupby([edges.lane_type, edges.road_class]).transform("median")
    return riders.fillna(similar).fillna(broader).fillna(riders.median()).clip(lower=MIN_RIDERS)


def add_per_rider_risk(edges: pd.DataFrame) -> pd.DataFrame:
    """route_risk is expected crashes per million trips over the segment, so it adds up along a route.
    Routes are scored at all three settings because the street shares are an assumption. These
    numbers are for comparing routes; the agent never quotes them as a rider's odds."""
    edges["expected_crashes_per_year"] = edges.crash_blended_rate * edges.length_mi
    for suffix, power in SETTINGS.items():
        riders = riders_per_day(edges, power)
        if suffix == "assumed":
            edges["exposure_daily"] = riders
        edges[f"route_risk_{suffix}"] = edges.expected_crashes_per_year * 1e6 / (riders * 365)
    return edges


def main() -> None:
    edges = pd.read_parquet(DATA / "edges_base.parquet")
    crashes = pd.read_parquet(DATA / "crashes.parquet", columns=["crash_date"])
    years = (crashes.crash_date.max() - pd.Timestamp("2021-01-01")).days / 365.25
    print(f"window: {years:.2f} years")
    edges["mile_years"] = edges.length_mi * years
    edges["raw_rate"] = edges.crashes / edges.mile_years

    # Exposure
    cells = pd.read_parquet(DATA / "citibike_h3.parquet")
    activity = citi_activity(cells)
    edges["citi_activity"] = edges.h3.map(activity).fillna(0.0)
    scale = counter_scale(activity, edges)
    print(f"riders per Citi Bike trip (median sensor): {scale:.2f}")
    edges["exposure_scale"] = scale
    covered = edges.citi_activity > 0
    edges["exposure_source"] = np.where(covered, "citibike_x_counter_median", "similar_streets")
    print(edges.groupby("exposure_source").length_mi.sum().round(0).to_string())

    # Similar-street classes
    edges["risk_class"] = edges.lane_type + "|" + edges.road_class + "|" + edges.borough.fillna("Unknown")
    miles = edges.groupby("risk_class").length_mi.transform("sum")
    small = miles < MIN_CLASS_MILES
    edges.loc[small, "risk_class"] = edges.lane_type + "|" + edges.road_class + "|citywide"
    print(f"{edges.risk_class.nunique()} classes; {small.sum():,} segments borrow a citywide class")

    edges = blend(edges, "crashes", "crash")
    edges = blend(edges, "severe_crashes", "severe")

    add_per_rider_risk(edges)
    for suffix in SETTINGS:
        rate = edges.groupby("lane_type").apply(lambda g: g[f"route_risk_{suffix}"].sum() / g.length_mi.sum(), include_groups=False)
        print(f"mean route risk per mile, {suffix}: {rate.round(1).to_dict()}")

    summary = edges.groupby("lane_type").apply(lambda g: pd.Series({
        "miles": g.length_mi.sum(), "crashes": g.crashes.sum(), "per_mile_year": g.crashes.sum() / g.mile_years.sum(),
        "median_own_weight": g.crash_own_weight.median()}), include_groups=False).round(3)
    print(summary.to_string())
    report("edges", edges)


if __name__ == "__main__":
    main()
