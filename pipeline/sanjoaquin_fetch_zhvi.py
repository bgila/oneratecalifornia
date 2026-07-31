"""
Fetch Zillow Research's ZHVI (Zillow Home Value Index) time series for
single-family homes, by ZIP code -- the external benchmark San Joaquin
County's market-value estimate is anchored on (see sanjoaquin_process.py for
why: unlike SF, this county's assessor data has no sale-date field at all, so
there's no internal way to tell a fresh assessed value from a decades-stale
one; ZHVI substitutes an outside, independently-updated read on what homes in
a given ZIP are worth right now).

Series: "ZHVI Single-Family Homes Time Series ($), Smoothed, Seasonally
Adjusted, By ZIP Code" from https://www.zillow.com/research/data/ (under the
"Home Values" tab). Zillow's research-data landing page is behind a bot
challenge (PerimeterX) that blocks plain HTTP fetches, so the exact filename
below was confirmed by resolving Zillow's actual public CSV bucket directly:

    https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_sfr_tier_0.33_0.67_sm_sa_month.csv

filename anatomy (per Zillow's ZHVI user guide): "zhvi" = the index;
"uc_sfr" = single-family homes only (as opposed to "uc_sfrcondo", which is
Zillow's *All Homes* series including condos -- NOT what we want here);
"tier_0.33_0.67" = the middle price tier, i.e. Zillow's standard "typical
home" cut; "sm_sa" = smoothed, seasonally adjusted; "month" = monthly series.
This was verified live (HTTP 200, ~122MB, Last-Modified in the current
month) rather than assumed from memory, and its ZIP/CountyName columns were
spot-checked to include San Joaquin County ZIPs (e.g. 95202 -> Stockton, CA).

IMPORTANT CAVEAT (see also data/sanjoaquin-methodology.json): ZHVI is itself
a smoothed, seasonally-adjusted *model* Zillow fits to a blend of its own
estimated home values and observed transactions -- not a raw sale-price feed.
Anchoring San Joaquin's market-value estimate on it is one layer more removed
from ground truth than the jump-confirmed-comps approach used for SF (which
anchors on the county's own confirmed reassessment events). This is flagged
explicitly rather than presented as equally rigorous.

Only the rows for California ZIPs are kept (the full national file is ~120MB
and covers ZIPs never relevant to this site); the county-level filtering down
to just San Joaquin's own ZIPs happens in sanjoaquin_process.py, once we know
which ZIPs actually appear in the parcel data.

Reads:  nothing (hits the network)
Writes: pipeline/tmp/sanjoaquin_zhvi_ca.json  ({zip: {"latest_value": v, "latest_month": "YYYY-MM-DD", "city":..., "county":...}})
"""
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
OUT_PATH = TMP_DIR / "sanjoaquin_zhvi_ca.json"

URL = "https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_sfr_tier_0.33_0.67_sm_sa_month.csv"
UA = "Mozilla/5.0 (compatible; oneratecalifornia-pipeline/1.0)"


def main():
    print(f"fetching {URL} ...", file=sys.stderr)
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames
    # date columns look like "2000-01-31" -- 10 chars, 4-digit year then a dash
    date_cols = [c for c in fieldnames if len(c) == 10 and c[:4].isdigit() and c[4] == "-"]

    out = {}
    total = 0
    for row in reader:
        total += 1
        if row.get("State") != "CA":
            continue
        zip5 = row["RegionName"].strip().zfill(5)
        # walk backward from the most recent month to find the latest non-blank value
        latest_val, latest_month = None, None
        for col in reversed(date_cols):
            v = row.get(col)
            if v not in (None, ""):
                latest_val = float(v)
                latest_month = col
                break
        if latest_val is None:
            continue
        out[zip5] = {
            "latest_value": latest_val,
            "latest_month": latest_month,
            "city": row.get("City"),
            "county": row.get("CountyName"),
        }
    print(f"CA zips with a ZHVI value: {len(out)} (of {total} total rows in national file)", file=sys.stderr)
    sj = {z: v for z, v in out.items() if v.get("county") == "San Joaquin County"}
    print(f"  of which San Joaquin County: {len(sj)}", file=sys.stderr)
    TMP_DIR.mkdir(exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f)
    print(f"DONE -> {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
