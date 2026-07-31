"""
Slim the full Sonoma County per-parcel estimate table down to just the
columns the live map needs, matching 07_make_map_data_sfr.py's shape plus a
"county" column (since this map data will eventually be combined with other
counties').

Reads:  pipeline/tmp/sonoma-full.csv (sonoma_process.py)
Writes: data/sonoma-map-data.csv
"""
import csv
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
SRC = TMP_DIR / "sonoma-full.csv"
OUT = DATA_DIR / "sonoma-map-data.csv"

COUNTY = "Sonoma"


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
