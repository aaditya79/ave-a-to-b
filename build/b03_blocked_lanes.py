"""311 blocked bike lane complaints (erm2-nwe9): complaint type Illegal Parking, descriptor Blocked Bike Lane."""

import pandas as pd

from common import START, cached, report, soda_all

WHERE = f"complaint_type='Illegal Parking' AND descriptor='Blocked Bike Lane' AND created_date>='{START}'"


def main() -> None:
    frame = cached("blocked_lanes", lambda: soda_all("erm2-nwe9", "unique_key,created_date,latitude,longitude", WHERE, "unique_key"))
    frame["created_date"] = pd.to_datetime(frame.created_date)
    frame["latitude"] = pd.to_numeric(frame.latitude, errors="coerce")
    frame["longitude"] = pd.to_numeric(frame.longitude, errors="coerce")
    print(f"{frame.latitude.isna().sum():,} complaints have no coordinates")
    print(frame.groupby(frame.created_date.dt.year).size().to_string())
    report("blocked_lanes", frame.dropna(subset=["latitude", "longitude"]))


if __name__ == "__main__":
    main()
