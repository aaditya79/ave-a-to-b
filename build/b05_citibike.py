"""Citi Bike trips: starts and ends per station, then per H3 cell. Raw trips never leave this script.

Usage: b05_citibike.py [first YYYYMM] [last YYYYMM]. Zips are deleted after they are counted.
"""

import sys
import zipfile

import h3
import pandas as pd
import requests

from common import RAW, report

FILE = "https://s3.amazonaws.com/tripdata/{month}-citibike-tripdata.zip"
H3_RES = 9
USE = ["start_station_id", "start_lat", "start_lng", "end_station_id", "end_lat", "end_lng"]
FIRST, LAST = "202510", "202609"  # the last 12 full months published


def count_month(month: str) -> pd.DataFrame:
    """One month -> rows of (station, lat, lon, starts, ends). Cached so a rerun doesn't download again."""
    done = RAW / f"citibike_{month}.parquet"
    if done.exists():
        return pd.read_parquet(done)
    path = RAW / f"{month}.zip"
    with requests.get(FILE.format(month=month), stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(path, "wb") as out:
            for chunk in response.iter_content(1 << 20):
                out.write(chunk)
    parts = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith(".csv") or name.startswith("__MACOSX"):
                continue
            with archive.open(name) as f:
                trips = pd.read_csv(f, usecols=USE, dtype={"start_station_id": str, "end_station_id": str})
            for side, col in (("start", "starts"), ("end", "ends")):
                ends = trips.dropna(subset=[f"{side}_station_id", f"{side}_lat", f"{side}_lng"])
                parts.append(ends.groupby(f"{side}_station_id").agg(
                    lat=(f"{side}_lat", "median"), lon=(f"{side}_lng", "median"), n=(f"{side}_lat", "size"),
                ).reset_index(names="station").assign(kind=col))
    rows = pd.concat(parts)
    totals = rows.pivot_table(index="station", columns="kind", values="n", aggfunc="sum", fill_value=0).reset_index()
    place = rows.groupby("station")[["lat", "lon"]].median().reset_index()
    result = totals.merge(place, on="station").assign(month=month)
    result.to_parquet(done, index=False)
    path.unlink()
    print(f"{month}: {result.starts.sum():,} trips, {len(result):,} stations", flush=True)
    return result


def main() -> None:
    first, last = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else (FIRST, LAST)
    months = [m.strftime("%Y%m") for m in pd.date_range(pd.to_datetime(first, format="%Y%m"), pd.to_datetime(last, format="%Y%m"), freq="MS")]
    all_months = pd.concat([count_month(m) for m in months])
    print(all_months.groupby("month").starts.sum().to_string())

    stations = all_months.groupby("station").agg(starts=("starts", "sum"), ends=("ends", "sum"), lat=("lat", "median"),
                                                 lon=("lon", "median"), months=("month", "nunique")).reset_index()
    stations["h3"] = [h3.latlng_to_cell(a, b, H3_RES) for a, b in zip(stations.lat, stations.lon)]
    report("citibike_stations", stations)
    cells = stations.groupby("h3").agg(starts=("starts", "sum"), ends=("ends", "sum"), stations=("station", "count")).reset_index()
    cells["trips_per_day"] = (cells.starts + cells.ends) / (len(months) * 365 / 12)
    report("citibike_h3", cells)


if __name__ == "__main__":
    main()
