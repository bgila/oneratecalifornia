"""
Fetch each hero-chart sample home's real per-year assessed value, 2007-2025
(DataSF's digitized assessor rolls don't go back further than that), and
merge it into data/home-tax-histories.json as a "history" field.

hero-chart.js previously reconstructed a home's entire 1975-2025 trajectory
from a single current (2025) snapshot, applying Prop 13's 2%/yr cap
backward from the home's most recent recorded sale (or 1975, if none).
That's now only needed for 1975-2006, where no real per-year data exists;
2007-2025 uses these real assessed values directly, which can show real-
world deviations (exemptions, appeals, disaster relief, etc.) a pure 2%
projection can't. It also can't fully fix the "one sale" limitation --
current_sales_date on file is a single most-recent-sale field, so an
earlier pre-2007 sale before that isn't recoverable -- but a reset that
happened at any point since 2007 now shows up as a real jump in the data
instead of being invisibly smoothed away.

Reads:  data/home-tax-histories.json (12_build_home_tax_histories.py, for
        the pool of parcel_numbers to fetch)
Writes: data/home-tax-histories.json (adds a "history" field per home)
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_PATH = REPO / "data" / "home-tax-histories.json"

BASE_URL = "https://data.sfgov.org/resource/wv5m-vpq2.json"
FIELDS = "parcel_number,closed_roll_year,assessed_land_value,assessed_improvement_value,assessed_fixtures_value"
START_YEAR = 2007
END_YEAR = 2025


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fetch_history(parcel_numbers):
    """One query per year, filtered to just these parcels (100 parcels x 19
    years is small enough to not need pagination within a year)."""
    by_parcel = {pn: {} for pn in parcel_numbers}
    in_list = ",".join("'" + pn.replace("'", "''") + "'" for pn in parcel_numbers)
    for year in range(START_YEAR, END_YEAR + 1):
        params = {
            "$select": FIELDS,
            "closed_roll_year": str(year),
            "$where": f"parcel_number in ({in_list})",
            "$limit": 500,
        }
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    rows = json.loads(resp.read())
                break
            except Exception as e:
                print(f"  retry after error on {year}: {e}", file=sys.stderr)
                time.sleep(2)
        else:
            raise RuntimeError(f"failed at year={year}")
        for r in rows:
            total = (to_float(r.get("assessed_land_value"))
                     + to_float(r.get("assessed_improvement_value"))
                     + to_float(r.get("assessed_fixtures_value")))
            if total > 0:
                by_parcel[r["parcel_number"]][str(year)] = round(total)
        print(f"year {year}: {len(rows)} rows", file=sys.stderr)
        time.sleep(0.1)
    return by_parcel


def main():
    homes = json.loads(DATA_PATH.read_text())
    parcel_numbers = [h["parcel_number"] for h in homes if h.get("parcel_number")]
    print(f"fetching {START_YEAR}-{END_YEAR} history for {len(parcel_numbers)} parcels...", file=sys.stderr)
    history = fetch_history(parcel_numbers)

    covered = 0
    for h in homes:
        pn = h.get("parcel_number")
        h["history"] = history.get(pn, {})
        if h["history"]:
            covered += 1
    print(f"{covered}/{len(homes)} homes got at least one real history row", file=sys.stderr)

    DATA_PATH.write_text(json.dumps(homes))
    print(f"wrote -> {DATA_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
