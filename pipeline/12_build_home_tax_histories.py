"""
Build a small pool of San Francisco single-family homes that have very
likely gone at least ~50 years without their assessed value resetting to
market rate, for the hero section's "50 years of Prop 13" chart.

DataSF's sale-date field only goes back to ~1983 (it's null for anything
not resold since before records were digitized), so "no recorded sale at
all" was our first proxy for long tenure -- but that alone was misleading:
it silently excluded every home whose most recent recorded event was a
non-arms-length transfer (inheritance, parent-child exclusion, trust/family
transfers) that keeps the OLD assessed value on the books despite having a
sale date on file. Those homes belong in the pool just as much as ones
with no sale record at all, and a real random sample of long-frozen homes
should include some of them -- see 03_process_sfr.py's comp-detection logic
for the same non-arms-length-transfer concept applied elsewhere.

So the actual qualifying signal here is the assessed value itself: built
before the cutoff year, AND assessed $/sqft well below typical SF market
value (empirically, homes with no sale on file cluster under ~$150-280/sqft
vs. ~$730/sqft median for homes with a real market-rate sale on file) --
that directly captures "this assessment looks stale" regardless of whether
a family-transfer sale date happens to be on file. Homes that DO have a
sale date are additionally required to have sold long enough ago (<= 2000)
that a low psf isn't just a very recent oddball transaction.

For each home we only keep the handful of raw facts needed to reconstruct
its tax trajectory client-side (see hero-chart.js): current assessed
value, sqft, address, lat/lon, year built. The reconstruction itself
(Prop 13's 2%/yr cap run backward from today, scaled against the FRED
house price index) happens in the browser -- see 13_fetch_hpi.py for the
other half of that data.

Reads:  nothing (hits DataSF network)
Writes: data/home-tax-histories.json
"""
import datetime
import json
import random
import re
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "home-tax-histories.json"

BASE_URL = "https://data.sfgov.org/resource/wv5m-vpq2.json"
FIELDS = (
    "property_location,parcel_number,property_area,year_property_built,"
    "current_sales_date,assessed_land_value,assessed_improvement_value,"
    "assessed_fixtures_value,the_geom"
)
POOL_SIZE = 50
MIN_SQFT = 400
MAX_SQFT = 6000
PSF_CUTOFF = 350       # assessed $/sqft below this looks like a stale, long-frozen assessment
SALE_YEAR_CUTOFF = 2000  # a recorded sale is only allowed if it's this old or older
SEED = 42


def clean_addr(addr):
    # Same cleanup as 07_make_map_data_sfr.py, kept in sync by hand since this
    # is a small enough helper not to be worth sharing a module for.
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
    """Page through the Socrata API -- built<cutoff_year alone matches ~110k
    rows, well past a single request's $limit, so this needs the same
    offset loop as 01_fetch_sfr_snapshot.py."""
    page_size = 50000
    rows = []
    offset = 0
    while True:
        params = {
            "$select": FIELDS,
            "closed_roll_year": "2025",
            "use_definition": "Single Family Residential",
            "$where": f"year_property_built < '{cutoff_year}'",
            "$order": "parcel_number",
            "$limit": page_size,
            "$offset": offset,
        }
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=60) as resp:
            batch = json.loads(resp.read())
        rows.extend(batch)
        print(f"  offset={offset} got={len(batch)} total_so_far={len(rows)}")
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def main():
    cutoff_year = datetime.date.today().year - 50
    print(f"fetching SFR parcels built before {cutoff_year}...")
    raw = fetch(cutoff_year)
    print(f"candidates: {len(raw)}")

    homes = []
    skipped_recent_sale = 0
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
        if assessed / sqft >= PSF_CUTOFF:
            continue
        sale_date = r.get("current_sales_date")
        sale_year = int(sale_date[:4]) if sale_date else None
        if sale_year is not None and sale_year > SALE_YEAR_CUTOFF:
            skipped_recent_sale += 1
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
            "sale_year": sale_year,
        })

    print(f"usable homes: {len(homes)} (skipped {skipped_recent_sale} low-psf but recently sold)")
    with_sale = sum(1 for h in homes if h["sale_year"])
    print(f"  {with_sale} have a recorded sale on file, {len(homes) - with_sale} don't")

    random.seed(SEED)
    pool = random.sample(homes, min(POOL_SIZE, len(homes)))
    pool.sort(key=lambda h: h["addr"])
    pool_with_sale = sum(1 for h in pool if h["sale_year"])
    print(f"pool: {len(pool)} homes, {pool_with_sale} with a recorded sale on file")
    OUT.write_text(json.dumps(pool))
    print(f"wrote {len(pool)} homes -> {OUT}")


if __name__ == "__main__":
    main()
