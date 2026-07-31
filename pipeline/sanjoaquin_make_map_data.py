"""
Slim San Joaquin's full per-parcel estimate table down to the columns the
live map needs, matching the shape of data/sf-map-data.csv but with an added
"county" column (this site now covers more than one county).

Reads:  pipeline/tmp/sanjoaquin-full.csv (sanjoaquin_process.py)
Writes: data/sanjoaquin-map-data.csv (committed -- lat,lon,addr,sqft,assessed,market,subsidy,change,county)
"""
import csv
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
SRC = TMP_DIR / "sanjoaquin-full.csv"
OUT = DATA_DIR / "sanjoaquin-map-data.csv"

COUNTY = "San Joaquin"


def main():
    rows = list(csv.DictReader(open(SRC)))
    print("input rows:", len(rows))

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lat", "lon", "addr", "sqft", "assessed", "market", "subsidy", "change", "county"])
        for r in rows:
            w.writerow([
                round(float(r["lat"]), 5), round(float(r["lon"]), 5),
                r["addr"],
                int(float(r["sqft"])),
                int(float(r["assessed"])),
                int(float(r["market"])),
                int(float(r["subsidy"])),
                int(float(r["change"])),
                COUNTY,
            ])

    print("wrote", OUT)


if __name__ == "__main__":
    main()
