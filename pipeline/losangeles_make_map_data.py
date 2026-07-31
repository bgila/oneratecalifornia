"""
Slim the full LA County single-family estimate table down to just the
columns the live map needs, matching 07_make_map_data_sfr.py's shape plus a
"county" column (this build covers multiple counties now, unlike SF's
single-county map data).

Reads:  pipeline/tmp/losangeles-full.csv (losangeles_process.py)
Writes: data/losangeles-map-data.csv (lat,lon,addr,sqft,assessed,market,subsidy,change,county)
"""
import csv
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
SRC = TMP_DIR / "losangeles-full.csv"
OUT = DATA_DIR / "losangeles-map-data.csv"

COUNTY = "Los Angeles"


def main():
    rows = list(csv.DictReader(open(SRC)))
    print("input rows:", len(rows))

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lat", "lon", "addr", "sqft", "assessed", "market", "subsidy", "change", "county"])
        for r in rows:
            w.writerow([
                round(float(r["lat"]), 5), round(float(r["lon"]), 5),
                r["address"],
                int(float(r["sqft"])),
                int(float(r["assessed_total"])),
                int(float(r["est_market_value"])),
                int(float(r["subsidy_vs_market_today"])),
                int(float(r["change_under_reform"])),
                COUNTY,
            ])

    print("wrote", OUT)


if __name__ == "__main__":
    main()
