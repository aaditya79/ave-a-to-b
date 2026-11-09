"""Bike routes (mzxg-pwib), SIP corridors (if4c-w48d), and truck routes (jjja-shxy), stored as WKT."""

import json

import pandas as pd
from shapely.geometry import shape

from common import cached, report, soda_all


def with_wkt(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame[frame.the_geom.notna()].copy()
    frame["wkt"] = frame.the_geom.map(lambda g: shape(g if isinstance(g, dict) else json.loads(g)).wkt)
    return frame.drop(columns="the_geom")


def main() -> None:
    routes = cached("bike_routes", lambda: with_wkt(soda_all(
        "mzxg-pwib", "the_geom,segmentid,bikeid,prevbikeid,status,boro,street,fromstreet,tostreet,onoffst,facilitycl,"
                     "allclasses,bikedir,lanecount,ft_facilit,tf_facilit,ft2facilit,tf2facilit,instdate,ret_date")))
    print(routes.status.value_counts().to_string())
    print(routes.ft_facilit.value_counts().head(20).to_string())
    routes["instdate"] = pd.to_datetime(routes.instdate, errors="coerce")
    routes["ret_date"] = pd.to_datetime(routes.ret_date, errors="coerce")
    report("bike_routes", routes)

    sips = cached("sip", lambda: with_wkt(soda_all("if4c-w48d", "the_geom,pjct_name,sip_year,end_date")))
    sips["sip_id"] = "sip-" + sips.index.astype(str)
    sips["sip_year"] = pd.to_numeric(sips.sip_year, errors="coerce")
    sips["end_date"] = pd.to_datetime(sips.end_date, errors="coerce")
    report("sip", sips)

    trucks = cached("truck_routes", lambda: with_wkt(soda_all("jjja-shxy", "the_geom,street,routetype,boroname,segmentid")))
    print(trucks.routetype.value_counts().to_string())
    report("truck_routes", trucks)


if __name__ == "__main__":
    main()
