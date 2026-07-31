"""
Fetch multi-year assessed-value history for Alameda County single-family
residential parcels, for jump-confirmed-comp detection (see
alameda_03_process.py).

Unlike SF (one Socrata table with a closed_roll_year column), Alameda
publishes each roll year as its OWN ArcGIS FeatureServer layer, e.g.
"Assessor Office Secured Tax Roll 2025 to 2026". Verified live (2026-07-31)
against https://services5.arcgis.com/ROBnTHSNjoZ2Wm1P/arcgis/rest/services :
the earliest one that exists is "2019 to 2020" -- there is a
"Deleted_Parcel_List_2018_to_2019" layer (parcels removed from that roll)
but NO "Secured_Tax_Roll_2018_to_2019" layer, so the roll table itself only
goes back to 2019-20. This is one year short of the assignment's "aim for
at least 2018-19"; every layer that actually exists in this window is
included instead of stopping short of it.

Each layer is labeled below by its *starting* year (e.g. the "2019 to 2020"
roll -> year 2019, "2025 to 2026" -> year 2025), matching SF's
closed_roll_year=2025 as "current". That gives 7 years of history:
2019-2025 inclusive.

Field-name and type notes (checked per-layer, not assumed from one sample):
  - Sort_Parcel, Total_Net_Value, Latest_Document_Date, Use_Code all exist
    on every one of the 7 layers with those exact names.
  - Their underlying ArcGIS field TYPE drifts across years (Total_Net_Value
    is esriFieldTypeInteger in one year, esriFieldTypeDouble in another,
    esriFieldTypeBigInteger in others; Use_Code is esriFieldTypeInteger in
    2019-2020/2020-2021 and esriFieldTypeString from 2021-2022 on). This
    doesn't matter for a `where=Use_Code='1100'` filter -- verified live
    that the quoted-string form returns the identical count as the
    unquoted numeric form against an integer-typed Use_Code field, so one
    query shape works for all 7 layers.
  - Latest_Document_Date's *field type* also drifts: esriFieldTypeDate
    (returned as epoch milliseconds) in 2019-2020 through 2022-2023,
    esriFieldTypeDateOnly (returned as a plain "YYYY-MM-DD" string) from
    2023-2024 on. Both are normalized to "YYYY-MM-DD" below.

Reads:  nothing (hits the network)
Writes: pipeline/tmp/alameda_history_2019_2025.json
"""
import datetime
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
OUT_PATH = TMP_DIR / "alameda_history_2019_2025.json"

ORG_BASE = "https://services5.arcgis.com/ROBnTHSNjoZ2Wm1P/arcgis/rest/services"

# year -> FeatureServer layer name, confirmed to exist via a live services listing.
ROLL_LAYERS = {
    2019: "Assessor_Office_Secured_Tax_Roll_2019_to_2020",
    2020: "Assessor_Office_Secured_Tax_Roll_2020_to_2021",
    2021: "Assessor_Office_Secured_Tax_Roll_2021_to_2022",
    2022: "Assessor_Office_Secured_Tax_Roll_2022_to_2023",
    2023: "Assessor_Office_Secured_Tax_Roll_2023_to_2024",
    2024: "Assessor_Office_Secured_Tax_Roll_2024_to_2025",
    2025: "Assessor_Office_Secured_Tax_Roll_2025_to_2026",
}
FIELDS = "Sort_Parcel,Total_Net_Value,Latest_Document_Date"
USE_CODE = "1100"
PAGE_SIZE = 2000


def normalize_doc_date(v):
    """Latest_Document_Date arrives as epoch-ms (older layers) or 'YYYY-MM-DD'
    (newer layers) -- normalize both to a plain 'YYYY-MM-DD' string."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.datetime.utcfromtimestamp(v / 1000).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(v, str):
        return v[:10] if v else None
    return None


def fetch_year(year, layer_name, page_size):
    base_url = f"{ORG_BASE}/{layer_name}/FeatureServer/0/query"
    rows = []
    offset = 0
    while True:
        params = {
            "where": f"Use_Code='{USE_CODE}'",
            "outFields": FIELDS,
            "orderByFields": "Sort_Parcel",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "json",
        }
        url = base_url + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    payload = json.loads(resp.read())
                if "error" in payload:
                    raise RuntimeError(payload["error"])
                break
            except Exception as e:
                print(f"  [{year}] retry {attempt} after error: {e}", file=sys.stderr)
                time.sleep(2)
        else:
            raise RuntimeError(f"failed at year={year} offset={offset}")

        batch = payload.get("features", [])
        for feat in batch:
            a = feat["attributes"]
            rows.append({
                "year": year,
                "parcel_number": a.get("Sort_Parcel"),
                "total_net_value": a.get("Total_Net_Value"),
                "doc_date": normalize_doc_date(a.get("Latest_Document_Date")),
            })
        if len(batch) < page_size:
            break
        offset += page_size
        time.sleep(0.2)
    return rows


def main():
    TMP_DIR.mkdir(exist_ok=True)
    all_rows = []
    for year in sorted(ROLL_LAYERS):
        layer_name = ROLL_LAYERS[year]
        print(f"fetching year={year} ({layer_name})...", file=sys.stderr)
        year_rows = fetch_year(year, layer_name, PAGE_SIZE)
        print(f"  year {year}: {len(year_rows)} rows", file=sys.stderr)
        all_rows.extend(year_rows)
        with open(OUT_PATH, "w") as f:
            json.dump(all_rows, f)
    print(f"TOTAL: {len(all_rows)} -> {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
