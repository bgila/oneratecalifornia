"""
Fetch Sonoma County single-family-dwelling parcels from the county's public
ArcGIS FeatureServer (no login required).

Endpoint: socogis.sonomacounty.ca.gov/map/rest/services/CRAPublic/
ParcelsPublic/FeatureServer/0

This layer is unusually rich for a public assessor feed: alongside the
current-roll assessed land/structure/fixtures values it carries the two most
recent recorded sale events per parcel (SaleRecordingDate/SaleSalesPrice for
the current sale, SalePriorRecordingDate/SalePriorSalesPrice for the one
before it), plus lat/lon already geocoded per parcel (Lat/Long fields) --
so no separate multi-year assessed-value-history fetch is needed the way
SF's 02_fetch_sfr_history.py is: real recorded transaction prices are a
strictly better market-value signal than SF's assessed-value-jump proxy.

Scope: UseCode='0010' ("SINGLE FAMILY DWELLING"), Sonoma's single dominant
single-family use code (~94,800 of the county's ~189,000 total parcels).
This mirrors SF's own scope (use_definition="Single Family Residential")
and keeps the run tractable; see sonoma_process.py's methodology output for
the exact counts.

Reads:  nothing (hits the network)
Writes: pipeline/tmp/sonoma_parcels_raw.json
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
OUT_PATH = TMP_DIR / "sonoma_parcels_raw.json"

BASE_URL = (
    "https://socogis.sonomacounty.ca.gov/map/rest/services/CRAPublic/"
    "ParcelsPublic/FeatureServer/0/query"
)
FIELDS = (
    "APN,UseCode,UseCodeDescription,SitusFormatted1,SitusFormatted2,"
    "LandSizeSqft,BuildingPrimarySize,BuildingPrimaryYearBuilt,"
    "BuildingPrimaryBedRooms,BuildingPrimaryBaths,"
    "Value601Land,Value601Structure,Value601Fixtures,Value601RollYear,"
    "SaleRecordingDate,SaleSalesPrice,SaleTranserType,"
    "SalePriorRecordingDate,SalePriorSalesPrice,SalePriorTransferType,"
    "Lat,Long"
)
WHERE = "UseCode='0010'"
PAGE_SIZE = 2000  # server's maxRecordCount


def fetch_all(out_path):
    rows = []
    offset = 0
    while True:
        params = {
            "where": WHERE,
            "outFields": FIELDS,
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "f": "json",
        }
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        for attempt in range(5):
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

        batch = [f["attributes"] for f in payload.get("features", [])]
        rows.extend(batch)
        print(f"  offset={offset} got={len(batch)} total_so_far={len(rows)}", file=sys.stderr)
        with open(out_path, "w") as f:
            json.dump(rows, f)

        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.2)
    return rows


if __name__ == "__main__":
    TMP_DIR.mkdir(exist_ok=True)
    print(f"fetching Sonoma parcels where {WHERE!r}...", file=sys.stderr)
    data = fetch_all(OUT_PATH)
    print(f"DONE. total rows: {len(data)} -> {OUT_PATH}", file=sys.stderr)
