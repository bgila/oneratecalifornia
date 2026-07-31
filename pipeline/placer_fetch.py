"""
Fetch the current Placer County assessor parcel snapshot (single-family
residential + condo parcels) from the county's public ArcGIS FeatureServer.

Unlike San Francisco's DataSF Socrata roll, Placer County has no bulk
sale-price dataset and no queryable multi-year assessed-value history --
this is a single current-roll snapshot. So this script pulls every field
placer_process.py needs to both (a) build the per-parcel estimate table and
(b) detect "confirmed recent reset" comps from *within that same snapshot*
(see placer_process.py's module docstring for how that detection works and
why TransactionDt needed special handling).

Source: "Parcels Public" feature layer, Placer County GIS Services
  Service:  https://services6.arcgis.com/PArfeTGcwA9RGNzN/arcgis/rest/services/Parcels_Public/FeatureServer/4
  Hub page: https://gis-placercounty.opendata.arcgis.com/ (search "Parcels Public")
This is the sanitized public view (no owner name/mailing address), confirmed
live via the FeatureServer's ?f=json metadata endpoint. Fields pulled:
  APN, LandValue, Structure (improvement value), LandSF, StructureSF,
  Use_Cd_N (detailed use description), Tax_Cd_N (assessment/exemption status
  description -- see placer_process.py for how this is used),
  TransactionDt (last recorded-document date), EffectiveYr,
  FormattedSitus1/2 + SitusAddressFull (site address), Community (small
  area code, used only for sanity-checking geographic spread).

Filtered server-side to Use_Cd_N in ("SINGLE FAM RES, HALF PLEX",
"SINGLE FAM RES, CONDO") -- Placer's assessor use-code scheme groups the
ordinary single-family-detached use under the (oddly-named, but confirmed
via Use_Cd/Use_Cd_N cross-tab, 143k of ~151k qualifying parcels) code
"SINGLE FAM RES, HALF PLEX", plus individually-deeded condos under
"SINGLE FAM RES, CONDO" -- together ~150,995 parcels, comparable in scale
to SF's ~155k single-family/condo roll.

Geometry: the layer is polygon (parcel boundary), not point. We request
returnCentroid=true / returnGeometry=false in WGS84 (outSR=4326), which
gets each parcel's centroid lat/lon directly from the server without
transferring full ring geometry -- much lighter, and all we need for a
point-marker map.

Reads:  nothing (hits the network)
Writes: pipeline/tmp/placer_parcels_raw.json

No API key required -- Placer's ArcGIS FeatureServer is open to the public
(same access model as any Esri-hosted public feature service), just paged
at the server's maxRecordCount (2000/request here).
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
OUT_PATH = TMP_DIR / "placer_parcels_raw.json"

BASE_URL = "https://services6.arcgis.com/PArfeTGcwA9RGNzN/arcgis/rest/services/Parcels_Public/FeatureServer/4/query"
OUT_FIELDS = (
    "OBJECTID,APN,LandValue,Structure,LandSF,StructureSF,Use_Cd_N,Tax_Cd_N,"
    "TransactionDt,EffectiveYr,FormattedSitus1,FormattedSitus2,SitusAddressFull,Community"
)
WHERE = "(Use_Cd_N='SINGLE FAM RES, HALF PLEX' OR Use_Cd_N='SINGLE FAM RES, CONDO')"
PAGE_SIZE = 2000  # server-enforced maxRecordCount for this layer


def fetch_all(out_path):
    rows = []
    offset = 0
    while True:
        params = {
            "where": WHERE,
            "outFields": OUT_FIELDS,
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "returnGeometry": "false",
            "returnCentroid": "true",
            "outSR": "4326",
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
        rows.extend(feats)
        print(f"  offset={offset} got={len(feats)} total_so_far={len(rows)}", file=sys.stderr)
        with open(out_path, "w") as f:
            json.dump(rows, f)

        if len(feats) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.15)
    return rows


if __name__ == "__main__":
    TMP_DIR.mkdir(exist_ok=True)
    print("fetching Placer County single-family/condo parcels...", file=sys.stderr)
    data = fetch_all(OUT_PATH)
    print(f"DONE. total rows: {len(data)} -> {OUT_PATH}", file=sys.stderr)
