"""
Fetch San Francisco County's historical house price index from FRED, for the
hero chart's "market value" line (see 12_build_home_tax_histories.py for the
other half of that chart's data).

Series ATNHPIUS06075A: "All-Transactions House Price Index for San Francisco
County, CA" (FIPS 06075), annual, no base-year normalization needed since
the chart only ever uses ratios between two years in the series. FRED's
plain CSV export needs no API key.

Reads:  nothing (hits FRED network)
Writes: data/sf-hpi.json  ({"1975": 10.68, ..., "2025": 292.54})
"""
import csv
import io
import json
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "sf-hpi.json"

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=ATNHPIUS06075A"


def main():
    with urllib.request.urlopen(URL, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    hpi = {}
    for row in reader:
        year = row["observation_date"][:4]
        value = row["ATNHPIUS06075A"]
        if value in ("", "."):
            continue
        hpi[year] = float(value)
    print(f"years: {min(hpi)}-{max(hpi)} ({len(hpi)} data points)")
    OUT.write_text(json.dumps(hpi))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
