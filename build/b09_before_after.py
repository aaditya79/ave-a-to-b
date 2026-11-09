"""Before/after table: difference-in-differences for segments that got a lane or SIP in the window.

Treated segment: got a new or better lane, or a SIP finished, between 2022 and mid-2025, leaving at
least a year before and a year after. Comparison: same road class and borough, with no lane change
or SIP at all in the window, over the same calendar dates. DiD = change in treated rate minus
change in comparison rate, in crashes per mile-year.
"""

import numpy as np
import pandas as pd

from common import DATA, report
from b07_edges import LANE_RANK

WINDOW_START = pd.Timestamp("2021-01-01")
MIN_YEARS = 1.0


def lane_treatments(history: pd.DataFrame, window_end: pd.Timestamp) -> pd.DataFrame:
    """A lane counts if it beats everything that was on the segment before it. A re-marked lane of
    the same type gets a new record in the city's table and should not count."""
    rows = []
    history = history.assign(rank=history.lane_type.map(LANE_RANK)).sort_values("instdate")
    for edge_id, group in history.groupby("edge_id"):
        for row in group[group.instdate >= WINDOW_START].itertuples():
            earlier = group[group.instdate < row.instdate]
            before = earlier["rank"].max() if len(earlier) else 0
            if row.rank > before:
                rows.append({"edge_id": edge_id, "treatment": f"lane: {row.lane_type}", "treated_on": row.instdate,
                             "project": f"bike route {row.bikeid}"})
                break
    return pd.DataFrame(rows)


def main() -> None:
    edges = pd.read_parquet(DATA / "edges.parquet", columns=["edge_id", "road_class", "borough", "length_mi"])
    crashes = pd.read_parquet(DATA / "crashes.parquet", columns=["collision_id", "crash_date"])
    located = pd.read_parquet(DATA / "crash_locations.parquet", columns=["collision_id", "edge_id"]).merge(crashes, on="collision_id")
    window_end = crashes.crash_date.max()

    lanes = lane_treatments(pd.read_parquet(DATA / "edge_lanes.parquet"), window_end)
    sips = pd.read_parquet(DATA / "edge_sips.parquet")
    sip_rows = sips[sips.sip_date >= WINDOW_START].assign(treatment="SIP", project=lambda d: d.sip_id + ": " + d.pjct_name.fillna(""))
    sip_rows = sip_rows.rename(columns={"sip_date": "treated_on"})[["edge_id", "treatment", "treated_on", "project"]]
    all_treated = pd.concat([lanes, sip_rows], ignore_index=True)
    treated = all_treated.copy()

    # Anything changed in the window, including projects too recent to study, can't be a comparison.
    untouched = edges[~edges.edge_id.isin(all_treated.edge_id)]
    treated["pre_years"] = (treated.treated_on - WINDOW_START).dt.days / 365.25
    treated["post_years"] = (window_end - treated.treated_on).dt.days / 365.25
    treated = treated[(treated.pre_years >= MIN_YEARS) & (treated.post_years >= MIN_YEARS)]
    treated = treated.merge(edges, on="edge_id")
    print(f"{treated.edge_id.nunique():,} treated segments, {treated.length_mi.sum():,.0f} miles; "
          f"{len(untouched):,} untouched segments available as comparisons")

    # Comparison rates, one per (road class, borough, treatment date)
    by_edge_class = untouched.set_index("edge_id")[["road_class", "borough"]]
    control_crashes = located[located.edge_id.isin(by_edge_class.index)].join(by_edge_class, on="edge_id")
    miles = untouched.groupby(["road_class", "borough"]).length_mi.sum()
    grouped = {key: frame.crash_date.to_numpy() for key, frame in control_crashes.groupby(["road_class", "borough"])}
    empty = np.array([], dtype="datetime64[ns]")

    def control_rates(row) -> tuple[float, float, int, int]:
        dates = grouped.get((row.road_class, row.borough), empty)
        cut = np.datetime64(row.treated_on)
        pre, post = int((dates < cut).sum()), int((dates >= cut).sum())
        mi = miles.get((row.road_class, row.borough), np.nan)
        return pre / (mi * row.pre_years), post / (mi * row.post_years), pre, post

    rates = [control_rates(r) for r in treated[["road_class", "borough", "treated_on", "pre_years", "post_years"]].drop_duplicates().itertuples()]
    keys = treated[["road_class", "borough", "treated_on", "pre_years", "post_years"]].drop_duplicates().assign(
        control_pre_rate=[r[0] for r in rates], control_post_rate=[r[1] for r in rates],
        control_pre_crashes=[r[2] for r in rates], control_post_crashes=[r[3] for r in rates])
    treated = treated.merge(keys, on=["road_class", "borough", "treated_on", "pre_years", "post_years"])

    # Treated counts
    on_edges = located[located.edge_id.isin(treated.edge_id)][["edge_id", "crash_date"]].merge(treated[["edge_id", "treated_on"]], on="edge_id")
    on_edges["post"] = on_edges.crash_date >= on_edges.treated_on
    counts = on_edges.groupby(["edge_id", "treated_on", "post"]).size().unstack(fill_value=0).rename(columns={False: "crashes_pre", True: "crashes_post"}).reset_index()
    treated = treated.merge(counts, on=["edge_id", "treated_on"], how="left")
    for col in ["crashes_pre", "crashes_post"]:
        if col not in treated:
            treated[col] = 0
        treated[col] = treated[col].fillna(0).astype(int)
    treated["pre_rate"] = treated.crashes_pre / (treated.length_mi * treated.pre_years)
    treated["post_rate"] = treated.crashes_post / (treated.length_mi * treated.post_years)
    treated["did"] = (treated.post_rate - treated.pre_rate) - (treated.control_post_rate - treated.control_pre_rate)

    # Pooled result, weighting by mile-years. No lane can be judged alone at these counts.
    for name, group in treated.groupby(treated.treatment.str.split(":").str[0] + treated.treatment.str.extract(r"(: \w+)")[0].fillna("")):
        pre = group.crashes_pre.sum() / (group.length_mi * group.pre_years).sum()
        post = group.crashes_post.sum() / (group.length_mi * group.post_years).sum()
        cpre = np.average(group.control_pre_rate, weights=group.length_mi * group.pre_years)
        cpost = np.average(group.control_post_rate, weights=group.length_mi * group.post_years)
        print(f"{name}: {group.edge_id.nunique():,} segments, {group.length_mi.sum():.0f} mi; pre {group.crashes_pre.sum()} crashes "
              f"({pre:.2f}/mi-yr), post {group.crashes_post.sum()} ({post:.2f}); comparison {cpre:.2f} -> {cpost:.2f}; "
              f"ratio {post / pre:.2f} vs {cpost / cpre:.2f}")
    report("before_after", treated.drop(columns=["road_class"]))


if __name__ == "__main__":
    main()
