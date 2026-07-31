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
live returnCountOnly query). That's still far beyond what's practical to
fully fetch and join across 4 tables (each capped at maxRecordCount=2000
per request) in one pipeline run, so per the assigned practical
constraints we take a bounded, systematic sample instead of the full
580,109: every 5th parcel in APN order (stride=5), which yields ~116,000
parcels (~20% of the SFR universe), comfortably inside the suggested
50,000-150,000 sample range while still spanning the entire county
geographically (APN order in Riverside's system runs by assessment map
book/page, which is itself geographically organized, so a fixed-stride
sample avoids the bias a contiguous head-N slice would have of only
capturing one part of the county).

Reads:  nothing (hits the network)
Writes: pipeline/tmp/riverside_snapshot_raw.json   (enriched sampled snapshot)
        pipeline/tmp/riverside_scope_meta.json     (scope/sampling metadata,
                                                     consumed by 03_process
                                                     for the methodology doc)
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
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
SAMPLE_STRIDE = 5  # keep every 5th parcel in APN order -> ~20% of the SFR universe
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


def fetch_full_sfr_list():
    """Page through PARCELS_CREST for every Single Family Dwelling parcel,
    lightweight fields only (no geometry yet -- that's fetched later, just
    for the sampled subset, since geometry payloads are heavy)."""
    rows = []
    offset = 0
    while True:
        params = {
            "where": f"CLASS_CODE='{CLASS_CODE_SFR}'",
            "outFields": "APN,SITUS_STREET,CITY,ZIP_CODE",
            "returnGeometry": "false",
            "orderByFields": "APN",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "f": "json",
        }
        data = http_query(PARCELS_CREST, params)
        feats = data.get("features", [])
        rows.extend(feats)
        print(f"  offset={offset} got={len(feats)} total_so_far={len(rows)}", file=sys.stderr)
        if len(feats) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.15)
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
    apn_sorted = sorted(by_apn.keys())
    sample_apns = apn_sorted[::SAMPLE_STRIDE]
    print(f"sampled {len(sample_apns)} parcels (every {SAMPLE_STRIDE}th, APN order)", file=sys.stderr)

    print("fetching geometry (centroids) for sample...", file=sys.stderr)
    geoms = {}
    for i, chunk in enumerate(chunks(sample_apns, JOIN_CHUNK_SIZE)):
        geoms.update(fetch_geometry_chunk(chunk))
        if i % 20 == 0:
            print(f"  geometry chunk {i}: {len(geoms)} so far", file=sys.stderr)
        time.sleep(0.1)

    print("fetching property characteristics (living area, etc) for sample...", file=sys.stderr)
    prop_char = {}
    for i, chunk in enumerate(chunks(sample_apns, JOIN_CHUNK_SIZE)):
        prop_char.update(fetch_property_char_chunk(chunk))
        if i % 20 == 0:
            print(f"  prop_char chunk {i}: {len(prop_char)} so far", file=sys.stderr)
        time.sleep(0.1)

    print("fetching current assessed value (CREST_TAXYEAR) for sample...", file=sys.stderr)
    tax_totals = {}
    for i, chunk in enumerate(chunks(sample_apns, JOIN_CHUNK_SIZE)):
        tax_totals.update(fetch_taxyear_chunk(chunk))
        if i % 20 == 0:
            print(f"  taxyear chunk {i}: {len(tax_totals)} so far", file=sys.stderr)
        time.sleep(0.1)

    print("joining...", file=sys.stderr)
    snapshot = []
    for apn in sample_apns:
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
            "sample_stride": SAMPLE_STRIDE,
            "sample_size_requested": len(sample_apns),
            "sample_size_usable": len(snapshot),
        }, f)


if __name__ == "__main__":
    main()
