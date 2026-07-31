"""
Fetch the current Inyo County parcel snapshot from the county's public
ArcGIS REST API (gisdata.inyo.gov's hosted FeatureServer), and the latest
ZHVI (Zillow Home Value Index) single-family-homes-by-ZIP time series from
Zillow Research.

Inyo County (rural Eastern Sierra: Bishop, Big Pine, Independence, Lone
Pine, Death Valley) has no internal sale-price or reassessment-history
signal available (see inyo_process.py for why), so unlike SF's pipeline
this fetch pulls TWO independent public sources:

1. Parcels: services.arcgis.com/0jRlQ17Qmni5zEMr/.../ParcelsPublic/
   FeatureServer/0 -- a single current-year snapshot (AssessYear, LandVal,
   ImproveVal, TotalVal, ExemptVal, FixtureVal, UseCode, PropClass,
   LotSqFeet, situs address, ZIP, polygon geometry). No sale/document-date
   field and no multi-year history endpoint exist for this county, so the
   SF-style "jump-confirmed comp" approach is not possible here at all.
   Only ~16,000 total parcels countywide, well under the API's 2000-
   record page cap, so this pages through with resultOffset.

   IMPORTANT caveat verified live against this API: LotSqFeet (and the
   GIS-computed SqFeet_gis, which is numerically almost identical to
   LotSqFeet) is the PARCEL/LOT area, not the building/living area -- e.g.
   APN 0010110600, a single "SFR (SINGLE FAMILY RESIDENCE)" parcel at 751
   Home St, Bishop, carries LotSqFeet=23,600 (roughly half an acre), which
   is obviously a lot size, not a home's floor area. There is NO building-
   square-footage field anywhere in this layer's schema (checked the full
   field list: OBJECTID, PIN, APN, GP, Inyo_Zonin, AssessYear, PersProp,
   LandVal, ImproveVal, TotalVal, ExemptVal, FixtureVal, UseCode,
   TxRateArea, LegalDescr, PropClass, ParcAdd1/City/State/ZIP, Neighborhd,
   MultiOwner, Width, Depth, LotSqFeet, LotAcres, LotType, Acres_gis,
   SqFeet_gis, RECORD_DOC_INFO, Taxmaps, enc_docs, GlobalID, Shape__Area,
   Shape__Length -- nothing else describes the structure). This is why
   inyo_process.py cannot do a $/sqft-of-building estimate directly from
   this dataset alone (see step 2 there for how ZHVI + implied ZIP-level
   $/sqft substitutes for it).

2. ZHVI: Zillow Research publishes a free, unauthenticated CSV, "ZHVI
   Single-Family Homes Time Series ($), Smoothed, Seasonally Adjusted, by
   ZIP Code" -- verified live on https://www.zillow.com/research/data/ by
   setting that page's own Data Type / Geography dropdowns and reading the
   resulting download URL out of the DOM (the page computes it
   client-side; it is not guessable/stable across Zillow's own past
   filename changes, hence fetching it this way instead of hardcoding a
   remembered URL). Filtered here to State=CA rows only in
   inyo_process.py; the full national file is ~120MB so it is not
   committed anywhere, only cached in pipeline/tmp/ (gitignored).

Reads:  nothing (hits the network)
Writes: pipeline/tmp/inyo_parcels_raw.json
        pipeline/tmp/inyo_zhvi_raw.csv
"""
import csv
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
PARCELS_OUT = TMP_DIR / "inyo_parcels_raw.json"
ZHVI_OUT = TMP_DIR / "inyo_zhvi_raw.csv"

ARCGIS_BASE = (
    "https://services.arcgis.com/0jRlQ17Qmni5zEMr/arcgis/rest/services/"
    "ParcelsPublic/FeatureServer/0/query"
)
FIELDS = (
    "APN,PropClass,UseCode,LandVal,ImproveVal,TotalVal,ExemptVal,FixtureVal,"
    "LotSqFeet,LotAcres,ParcAdd1,ParcCity,ParcState,ParcZIP,Neighborhd,AssessYear"
)
PAGE_SIZE = 2000  # this service's maxRecordCount

# Zillow Research's live "Download" link for Data Type = "ZHVI Single-Family
# Homes Time Series ($)" (implicitly smoothed/seasonally-adjusted -- that's
# the only cut Zillow offers at ZIP granularity) x Geography = "ZIP Code",
# read directly out of the research/data page's own dropdown-driven <select>
# element on 2026-07-31. Zillow documents that it occasionally changes CSV
# paths, so if this 404s, re-derive it from https://www.zillow.com/research/data/
# (Data Type dropdown -> "ZHVI Single-Family Homes Time Series ($)",
# Geography dropdown -> "ZIP Code" -> Download).
ZHVI_URL = "https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_sfr_tier_0.33_0.67_sm_sa_month.csv"


def fetch_parcels():
    rows = []
    offset = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": FIELDS,
            "returnGeometry": "false",
            "returnCentroid": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "f": "json",
        }
        url = ARCGIS_BASE + "?" + urllib.parse.urlencode(params)
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

        feats = batch.get("features", [])
        rows.extend(feats)
        print(f"  offset={offset} got={len(feats)} total_so_far={len(rows)}", file=sys.stderr)
        with open(PARCELS_OUT, "w") as f:
            json.dump(rows, f)

        if len(feats) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.2)
    return rows


def fetch_zhvi():
    print(f"fetching ZHVI from {ZHVI_URL} ...", file=sys.stderr)
    with urllib.request.urlopen(ZHVI_URL, timeout=120) as resp:
        data = resp.read()
    with open(ZHVI_OUT, "wb") as f:
        f.write(data)
    # sanity check + report CA/Inyo coverage immediately
    reader = csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace")))
    rows = list(reader)
    ca_rows = [r for r in rows if r.get("State") == "CA"]
    inyo_rows = [r for r in ca_rows if "Inyo" in (r.get("CountyName") or "")]
    print(f"ZHVI total rows: {len(rows)}, CA rows: {len(ca_rows)}, Inyo County rows: {len(inyo_rows)}",
          file=sys.stderr)
    for r in inyo_rows:
        print(f"    ZIP {r['RegionName']} ({r.get('City')}): county has ZHVI coverage", file=sys.stderr)


if __name__ == "__main__":
    TMP_DIR.mkdir(exist_ok=True)
    print("fetching Inyo County parcel snapshot from ArcGIS FeatureServer...", file=sys.stderr)
    parcels = fetch_parcels()
    print(f"DONE parcels. total rows: {len(parcels)} -> {PARCELS_OUT}", file=sys.stderr)
    fetch_zhvi()
    print(f"DONE ZHVI -> {ZHVI_OUT}", file=sys.stderr)
