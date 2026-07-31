"""
Slim the full Placer County single-family/condo estimate table down to just
the columns the live map needs, plus a literal "Placer" county column so
this county's rows can be told apart once merged with other counties' map
data.

Mirrors 07_make_map_data_sfr.py's job for San Francisco, with one added
column (county) since this map is being extended to more than one county.

Reads:  pipeline/tmp/placer-full.csv (placer_process.py)
Writes: data/placer-map-data.csv
"""
import csv
import re
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
SRC = TMP_DIR / "placer-full.csv"
OUT = DATA_DIR / "placer-map-data.csv"

COUNTY = "Placer"


def clean_addr(addr):
    """Title-case and collapse whitespace. placer_process.py already sources
    this from Placer's street-only FormattedSitus1 field (falling back to
    the combined SitusAddressFull only when that's missing), so this is
    normally just a street address with no city/state/zip -- but strip a
    trailing " CA 95678"-style state+zip anyway in case the fallback path
    was used for a given row.
    """
    s = " ".join(addr.split())
    s = re.sub(r'\s+[A-Z]{2}\s+\d{5}(-\d{4})?$', '', s)  # drop trailing " CA 95678" if present
    return s.strip().title()


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
