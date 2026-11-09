"""Crashes (h9gi-nx95) joined to rider rows from Person (f55k-p6yu).

Two choices the raw columns force:
- Riders. NYPD logs many e-bike riders as person_type "Other Motorized", and that share has grown
  since 2024. The crash table's cyclist counts miss them. So a crash counts if a cyclist was hurt
  OR a hurt "Other Motorized" person was in a crash involving an e-bike.
- Severity. bodily_injury is only the body part. The type of injury is in complaint, and
  emotional_status records loss of consciousness, so severe uses those. NYC does not publish its
  own severe-injury flag, so this is a proxy based on what officers wrote at the scene.
"""

import re

import pandas as pd

from common import DATA, START, cached, report, soda_all

CRASH_COLS = ("collision_id,crash_date,crash_time,borough,latitude,longitude,on_street_name,off_street_name,"
              "cross_street_name,number_of_cyclist_injured,number_of_cyclist_killed,number_of_persons_injured,"
              "number_of_persons_killed,contributing_factor_vehicle_1,contributing_factor_vehicle_2,"
              "contributing_factor_vehicle_3,vehicle_type_code1,vehicle_type_code2,vehicle_type_code_3,"
              "vehicle_type_code_4,vehicle_type_code_5")
PERSON_COLS = ("unique_id,collision_id,person_type,person_injury,bodily_injury,complaint,emotional_status,"
               "safety_equipment")

VEHICLE_COLS = ["vehicle_type_code1", "vehicle_type_code2", "vehicle_type_code_3", "vehicle_type_code_4",
                "vehicle_type_code_5"]
SEVERE_COMPLAINTS = {"Fracture - Distorted - Dislocation", "Internal", "Concussion", "Severe Bleeding",
                     "Severe Lacerations", "Crush Injuries", "Amputation", "Paralysis", "Severe Burn",
                     "Moderate Burn"}
SEVERE_STATES = {"Unconscious", "Semiconscious", "Apparent Death"}
# A box around the five boroughs. Coordinates outside it (mostly 0, 0) count as missing.
LAT, LON = (40.45, 40.95), (-74.30, -73.65)


def normalize_vehicle(text) -> str | None:
    """Group the free-text vehicle field. Order matters: check e-bike before bike."""
    if not isinstance(text, str) or not text.strip():
        return None
    t = re.sub(r"[^A-Z0-9]", "", text.upper())
    if "EBIK" in t or "EBK" in t or re.search(r"ELE?C?T?RI?C?BI|PEDALASSIST", t):
        return "e-bike"
    if re.search(r"^ESCOOT|^ESCO|ELECTRICSC|^ESC$|^EVSCOOT", t):
        return "e-scooter"
    if t in ("BIKE", "BICYCLE", "BIK", "BIC", "BICYC", "BICY", "PEDALBIKE", "CITIBIKE", "CITIBIK") or t.startswith("BICYCL"):
        return "bike"
    if "MOPED" in t or "MOPAD" in t or "MOPD" in t:
        return "moped"
    if "SCOOT" in t:
        return "scooter"
    if "MOTORCYCLE" in t or t in ("MOTORBIKE", "DIRTBIKE"):
        return "motorcycle"
    if "TAXI" in t or "LIVERY" in t or t in ("CAB",):
        return "taxi"
    if "BUS" in t:
        return "bus"
    if any(w in t for w in ("TRUCK", "TRAILER", "TRACTOR", "DUMP", "FLATBED", "TANKER", "CONCRETE", "GARBAGE",
                            "REFUSE", "TOW", "BOXTR", "BOX")):
        return "truck"
    if "VAN" in t:
        return "van"
    if any(w in t for w in ("SEDAN", "SUV", "SPORTUTIL", "STATIONWAGON", "PASSENGER", "PICKUP", "CONVERTIBLE", "4DR", "2DR")):
        return "car"
    return "other"


def main() -> None:
    crashes = cached("crashes", lambda: soda_all("h9gi-nx95", CRASH_COLS, f"crash_date>='{START}'", "collision_id"))
    people = cached("people", lambda: soda_all(
        "f55k-p6yu", PERSON_COLS,
        f"crash_date>='{START}' AND person_type in('Bicyclist','Other Motorized')", "unique_id"))
    print(f"raw: {len(crashes):,} crashes, {len(people):,} rider-type person rows", flush=True)

    crashes = crashes.drop_duplicates("collision_id").copy()
    for col in ["number_of_cyclist_injured", "number_of_cyclist_killed", "number_of_persons_injured",
                "number_of_persons_killed"]:
        crashes[col] = pd.to_numeric(crashes[col], errors="coerce").fillna(0).astype(int)

    # Normalize vehicle text and log every mapping
    texts = pd.concat([crashes[c] for c in VEHICLE_COLS]).dropna()
    mapping = texts.value_counts().rename_axis("raw").reset_index(name="rows")
    mapping["normalized"] = mapping["raw"].map(normalize_vehicle)
    mapping.to_csv(DATA / "vehicle_type_mapping.csv", index=False)
    print(mapping.groupby("normalized")["rows"].sum().sort_values(ascending=False).to_string())
    print("e-bike spellings:", mapping[mapping.normalized == "e-bike"].raw.head(25).tolist())
    lookup = dict(zip(mapping.raw, mapping.normalized))
    vehicles = crashes[VEHICLE_COLS].apply(lambda col: col.map(lookup))
    crashes["vehicles"] = vehicles.apply(lambda row: sorted({v for v in row if isinstance(v, str)}), axis=1)
    crashes["has_ebike"] = crashes["vehicles"].map(lambda v: "e-bike" in v)

    # Rider rows, hurt only
    people["collision_id"] = people["collision_id"].astype(str)
    crashes["collision_id"] = crashes["collision_id"].astype(str)
    people = people.merge(crashes[["collision_id", "has_ebike"]], on="collision_id", how="inner")
    hurt = people[people.person_injury.isin(["Injured", "Killed"])]
    riders = hurt[(hurt.person_type == "Bicyclist") | hurt.has_ebike].copy()
    riders["rider_type"] = riders.has_ebike.map({True: "e-bike", False: "pedal"})
    riders["killed"] = riders.person_injury == "Killed"
    riders["severe"] = riders.killed | riders.complaint.isin(SEVERE_COMPLAINTS) | riders.emotional_status.isin(SEVERE_STATES)

    per_crash = riders.groupby("collision_id").agg(
        riders_hurt=("unique_id", "count"), severe=("severe", "max"), killed=("killed", "max"),
        rider_type=("rider_type", "first"), any_helmet=("safety_equipment", lambda s: s.str.contains("Helmet", na=False).any()),
    ).reset_index()

    keep = crashes[(crashes.number_of_cyclist_injured + crashes.number_of_cyclist_killed > 0)
                   | crashes.collision_id.isin(per_crash.collision_id)].merge(per_crash, on="collision_id", how="left")
    unmatched = keep.riders_hurt.isna().sum()
    keep["riders_hurt"] = keep.riders_hurt.fillna(keep.number_of_cyclist_injured + keep.number_of_cyclist_killed).astype(int)
    keep["killed"] = keep.killed.astype("boolean").fillna(False) | (keep.number_of_cyclist_killed > 0)
    keep["severe"] = keep.severe.astype("boolean").fillna(False) | keep.killed
    keep["rider_type"] = keep.rider_type.fillna(keep.has_ebike.map({True: "e-bike", False: "pedal"}))
    keep["only_other_motorized"] = keep.number_of_cyclist_injured + keep.number_of_cyclist_killed == 0
    print(f"crashes kept: {len(keep):,}; added by the e-bike rule: {keep.only_other_motorized.sum():,}; "
          f"cyclist crashes with no Person row: {unmatched:,}")

    keep["crash_date"] = pd.to_datetime(keep.crash_date)
    keep["year"] = keep.crash_date.dt.year
    keep["hour"] = pd.to_numeric(keep.crash_time.str.split(":").str[0], errors="coerce")
    keep["latitude"] = pd.to_numeric(keep.latitude, errors="coerce")
    keep["longitude"] = pd.to_numeric(keep.longitude, errors="coerce")
    keep["has_coords"] = keep.latitude.between(*LAT) & keep.longitude.between(*LON)

    by_year = keep.groupby("year").agg(crashes=("collision_id", "count"), severe=("severe", "sum"), killed=("killed", "sum"),
                                       added_ebike=("only_other_motorized", "sum"), ebike=("has_ebike", "sum"),
                                       missing_coords=("has_coords", lambda s: (~s).sum()))
    by_year["missing_pct"] = (by_year.missing_coords / by_year.crashes * 100).round(1)
    print(by_year.to_string())
    print(f"missing coordinates overall: {(~keep.has_coords).mean() * 100:.1f}%")

    keep = keep.drop(columns=VEHICLE_COLS + ["number_of_persons_injured", "number_of_persons_killed"])
    keep["vehicles"] = keep.vehicles.map(lambda v: ", ".join(v))
    for col in ["severe", "killed"]:
        keep[col] = keep[col].astype(bool)
    report("crashes", keep)
    report("crash_people", riders[["unique_id", "collision_id", "person_type", "rider_type", "person_injury",
                                   "bodily_injury", "complaint", "emotional_status", "safety_equipment", "severe"]])


if __name__ == "__main__":
    main()
