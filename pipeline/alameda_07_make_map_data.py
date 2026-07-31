"""
Slim the full Alameda County single-family estimate table down to just the
columns the live map needs, matching the exact schema used across counties.

Reads:  pipeline/tmp/alameda-full.csv (alameda_03_process.py)
Writes: data/alameda-map-data.csv (lat,lon,addr,sqft,assessed,market,subsidy,change,county)

Note on "sqft": this is parcel LOT square footage, not building square
footage -- Alameda's public assessor data doesn't publish building square
footage anywhere (see alameda_03_process.py's docstring and
data/alameda-methodology.json's "sqft_caveat" for detail). It is carried
through unchanged from the full table.

Note on "addr": Alameda's situs address fields (street number + street name
+ unit) arrive already reasonably clean from the assessor (unlike SF's
fixed-width padded strings), so this is just a title-case pass, no
zero-padding cleanup needed.
"""
import csv
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
SRC = TMP_DIR / "alameda-full.csv"
OUT = DATA_DIR / "alameda-map-data.csv"

COUNTY = "Alameda"


def clean_addr(addr):
    return " ".join(addr.split()).title()


def main():
    rows = list(csv.DictReader(open(SRC)))
    print("input rows:", len(rows))

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lat", "lon", "addr", "sqft", "assessed", "market", "subsidy", "change", "county"])
        for r in rows:
            w.writerow([
                round(float(r["lat"]), 5), round(float(r["lon"]), 5),
                clean_addr(r["address"]),
                int(float(r["lot_sqft"])),
                int(float(r["assessed_total"])),
                int(float(r["est_market_value"])),
                int(float(r["subsidy_vs_market_today"])),
                int(float(r["change_under_reform"])),
                COUNTY,
            ])

    print("wrote", OUT)


if __name__ == "__main__":
    main()
