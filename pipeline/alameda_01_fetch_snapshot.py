"""
Fetch the current (2025-26 roll) snapshot of Alameda County single-family
residential parcels from the county's public ArcGIS FeatureServer.

Alameda County (unlike SF) does not publish its assessor roll on Socrata --
it publishes it as a set of ArcGIS FeatureServer layers on data.acgov.org,
one per roll year, plus a separate "Parcels" polygon layer that carries
geometry/centroids. Verified live (2026-07-31) via
https://services5.arcgis.com/ROBnTHSNjoZ2Wm1P/arcgis/rest/services :

  - The per-year "Assessor Office Secured Tax Roll YYYY to YYYY" layers
    (used by 02_fetch_alameda_history.py) have NO geometry -- they are
    plain attribute tables, confirmed via each layer's `geometryType: None`.
  - The "Parcels" layer (this script) is the only layer in the org with
    parcel geometry, and its FeatureServer/0 layer *also* carries a
    same-day mirror of the assessed-value fields (Land/Imps/TotalNetValue/
    UseCode) -- spot-checked against the 2025-26 tax roll table for parcel
    "001 011100100" and the dollar figures match exactly. So this single
    layer gives us everything the current-year snapshot needs: geometry +
    situs address + current assessed value + use code.

Scope: Use_Code='1100' ("Single family residential homes used as such" --
confirmed via the Assessor_Office_Use_Codes reference layer), the closest
Alameda equivalent to SF's "Single Family Residential" use_definition
filter. This is ~265,500 parcels countywide -- large, but well within what
a ~140-request paginated fetch (2000 rows/page, the layer's server-side
max) can pull in a few minutes, so no further down-sampling was needed.

Important gap vs. SF: Alameda's public data has NO building square
footage field anywhere in this ArcGIS org (checked every layer's field
list, and the county's full open-data catalog of 160+ datasets -- there is
no "building characteristics"/"improvement records" dataset at all, only
parcel-boundary polygons). The `lot_sqft` this script emits is therefore
parcel LOT square footage (from the parcel polygon's Shape__Area), not
building square footage. This is used only as informational sqft for the
map, NOT as an input to the market-value math -- see alameda_03_process.py
for how that's handled.

Geometry note: CENTROID_X/CENTROID_Y come back in the layer's native Web
Mercator (EPSG:3857), not lat/lon. There is no on-the-fly reprojection for
plain attribute fields (only for returned geometry), so this script does
the standard spherical-Mercator inverse projection by hand rather than
pulling in pyproj as a new dependency.

Reads:  nothing (hits the network)
Writes: pipeline/tmp/alameda_parcels_raw.json
"""
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
OUT_PATH = TMP_DIR / "alameda_parcels_raw.json"

BASE_URL = "https://services5.arcgis.com/ROBnTHSNjoZ2Wm1P/arcgis/rest/services/Parcels/FeatureServer/0/query"
FIELDS = (
    "SortParcel,CENTROID_X,CENTROID_Y,SitusStreetNumber,SitusStreetName,SitusUnit,"
    "SitusCity,SitusZip,Land,Imps,TotalNetValue,UseCode,Shape__Area,LatestDocumentDate"
)
USE_CODE = "1100"
PAGE_SIZE = 2000  # server-enforced max for this layer (confirmed via maxRecordCount)

WEB_MERCATOR_R = 6378137.0


def web_mercator_to_lonlat(x, y):
    lon = x / WEB_MERCATOR_R * 180.0 / math.pi
    lat = (2 * math.atan(math.exp(y / WEB_MERCATOR_R)) - math.pi / 2) * 180.0 / math.pi
    return lon, lat


def fetch_all(page_size, out_path):
    """Page through the ArcGIS REST query endpoint, ordered by SortParcel for
    stable pagination, checkpointing to out_path after every page."""
    rows = []
    offset = 0
    while True:
        params = {
            "where": f"UseCode='{USE_CODE}'",
            "outFields": FIELDS,
            "orderByFields": "SortParcel",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "returnGeometry": "false",
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

        batch = payload.get("features", [])
        rows.extend(batch)
        print(f"  offset={offset} got={len(batch)} total_so_far={len(rows)}", file=sys.stderr)
        with open(out_path, "w") as f:
            json.dump(rows, f)

        if len(batch) < page_size:
            break
        offset += page_size
        time.sleep(0.2)
    return rows


def main():
    TMP_DIR.mkdir(exist_ok=True)
    print(f"fetching Use_Code={USE_CODE!r} parcels from the Parcels layer...", file=sys.stderr)
    raw = fetch_all(PAGE_SIZE, OUT_PATH)
    print(f"DONE. total raw rows: {len(raw)}", file=sys.stderr)

    # Sanity pass: convert centroids to lon/lat in place so downstream scripts
    # don't need to redo the Web Mercator math, and drop any row with no usable
    # centroid (none were seen in spot checks, but don't assume that holds county-wide).
    converted = 0
    for r in raw:
        a = r["attributes"]
        x, y = a.get("CENTROID_X"), a.get("CENTROID_Y")
        if x is None or y is None:
            a["lon"], a["lat"] = None, None
            continue
        lon, lat = web_mercator_to_lonlat(x, y)
        a["lon"], a["lat"] = lon, lat
        converted += 1
    with open(OUT_PATH, "w") as f:
        json.dump(raw, f)
    print(f"converted {converted}/{len(raw)} centroids to lon/lat -> {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
