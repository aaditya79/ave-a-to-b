"""Street-name matching, shared by the offline build and the live geocoder."""

import re
import unicodedata

ABBREVIATIONS = {"AVE": "AVENUE", "AV": "AVENUE", "ST": "STREET", "BLVD": "BOULEVARD", "RD": "ROAD", "PKWY": "PARKWAY",
                 "PL": "PLACE", "DR": "DRIVE", "CT": "COURT", "LN": "LANE", "HWY": "HIGHWAY", "TPKE": "TURNPIKE",
                 "E": "EAST", "W": "WEST", "N": "NORTH", "S": "SOUTH"}
# One street, two names: police reports tend to use the first, OSM the second.
ALIASES = {"AVENUEOFTHEAMERICAS": "6AVENUE", "8AVENUE": "FREDERICKDOUGLASSBOULEVARD", "7AVENUE": "ADAMCLAYTONPOWELLJRBOULEVARD",
           "LENOXAVENUE": "MALCOLMXBOULEVARD", "WEST110STREET": "CENTRALPARKNORTH", "CATHEDRALPARKWAY": "CENTRALPARKNORTH",
           "2AVENUE": "MEXICOTENOCHTITLANAVENUE"}


def street_key(name) -> str:
    """'W 14th St' and 'WEST 14 STREET' -> 'WEST14STREET', so OSM and police names can be compared.
    Spaces go too, for 'DE KALB' and 'DeKalb'. Address-style reports put the house number in front
    ('694       5 AVE'), so that comes off first."""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    name = re.sub(r"^\s*\d[\d-]*[A-Z]?\s{2,}", "", name)
    words = re.sub(r"(\d)(ST|ND|RD|TH)\b", r"\1", re.sub(r"[^A-Z0-9 ]", " ", name)).split()
    if words and words[0] == "ST":
        words[0] = "SAINT"
    key = "".join(ABBREVIATIONS.get(w, w) for w in words)
    return ALIASES.get(key, key)
