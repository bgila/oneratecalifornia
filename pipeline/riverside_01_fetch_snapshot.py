"""
Fetch the current-roll snapshot of Riverside County single-family
residential parcels from the county's public ArcGIS REST API (no key
required): gis.countyofriverside.us/arcgis_mapping/rest/services/OpenData/
Assessor/MapServer.

Riverside publishes assessor data as four joinable tables (by PIN/APN)
plus a parcel geometry layer, rather than DataSF's single flat Socrata
table:
  - PARCELS_CREST (layer 50): APN, CLASS_CODE, situs address, parcel
    polygon geometry. Used here as the driving table for scope (which
    parcels are "Single Family Dwelling") and for address/geometry.
  - CREST_PROPERTY_CHAR (table 80): LIVING_AREA (sqft), YEAR_BUILT,
    BEDROOM_COUNT, BATH_COUNT. A parcel can have >1 row (accessory
    buildings); we keep the row with the largest LIVING_AREA as the main
    dwelling.
  - CREST_TAXYEAR (table 100): LAND + STRUCTURES + LIVING_IMPROVEMENTS
    for the current assessment. IMPORTANT: live queries against this
    table (see below) show it holds only a SINGLE tax year (2027, i.e.
    the current FY2026-27 roll) for all ~1.01M rows countywide -- there
    is no multi-year series here, contrary to what preliminary scouting
    assumed. This is confirmed by directly querying TAX_YEAR=2020..2028
    and getting zero rows for every year except 2027. See
    riverside_02_fetch_baseyear.py and riverside_03_process.py for how
    this changes the comp-detection approach.

Scope decision: Riverside has 846,251 total parcels in PARCELS_CREST, of
which 580,109 carry CLASS_CODE='Single Family Dwelling' (confirmed via a
live returnCountOnly query). An earlier version of this pipeline took a
bounded systematic sample instead of the full 580,109 (every 5th parcel
in APN order, ~116,000 parcels, "for tractability"). Per an explicit
follow-up request for full county coverage, this version fetches and
joins the ENTIRE 580,109-parcel Single Family Dwelling universe across
the 3 rate-limited tables (each capped at maxRecordCount=2000 per
request, so this just means ~5x the pagination/chunking of the sampled
run -- same join logic, same chunk size, no sampling step). Expect this
run to take roughly 5x as long as the sampled run.

Reads:  nothing (hits the network)
Writes: pipeline/tmp/riverside_snapshot_raw.json   (enriched full-county snapshot)
        pipeline/tmp/riverside_scope_meta.json     (scope metadata, consumed by
                                                     03_process for the
                                                     methodology doc)
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
OUT_SNAPSHOT = TMP_DIR / "riverside_snapshot_raw.json"
OUT_SCOPE_META = TMP_DIR / "riverside_scope_meta.json"

MAPSERVER = "https://gis.countyofriverside.us/arcgis_mapping/rest/services/OpenData/Assessor/MapServer"
PARCELS_CREST = f"{MAPSERVER}/50/query"
PROPERTY_CHAR = f"{MAPSERVER}/80/query"
TAXYEAR = f"{MAPSERVER}/100/query"

CLASS_CODE_SFR = "Single Family Dwelling"
PAGE_SIZE = 2000  # ArcGIS server's maxRecordCount for this MapServer
JOIN_CHUNK_SIZE = 250  # PINs per "IN (...)" join query


# The county's ArcGIS server 403s requests carrying Python's default
# "Python-urllib/3.x" User-Agent (a common WAF rule); a normal browser-like
# UA gets through fine.
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


def fetch_sfr_count():
    params = {"where": f"CLASS_CODE='{CLASS_CODE_SFR}'", "returnCountOnly": "true", "f": "json"}
    return http_query(PARCELS_CREST, params).get("count", 0)


def fetch_sfr_page(offset):
    params = {
        "where": f"CLASS_CODE='{CLASS_CODE_SFR}'",
        "outFields": "APN,SITUS_STREET,CITY,ZIP_CODE",
        "returnGeometry": "false",
        "orderByFields": "APN",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
        "f": "json",
    }
    return http_query(PARCELS_CREST, params).get("features", [])


def fetch_full_sfr_list():
    """Page through PARCELS_CREST for every Single Family Dwelling parcel,
    lightweight fields only (no geometry yet -- that's fetched later). ArcGIS
    supports random-access offset paging, so once the total count is known
    every page can be requested concurrently instead of one at a time."""
    total = fetch_sfr_count()
    offsets = list(range(0, total, PAGE_SIZE))
    print(f"  total SFR count: {total} ({len(offsets)} pages)", file=sys.stderr)
    rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_sfr_page, off): off for off in offsets}
        for fut in as_completed(futures):
            rows.extend(fut.result())
            done += 1
            if done % 40 == 0:
                print(f"  list page {done}/{len(offsets)}: {len(rows)} so far", file=sys.stderr)
    return rows


def polygon_centroid(ring):
    """Area-weighted centroid of a simple polygon ring [[x,y], ...] (closed).
    Falls back to a plain vertex average for degenerate (near-zero-area)
    rings, which occasionally happens for sliver/odd-shaped parcels."""
    a_sum = cx_sum = cy_sum = 0.0
    n = len(ring)
    for i in range(n - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        cross = x0 * y1 - x1 * y0
        a_sum += cross
        cx_sum += (x0 + x1) * cross
        cy_sum += (y0 + y1) * cross
    area = a_sum / 2.0
    if abs(area) < 1e-9:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    return cx_sum / (6 * area), cy_sum / (6 * area)


def fetch_geometry_chunk(apns):
    in_list = ",".join(f"'{a}'" for a in apns)
    params = {
        "where": f"APN IN ({in_list})",
        "outFields": "APN",
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": "6",
        "f": "json",
    }
    data = http_query(PARCELS_CREST, params, method="POST")
    out = {}
    for f in data.get("features", []):
        apn = f["attributes"]["APN"]
        rings = (f.get("geometry") or {}).get("rings")
        if not rings:
            continue
        lon, lat = polygon_centroid(rings[0])
        out[apn] = (lat, lon)
    return out


def fetch_property_char_chunk(pins):
    in_list = ",".join(f"'{p}'" for p in pins)
    params = {
        "where": f"PIN IN ({in_list})",
        "outFields": "PIN,LIVING_AREA,YEAR_BUILT,BEDROOM_COUNT,BATH_COUNT",
        "returnGeometry": "false",
        "f": "json",
    }
    data = http_query(PROPERTY_CHAR, params, method="POST")
    best = {}
    for f in data.get("features", []):
        a = f["attributes"]
        pin = a["PIN"]
        area = a.get("LIVING_AREA") or 0
        if pin not in best or area > (best[pin].get("LIVING_AREA") or 0):
            best[pin] = a
    return best


def fetch_taxyear_chunk(pins):
    in_list = ",".join(f"'{p}'" for p in pins)
    params = {
        "where": f"PIN IN ({in_list})",
        "outFields": "PIN,LAND,STRUCTURES,LIVING_IMPROVEMENTS",
        "returnGeometry": "false",
        "f": "json",
    }
    data = http_query(TAXYEAR, params, method="POST")
    totals = defaultdict(float)
    for f in data.get("features", []):
        a = f["attributes"]
        pin = a["PIN"]
        totals[pin] += (a.get("LAND") or 0) + (a.get("STRUCTURES") or 0) + (a.get("LIVING_IMPROVEMENTS") or 0)
    return totals


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main():
    TMP_DIR.mkdir(exist_ok=True)

    print("fetching full Single Family Dwelling parcel list (lightweight)...", file=sys.stderr)
    full_rows = fetch_full_sfr_list()
    total_sfr = len(full_rows)
    print(f"total SFR parcels countywide: {total_sfr}", file=sys.stderr)

    by_apn = {}
    for f in full_rows:
        a = f["attributes"]
        by_apn[a["APN"]] = a
    target_apns = sorted(by_apn.keys())
    print(f"targeting full universe: {len(target_apns)} parcels (no sampling)", file=sys.stderr)

    # Each chunk is an independent join query (no shared state between them), so
    # they're fetched concurrently via a thread pool instead of one at a time --
    # the bottleneck here is per-request network round-trip latency, not CPU or
    # the server's own throughput limit (no rate-limit/429 responses observed at
    # this concurrency in testing), so parallelizing gives a large wall-clock win.
    MAX_WORKERS = 12

    def fetch_all_chunks(label, fetch_fn, merge_into):
        chunk_list = list(chunks(target_apns, JOIN_CHUNK_SIZE))
        done = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(fetch_fn, c): c for c in chunk_list}
            for fut in as_completed(futures):
                merge_into.update(fut.result())
                done += 1
                if done % 40 == 0:
                    print(f"  {label} chunk {done}/{len(chunk_list)}: {len(merge_into)} so far", file=sys.stderr)
        print(f"  {label} DONE: {done}/{len(chunk_list)} chunks, {len(merge_into)} total", file=sys.stderr)

    print("fetching geometry (centroids) for full universe...", file=sys.stderr)
    geoms = {}
    fetch_all_chunks("geometry", fetch_geometry_chunk, geoms)

    print("fetching property characteristics (living area, etc) for full universe...", file=sys.stderr)
    prop_char = {}
    fetch_all_chunks("prop_char", fetch_property_char_chunk, prop_char)

    print("fetching current assessed value (CREST_TAXYEAR) for full universe...", file=sys.stderr)
    tax_totals = {}
    fetch_all_chunks("taxyear", fetch_taxyear_chunk, tax_totals)

    print("joining...", file=sys.stderr)
    snapshot = []
    for apn in target_apns:
        if apn not in geoms:
            continue
        lat, lon = geoms[apn]
        pc = prop_char.get(apn)
        sqft = (pc.get("LIVING_AREA") if pc else None) or 0
        assessed_total = tax_totals.get(apn, 0.0)
        if sqft <= 0 or assessed_total <= 0:
            continue
        addr_row = by_apn[apn]
        street = " ".join((addr_row.get("SITUS_STREET") or "").split())
        city = " ".join((addr_row.get("CITY") or "").split())
        snapshot.append({
            "pin": apn,
            "street": street,
            "city": city or "Unincorporated",
            "zip": addr_row.get("ZIP_CODE") or "",
            "lat": lat, "lon": lon,
            "sqft": sqft,
            "year_built": pc.get("YEAR_BUILT") if pc else None,
            "beds": pc.get("BEDROOM_COUNT") if pc else None,
            "baths": pc.get("BATH_COUNT") if pc else None,
            "assessed_total": assessed_total,
        })

    with open(OUT_SNAPSHOT, "w") as f:
        json.dump(snapshot, f)
    print(f"wrote {len(snapshot)} usable parcels -> {OUT_SNAPSHOT}", file=sys.stderr)

    with open(OUT_SCOPE_META, "w") as f:
        json.dump({
            "total_sfr_parcels_countywide": total_sfr,
            "full_coverage": True,
            "sample_stride": None,
            "sample_size_requested": len(target_apns),
            "sample_size_usable": len(snapshot),
        }, f)


if __name__ == "__main__":
    main()
