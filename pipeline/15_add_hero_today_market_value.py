"""
Add each hero-chart sample home's current estimated market value -- from the
same nearest-comps model that powers the "Individual homes" map (03_process_
sfr.py) -- to data/home-tax-histories.json, as "today_market_value".

hero-chart.js previously anchored its "market value, today's rate" line on
the home's real ASSESSED value at the earliest year in history (2007, or the
current snapshot as a fallback) and scaled it by the FRED house price index.
That's wrong: a long-held home's assessed value is exactly the number Prop 13
suppresses below market value, so anchoring on it just propagates that
suppression into a line that's supposed to represent true market value,
sometimes producing an obviously-too-low result. Anchoring on the map's own
comps-based market-value estimate for today, then projecting backward via
the HPI ratio, is consistent with what the rest of the site already claims
this home is worth.

Reads:  data/home-tax-histories.json         (12_build_home_tax_histories.py)
        pipeline/tmp/sf-citywide-sfr-full.csv (03_process_sfr.py)
Writes: data/home-tax-histories.json (adds a "today_market_value" field)
"""
import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_PATH = REPO / "data" / "home-tax-histories.json"
PIPELINE_DIR = Path(__file__).resolve().parent
SFR_FULL_CSV = PIPELINE_DIR / "tmp" / "sf-citywide-sfr-full.csv"


def main():
    homes = json.loads(DATA_PATH.read_text())

    market_by_parcel = {}
    with open(SFR_FULL_CSV) as f:
        for row in csv.DictReader(f):
            market_by_parcel[row["parcel_number"]] = round(float(row["est_market_value"]))

    missing = 0
    for h in homes:
        val = market_by_parcel.get(h.get("parcel_number"))
        if val is None:
            missing += 1
            val = h["assessed"]  # fallback: no comps-based estimate on file
        h["today_market_value"] = val

    print(f"{len(homes) - missing}/{len(homes)} homes matched to a comps-based estimate; "
          f"{missing} fell back to assessed value", file=sys.stderr)
    DATA_PATH.write_text(json.dumps(homes))
    print(f"wrote -> {DATA_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
