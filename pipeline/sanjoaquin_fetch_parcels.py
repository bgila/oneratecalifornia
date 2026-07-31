"""
Fetch San Joaquin County's current single-family-dwelling parcels from the
county's public ArcGIS REST API (opendata.sjgov.org).

Service: services2.arcgis.com/GQhSReJEO6f7tsvy/arcgis/rest/services/Parcels/
FeatureServer/0 -- a single current-year snapshot (VALUE_ROLL_YEAR), no
history endpoint and NO sale-date/document-date field anywhere in its 72-field
schema (confirmed by inspecting FeatureServer/0?f=json's field list and
spot-querying sample rows; the closest-sounding fields, PROP_RETIRE_DATE and
SITUSLASTUPDATE, are about parcel retirement/address-record bookkeeping, not
sales). That's the reason this county's pipeline (see sanjoaquin_process.py)
can't use the jump-confirmed-comps approach the SF pipeline uses -- there's
no internal signal at all for whether a given assessed value is fresh or
decades-stale.

Scope: USECODE='10' ("SINGLE FAMILY DWELLING(SFD)"), the dominant residential
use code (178,991 of ~252,652 total parcels county-wide). This intentionally
excludes condos, duplexes, rural residences, etc. -- kept narrow so the
per-ZIP $/sqft rate (sanjoaquin_process.py) is built from, and applied to,
the same single-family housing stock that Zillow's ZHVI *_sfr_* (single-
family only, not sfrcondo) series measures.

Per parcel we keep: APN, the four assessed-value components (LAND_VALUE,
IMPROVEMENT_VALUE, STRUCTURE_VALUE, FIXED_EQUIP_VALUE), TOTALLIV_AREA
(building sqft), situs address parts, SITUSZIP, and a lat/lon centroid.
MAILZIPPREFIX is fetched too but deliberately NOT used for ZIP assignment --
see sanjoaquin_process.py's assign_zips() docstring for why (it's the owner's
mailing-address ZIP, not the property's, and turned out to be a bad proxy).
Parcel
geometry in this service is polygon, in a CA state-plane feet SRS (wkid
102643/2227); rather than pull full ring geometry and reproject client-side,
we ask the server itself for just the centroid, reprojected to WGS84
(returnCentroid=true, outSR=4326) -- much smaller payload, and no extra
geo dependency (pyproj/shapely) needed beyond stdlib.

Reads:  nothing (hits the network)
Writes: pipeline/tmp/sanjoaquin_parcels_raw.json
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
OUT_PATH = TMP_DIR / "sanjoaquin_parcels_raw.json"

BASE_URL = "https://services2.arcgis.com/GQhSReJEO6f7tsvy/arcgis/rest/services/Parcels/FeatureServer/0/query"
OUT_FIELDS = (
    "APN,LAND_VALUE,IMPROVEMENT_VALUE,STRUCTURE_VALUE,FIXED_EQUIP_VALUE,"
    "YEAR_BUILT,BEDROOMS,BATHROOM_WHOLE,BATHROOM_HALF,TOTALLIV_AREA,"
    "SITUSNUMBER,SITUSDIRECTION,SITUSTREET,SITUSTYPE,SITUSCITY,SITUSZIP,"
    "FULL_ADDRESS,MAILZIPPREFIX"
)
WHERE = "USECODE='10'"
PAGE_SIZE = 2000  # service's maxRecordCount; larger requests are silently truncated


def fetch_all(out_path):
    rows = []
    offset = 0
    while True:
        params = {
            "where": WHERE,
            "outFields": OUT_FIELDS,
            "returnGeometry": "false",
            "returnCentroid": "true",
            "outSR": "4326",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "f": "json",
        }
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    payload = json.loads(resp.read())
                if "error" in payload:
                    raise RuntimeError(payload["error"])
                break
            except Exception as e:
                print(f"  retry {attempt} after error: {e}", file=sys.stderr)
                time.sleep(2)
        else:
            raise RuntimeError(f"failed to fetch page at offset {offset}")

        feats = payload.get("features", [])
        for f in feats:
            attrs = f["attributes"]
            centroid = f.get("centroid") or {}
            attrs["_lon"] = centroid.get("x")
            attrs["_lat"] = centroid.get("y")
            rows.append(attrs)

        print(f"  offset={offset} got={len(feats)} total_so_far={len(rows)}", file=sys.stderr)
        with open(out_path, "w") as f:
            json.dump(rows, f)

        if len(feats) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.1)
    return rows


if __name__ == "__main__":
    TMP_DIR.mkdir(exist_ok=True)
    print(f"fetching San Joaquin parcels where {WHERE!r}...", file=sys.stderr)
    data = fetch_all(OUT_PATH)
    print(f"DONE. total rows: {len(data)} -> {OUT_PATH}", file=sys.stderr)
