"""
Slim the full Riverside County estimate table down to just the columns the
live map needs, matching the exact schema the map-data CSV contract
requires (lat,lon,addr,sqft,assessed,market,subsidy,change,county), with a
literal "Riverside" county column so this file can be concatenated with
other counties' map-data CSVs.

Mirrors 07_make_map_data_sfr.py's role for SF, with one addition (the
"county" column) since this map now spans more than one county.

Reads:  pipeline/tmp/riverside-full.csv (riverside_03_process.py)
Writes: data/riverside-map-data.csv (lat,lon,addr,sqft,assessed,market,subsidy,change,county)
"""
import csv
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
SRC = TMP_DIR / "riverside-full.csv"
OUT = DATA_DIR / "riverside-map-data.csv"


def clean_addr(street, city):
    street = " ".join((street or "").split()).title()
    city = " ".join((city or "").split()).title()
    if street and city:
        return f"{street}, {city}"
    return street or city


def main():
    rows = list(csv.DictReader(open(SRC)))
    print("input rows:", len(rows))

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lat", "lon", "addr", "sqft", "assessed", "market", "subsidy", "change", "county"])
        for r in rows:
            w.writerow([
                round(float(r["lat"]), 5), round(float(r["lon"]), 5),
                clean_addr(r["address"], r["city"]),
                int(float(r["sqft"])),
                int(float(r["assessed_total"])),
                int(float(r["est_market_value"])),
                int(float(r["subsidy_vs_market_today"])),
                int(float(r["change_under_reform"])),
                "Riverside",
            ])

    print("wrote", OUT)


if __name__ == "__main__":
    main()
