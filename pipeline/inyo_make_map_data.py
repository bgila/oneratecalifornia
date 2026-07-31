"""
Slim Inyo County's full single-family estimate table down to the shared
cross-county map-data schema and write the committed CSV.

Mirrors 07_make_map_data_sfr.py's role for SF, but the output schema here
adds a "county" column (literal "Inyo" on every row) since this dataset is
meant to sit alongside other counties' map data rather than assume it's the
only county in the file the way SF's original single-county map did.

IMPORTANT: the "sqft" column here is LotSqFeet (parcel/lot size), NOT
building/living area -- Inyo's assessor data has no building-square-footage
field at all (see inyo_process.py's docstring for the full explanation).
This is NOT the same quantity as SF's "sqft" column. Documented again here,
and in data/inyo-methodology.json, so nobody downstream conflates the two.

Reads:  pipeline/tmp/inyo-full.csv (inyo_process.py)
Writes: data/inyo-map-data.csv (committed -- lat,lon,addr,sqft,assessed,market,subsidy,change,county)
"""
import csv
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
SRC = TMP_DIR / "inyo-full.csv"
OUT = DATA_DIR / "inyo-map-data.csv"

COUNTY = "Inyo"


def clean_addr(street, city):
    street = " ".join(street.split()).title()
    city = " ".join(city.split()).title()
    if city:
        return f"{street}, {city}"
    return street


def main():
    rows = list(csv.DictReader(open(SRC)))
    print("input rows:", len(rows))

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lat", "lon", "addr", "sqft", "assessed", "market", "subsidy", "change", "county"])
        for r in rows:
            w.writerow([
                round(float(r["lat"]), 5), round(float(r["lon"]), 5),
                clean_addr(r["addr"], r["city"]),
                int(float(r["lot_sqft"])),
                int(float(r["assessed"])),
                int(float(r["est_market_value"])),
                int(float(r["subsidy_vs_market_today"])),
                int(float(r["change_under_reform"])),
                COUNTY,
            ])

    print("wrote", OUT)


if __name__ == "__main__":
    main()
