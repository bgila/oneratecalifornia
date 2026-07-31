"""
Fetch each sampled parcel's PRIME_BASE_YEAR from CREST_GENERAL -- this is
the signal riverside_03_process.py uses to detect probable market-reset
comps, standing in for the "multi-year assessed-value history" that
02_fetch_sfr_history.py provides for San Francisco.

Why this instead of a real history table: preliminary research assumed
CREST_TAXYEAR held a multi-year series (like SF's Socrata roll history),
which would have let us replicate SF's exact "assessed value jumped >=8%
year-over-year" jump-detection. Live queries against CREST_TAXYEAR (done
while building riverside_01_fetch_snapshot.py) disproved that: a
returnCountOnly query for TAX_YEAR=2020 through 2028 returns zero rows
for every year except 2027 -- the table holds only the single current
roll, not a history. So there is no year-over-year series to compute a
jump from at all, regardless of the sale-date question.

PRIME_BASE_YEAR is the county's own recorded substitute: it's the year
the parcel's current Prop 13 base-year value was established (reset by a
change of ownership or completed new construction -- exactly the kind of
event SF's jump-detection is trying to infer indirectly). A parcel whose
PRIME_BASE_YEAR is recent is therefore a strong candidate for "the
current assessed value is close to its market value at last reset,"
without needing to compute a jump ratio at all.

Confirmed live (see riverside_01_fetch_snapshot.py's docstring and the
methodology JSON) that no field anywhere in this schema
(CREST_GENERAL, CREST_PROPERTY_CHAR, CREST_RECORDED_BOOK, CREST_TAXYEAR,
PARCELS_CREST) records a sale date or recording date. So, per the
assigned methodology, comps here are NOT sale-confirmed -- PRIME_BASE_YEAR
resets from non-arms-length events (Prop 19 parent-child/trust transfers,
new construction, assessment appeals/corrections) are indistinguishable
from real sales in this data and will occasionally leak into the comp
pool. See riverside_03_process.py for how the comp window is chosen to
bound this.

Reads:  pipeline/tmp/riverside_snapshot_raw.json (riverside_01_fetch_snapshot.py)
Writes: pipeline/tmp/riverside_baseyear_raw.json  ({pin: prime_base_year})
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
IN_SNAPSHOT = TMP_DIR / "riverside_snapshot_raw.json"
OUT_PATH = TMP_DIR / "riverside_baseyear_raw.json"

MAPSERVER = "https://gis.countyofriverside.us/arcgis_mapping/rest/services/OpenData/Assessor/MapServer"
CREST_GENERAL = f"{MAPSERVER}/70/query"
JOIN_CHUNK_SIZE = 250


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; oneratecalifornia-pipeline/1.0)"}


def http_query(url, params, method="GET", timeout=60, retries=4):
    body = urllib.parse.urlencode(params, doseq=True).encode()
    for attempt in range(retries):
        try:
            if method == "GET":
                req = urllib.request.Request(url + "?" + body.decode(), headers=HEADERS)
            else:
                req = urllib.request.Request(url, data=body, method="POST", headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"  retry {attempt} after error: {e}", file=sys.stderr)
            time.sleep(2)
    raise RuntimeError(f"failed query to {url} params={params}")


def fetch_baseyear_chunk(pins):
    in_list = ",".join(f"'{p}'" for p in pins)
    params = {
        "where": f"PIN IN ({in_list})",
        "outFields": "PIN,PRIME_BASE_YEAR",
        "returnGeometry": "false",
        "f": "json",
    }
    data = http_query(CREST_GENERAL, params, method="POST")
    out = {}
    for f in data.get("features", []):
        a = f["attributes"]
        out[a["PIN"]] = a.get("PRIME_BASE_YEAR")
    return out


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main():
    snapshot = json.load(open(IN_SNAPSHOT))
    pins = [p["pin"] for p in snapshot]
    print(f"fetching PRIME_BASE_YEAR for {len(pins)} sampled parcels...", file=sys.stderr)

    # Parallelized the same way as riverside_01_fetch_snapshot.py: independent
    # chunked join queries, bottlenecked on request latency rather than server
    # throughput, so a thread pool gives a large wall-clock speedup over one
    # chunk at a time.
    result = {}
    chunk_list = list(chunks(pins, JOIN_CHUNK_SIZE))
    done = 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_baseyear_chunk, c): c for c in chunk_list}
        for fut in as_completed(futures):
            result.update(fut.result())
            done += 1
            if done % 40 == 0:
                print(f"  chunk {done}/{len(chunk_list)}: {len(result)} so far", file=sys.stderr)
                with open(OUT_PATH, "w") as f:
                    json.dump(result, f)

    with open(OUT_PATH, "w") as f:
        json.dump(result, f)
    print(f"DONE. {len(result)} rows -> {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
