"""DOT bike counters: sensors (6up2-gnw8) and daily bike totals per sensor (ct66-47at), last 24 months.

The counts table has 20 million 15-minute rows, so we ask the API to sum one month at a time.
"""

import pandas as pd

from common import cached, report, soda_all


def month_totals(start: pd.Timestamp) -> pd.DataFrame:
    end = start + pd.offsets.MonthBegin(1)
    return soda_all(
        "ct66-47at", "sensor_id,date_trunc_ymd(timestamp) as day,sum(counts) as bikes",
        f"travelmode='bike' AND timestamp>='{start:%Y-%m-%dT00:00:00}' AND timestamp<'{end:%Y-%m-%dT00:00:00}'"
        " GROUP BY sensor_id,day", "sensor_id,day")


def main() -> None:
    sensors = cached("sensors", lambda: soda_all("6up2-gnw8", "id,name,lat,lon,travelmodes,firstdata,lastdata", order="id"))
    last = pd.Timestamp.now().normalize().replace(day=1)
    months = pd.date_range(last - pd.DateOffset(months=24), last - pd.DateOffset(months=1), freq="MS")
    daily = cached("counter_daily", lambda: pd.concat([month_totals(m) for m in months], ignore_index=True))
    daily["day"] = pd.to_datetime(daily.day)
    daily["bikes"] = pd.to_numeric(daily.bikes)

    sensors = sensors[sensors.travelmodes.str.contains("bike", na=False)].rename(columns={"id": "sensor_id"})
    sensors["lat"] = pd.to_numeric(sensors.lat)
    sensors["lon"] = pd.to_numeric(sensors.lon)
    report("counter_sensors", sensors[["sensor_id", "name", "lat", "lon", "firstdata", "lastdata"]])
    report("counter_daily", daily)
    print(f"{daily.sensor_id.nunique()} sensors with counts, {daily.day.min():%Y-%m-%d} to {daily.day.max():%Y-%m-%d}")


if __name__ == "__main__":
    main()
