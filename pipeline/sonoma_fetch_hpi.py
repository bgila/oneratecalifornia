"""
Fetch Sonoma County's historical house price index from FRED, for
appreciation-adjusting older sold comps to today's-equivalent price the same
way 13_fetch_hpi.py does for SF with data/sf-hpi.json.

Series ATNHPIUS06097A: "All-Transactions House Price Index for Sonoma
County, CA" (FIPS 06097), annual, no base-year normalization needed since
sonoma_process.py only ever uses ratios between two years in the series.

Tries FRED's plain CSV export first (fredgraph.csv, same endpoint
13_fetch_hpi.py uses). Falls back to scraping the "DATE/VALUE" observations
table out of FRED's series data page (fred.stlouisfed.org/data/<series>),
which returns the identical numbers via plain HTML and proved more reliable
against this endpoint during development. No API key required either way.

Reads:  nothing (hits FRED network)
Writes: data/sonoma-hpi.json  ({"1975": 13.21, ..., "2025": 259.39})
"""
import csv
import io
import json
import re
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "sonoma-hpi.json"

SERIES_ID = "ATNHPIUS06097A"
CSV_URL = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES_ID}"
TABLE_URL = f"https://fred.stlouisfed.org/data/{SERIES_ID}"
UA = "Mozilla/5.0 (compatible; oneratecalifornia-pipeline/1.0)"


def fetch_via_csv():
    req = urllib.request.Request(CSV_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    hpi = {}
    for row in reader:
        year = row["observation_date"][:4]
        value = row[SERIES_ID]
        if value in ("", "."):
            continue
        hpi[year] = float(value)
    return hpi


def fetch_via_table():
    req = urllib.request.Request(TABLE_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        html = resp.read().decode("utf-8")
    # Rows look like:
    #   <th scope="row" class="pe-5">2024-01-01</th>
    #   <td class="pe-5">258.12</td>
    pattern = re.compile(
        r'<th scope="row"[^>]*>(\d{4})-\d{2}-\d{2}</th>\s*<td[^>]*>\s*([\d.]+)\s*</td>'
    )
    hpi = {year: float(value) for year, value in pattern.findall(html)}
    return hpi


def with_retries(fn, label, attempts=4):
    for attempt in range(attempts):
        try:
            result = fn()
            if result:
                return result
            raise ValueError("empty result")
        except Exception as e:
            print(f"  {label} attempt {attempt}: {e}")
            time.sleep(3 * (attempt + 1))
    return None


def main():
    hpi = with_retries(fetch_via_csv, "CSV endpoint")
    if hpi:
        print(f"fetched via CSV endpoint: {len(hpi)} points")
    else:
        print("CSV endpoint exhausted retries, falling back to table scrape...")
        hpi = with_retries(fetch_via_table, "table scrape")
        if hpi:
            print(f"fetched via table scrape: {len(hpi)} points")

    if not hpi:
        raise RuntimeError("could not fetch Sonoma HPI from FRED by either method")

    print(f"years: {min(hpi)}-{max(hpi)} ({len(hpi)} data points)")
    OUT.write_text(json.dumps(hpi))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
