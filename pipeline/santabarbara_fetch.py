"""
Fetch the current Santa Barbara County assessor parcel snapshot (single family
residential only) from the county's public ArcGIS REST FeatureServer.

Unlike San Francisco's DataSF roll, this service has NO multi-year history --
it is a single current-snapshot table (LandValue, StrImpr, TradeFix, LivImpr,
SqFootage, DocDate/DocNum, ValReason, etc. all describe "today's" roll only).
That means santabarbara_process.py cannot use SF's year-over-year "jump"
detection and instead uses recency-of-last-transfer (DocDate) plus the
ValReason code as its reset-detection signal -- see that script for details.

Source: Santa Barbara County AssessorParcels FeatureServer, layer 0.
  https://services6.arcgis.com/STxBI5x7lq6k9HIB/arcgis/rest/services/AssessorParcels/FeatureServer/0
No API key required. maxRecordCount is 2000/request, so we page with
resultOffset. We ask the service for a projected centroid (returnCentroid,
outSR=4326) instead of full parcel polygon geometry -- we only need a single
lat/lon per parcel for nearest-neighbor comp lookup, and pulling full
polygons for ~78k parcels would be far heavier than necessary.

Reads:  nothing (hits the network)
Writes: pipeline/tmp/santabarbara_snapshot_raw.json
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
OUT_PATH = TMP_DIR / "santabarbara_snapshot_raw.json"

BASE_URL = (
    "https://services6.arcgis.com/STxBI5x7lq6k9HIB/arcgis/rest/services/"
    "AssessorParcels/FeatureServer/0/query"
)
OUT_FIELDS = (
    "APN,Situs1,Situs2,LandUse,UseCode,SqFootage,LandValue,StrImpr,TradeFix,"
    "LivImpr,DocDate,DocNum,ValReason,PctTransf,YearBuilt,Bedrooms,Bathrooms"
)
WHERE = "LandUse='SINGLE FAMILY RESIDENCE'"
PAGE_SIZE = 2000  # service-enforced maxRecordCount


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
            "orderByFields": "APN",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "f": "json",
        }
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    batch = json.loads(resp.read())
                break
            except Exception as e:
                print(f"  retry {attempt} after error: {e}", file=sys.stderr)
                time.sleep(2)
        else:
            raise RuntimeError(f"failed to fetch page at offset {offset}")

        if "error" in batch:
            raise RuntimeError(f"ArcGIS error at offset {offset}: {batch['error']}")

        feats = batch.get("features", [])
        # flatten {attributes, centroid} -> one dict per row for easy downstream use
        for f in feats:
            row = dict(f["attributes"])
            c = f.get("centroid") or {}
            row["_lon"] = c.get("x")
            row["_lat"] = c.get("y")
            rows.append(row)

        print(f"  offset={offset} got={len(feats)} total_so_far={len(rows)}", file=sys.stderr)
        with open(out_path, "w") as fh:
            json.dump(rows, fh)

        if len(feats) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.2)
    return rows


if __name__ == "__main__":
    TMP_DIR.mkdir(exist_ok=True)
    print("fetching Santa Barbara County single-family-residential parcels...", file=sys.stderr)
    data = fetch_all(OUT_PATH)
    print(f"DONE. total rows: {len(data)} -> {OUT_PATH}", file=sys.stderr)
