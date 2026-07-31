"""
Build a small pool of San Francisco single-family homes that have very
likely been under the same ownership for at least 50 years, for the hero
section's "50 years of Prop 13" chart.

DataSF's sale-date field only goes back to the early 1980s (it's null for
anything not resold since before the records were digitized) -- so there's
no direct "years since purchase" field to filter on. Instead we use two
proxies together: built before the cutoff year, AND no recorded sale at
all (current_sales_date IS NULL). A home matching both has almost
certainly not changed hands in the digitized era, which given the ~1983
cutoff of that record-keeping means well over 40 years, and combined with
predating the cutoff year itself, comfortably clears 50.

For each home we only keep the handful of raw facts needed to reconstruct
its tax trajectory client-side (see map.js): current assessed value, sqft,
address, lat/lon, year built. The reconstruction itself (Prop 13's 2%/yr
cap run backward from today, scaled against the FRED house price index)
happens in the browser, not here -- see 13_fetch_hpi.py for the other half
of that data.

Reads:  nothing (hits DataSF network)
Writes: data/home-tax-histories.json
"""
import datetime
import json
import random
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "home-tax-histories.json"

BASE_URL = "https://data.sfgov.org/resource/wv5m-vpq2.json"
FIELDS = (
    "property_location,parcel_number,property_area,year_property_built,"
    "assessed_land_value,assessed_improvement_value,assessed_fixtures_value,the_geom"
)
POOL_SIZE = 50
MIN_SQFT = 400
MAX_SQFT = 6000
SEED = 42


def clean_addr(addr):
    # Same cleanup as 07_make_map_data_sfr.py, kept in sync by hand since this
    # is a small enough helper not to be worth sharing a module for.
    import re
    parts = addr.split()
    if parts and parts[0] == "0000":
        parts = parts[1:]
    s = " ".join(parts)
    s = re.sub(r'(?<=[A-Za-z])0*\d{2,4}$', '', s)
    s = re.sub(r'\s+0000$', '', s)
    s = re.sub(r'^0+(?=\d)', '', s)
    return s.strip().title()


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fetch(cutoff_year):
    params = {
        "$select": FIELDS,
        "closed_roll_year": "2025",
        "use_definition": "Single Family Residential",
        "$where": f"current_sales_date IS NULL AND year_property_built < '{cutoff_year}'",
        "$limit": 50000,
    }
    url = BASE_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read())


def main():
    cutoff_year = datetime.date.today().year - 50
    print(f"fetching SFR parcels built before {cutoff_year} with no recorded sale...")
    raw = fetch(cutoff_year)
    print(f"candidates: {len(raw)}")

    homes = []
    for r in raw:
        geom = r.get("the_geom")
        if not geom or geom.get("type") != "Point":
            continue
        lon, lat = geom["coordinates"]
        sqft = to_float(r.get("property_area"))
        if sqft < MIN_SQFT or sqft > MAX_SQFT:
            continue
        land = to_float(r.get("assessed_land_value"))
        impr = to_float(r.get("assessed_improvement_value"))
        fix = to_float(r.get("assessed_fixtures_value"))
        assessed = land + impr + fix
        if assessed <= 0:
            continue
        addr = clean_addr(r.get("property_location") or "")
        if not addr:
            continue
        year_built = r.get("year_property_built")
        homes.append({
            "addr": addr,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "sqft": round(sqft),
            "assessed": round(assessed),
            "year_built": int(year_built) if year_built else None,
        })

    print(f"usable homes: {len(homes)}")
    random.seed(SEED)
    pool = random.sample(homes, min(POOL_SIZE, len(homes)))
    pool.sort(key=lambda h: h["addr"])
    OUT.write_text(json.dumps(pool))
    print(f"wrote {len(pool)} homes -> {OUT}")


if __name__ == "__main__":
    main()
