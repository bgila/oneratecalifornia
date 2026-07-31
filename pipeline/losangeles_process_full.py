"""
Process the FULL Los Angeles County assessor bulk file (all ~17.7GB,
pipeline/tmp/losangeles_full.tsv) into a citywide market-value estimate,
replacing the earlier single-byte-range-slice version (losangeles_process.py,
which only covered ~105k parcels in the northwest San Fernando Valley).

Rows are AIN-major, RollYear-minor (each parcel's ~19 yearly rows are
contiguous, then the file moves to the next AIN) -- confirmed by inspecting
a real sample in an earlier session. This lets the whole file be processed
in a SINGLE STREAMING PASS with only one parcel's rows buffered in memory
at a time, never the full ~2-3M-parcel universe at once.

For each parcel (buffered group of rows sharing one AIN):
  - Skip anything that isn't UseType=='SFR'.
  - The row with the highest RollYear is that parcel's current snapshot
    (address, sqft, year built, current assessed value = Roll_TotalValue,
    the pre-exemption gross total -- verified against netTaxableValue +
    Roll_TotalExemption in an earlier session; deliberately NOT
    netTaxableValue, which nets out the ~$7k homeowner's exemption and
    would understate assessed value relative to SF's own convention).
  - Jump-confirmed comp detection, identical logic to SF's
    pipeline/03_process_sfr.py: for each year-over-year step within the
    parcel's own history, if Roll_TotalValue jumps >=8% AND RecordingDate
    falls within about a year of that jump, the post-jump value is a real
    market-reset price signal (same JUMP_THRESHOLD as SF; LA has a genuine
    RecordingDate field, unlike several other counties added this batch).

Comps are pooled by SitusCity (LA County spans wildly different submarkets
-- Bel-Air, Compton, Lancaster -- so a flat citywide nearest-K pool would
badly misprice the very cities a same-city comparison exists to prevent;
this replaces the single-slice version's flat-pool design, which was only
valid because that version covered one small, roughly homogeneous area).
A city with fewer than MIN_COMPS_FOR_LOCAL_GROUP comps of its own falls
back to a price-tier pool (same-tier cities ranked by median comp $/sqft),
mirroring 03_process_sfr.py's neighborhood/tier fallback; a tier with too
few comps falls back further to the full county.

Comps are appreciation-adjusted to 2025-equivalent via LA County's FRED
HPI (data/losangeles-hpi.json, series ATNHPIUS06037A) before entering the
nearest-K pool, same technique as every other jump-confirmed-comp county
in this batch.

Reads:  pipeline/tmp/losangeles_full.tsv  (full bulk file, ~17.7GB)
        data/losangeles-hpi.json          (FRED LA County HPI)
Writes: pipeline/tmp/losangeles_full_full.csv   (full per-parcel table)
        data/losangeles-map-data.csv            (lat,lon,addr,sqft,assessed,market,subsidy,change,county)
        data/losangeles-methodology.json
"""
import csv
import gzip
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
REPO = PIPELINE_DIR.parent
DATA_DIR = REPO / "data"

FULL_TSV = TMP_DIR / "losangeles_full.tsv"
HPI_PATH = DATA_DIR / "losangeles-hpi.json"
OUT_FULL_CSV = TMP_DIR / "losangeles_full_full.csv"
# Gzipped: at full-county scale (~1.5M rows) the plain CSV is ~145MB, over
# GitHub's 100MB per-file limit (and Git LFS isn't an option -- GitHub Pages,
# which serves this site, doesn't resolve LFS pointer files, so an LFS-tracked
# file would break the live site). Gzip -9 gets this down to ~39MB. map.js
# decompresses it client-side via the Fetch API's DecompressionStream.
OUT_MAP_CSV = DATA_DIR / "losangeles-map-data.csv.gz"
OUT_METHOD = DATA_DIR / "losangeles-methodology.json"

ENCODING = "latin-1"
JUMP_THRESHOLD = 1.08
CURRENT_YEAR = 2025
K = 7
MIN_COMPS_FOR_LOCAL_GROUP = 12
NUM_PRICE_TIERS = 4
GENERAL_RATE_CURRENT = 1.00
BOND_RATE_SF = 0.18
GENERAL_RATE_PROPOSED = 0.70

MIN_SQFT = 300
MAX_SQFT = 15000


def to_float(x, default=0.0):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return default


def clean_city(situs_city):
    c = (situs_city or "").strip()
    if c.upper().endswith(" CA"):
        c = c[:-3].strip()
    return c.title() if c else "Unincorporated"


def clean_address(row):
    parts = [row.get("SitusHouseNo", ""), row.get("SitusFraction", ""), row.get("SitusDirection", ""),
             row.get("SitusStreet", ""), row.get("SitusUnit", "")]
    street = " ".join(p.strip() for p in parts if p and p.strip())
    street = " ".join(street.split())
    city = clean_city(row.get("SitusCity"))
    return f"{street}, {city}" if street else None


def process_parcel_group(rows, all_comps, comps_by_city):
    """rows: list of dict rows for one AIN, in file order (should already be
    RollYear-ascending within the group, but sort defensively)."""
    if not rows or rows[0].get("UseType") != "SFR":
        return None
    rows = sorted(rows, key=lambda r: int(r.get("RollYear") or 0))

    latest = rows[-1]
    if latest.get("isTaxableParcel") != "Y":
        return None
    sqft = to_float(latest.get("SQFTmain"))
    if sqft < MIN_SQFT or sqft > MAX_SQFT:
        return None
    assessed_total = to_float(latest.get("Roll_TotalValue"))
    if assessed_total <= 0:
        return None
    lat = to_float(latest.get("CENTER_LAT"), None)
    lon = to_float(latest.get("CENTER_LON"), None)
    if lat is None or lon is None or lat == 0 or lon == 0:
        return None
    addr = clean_address(latest)
    if not addr:
        return None
    city = clean_city(latest.get("SitusCity"))
    year_built = latest.get("YearBuilt") or None

    # Jump-confirmed comp detection across this parcel's own year-over-year history.
    best_jump = None
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        prev_val = to_float(prev.get("Roll_TotalValue"))
        cur_val = to_float(cur.get("Roll_TotalValue"))
        if prev_val <= 0 or cur_val <= 0:
            continue
        ratio = cur_val / prev_val
        if ratio < JUMP_THRESHOLD:
            continue
        rec_date = cur.get("RecordingDate") or prev.get("RecordingDate")
        if not rec_date or len(rec_date) < 4:
            continue
        rec_year = int(rec_date[:4])
        cur_year = int(cur.get("RollYear") or 0)
        if rec_year not in (cur_year - 1, cur_year):
            continue
        cur_sqft = to_float(cur.get("SQFTmain"))
        if cur_sqft < MIN_SQFT:
            continue
        raw_psf = cur_val / cur_sqft
        if raw_psf <= 0 or raw_psf > 10000:
            continue
        if best_jump is None or cur_year > best_jump[0]:
            best_jump = (cur_year, raw_psf)

    if best_jump is not None:
        comp_year, raw_psf = best_jump
        comp = {"city": city, "lat": lat, "lon": lon, "raw_psf": raw_psf, "comp_year": comp_year, "ain": latest.get("AIN")}
        all_comps.append(comp)
        comps_by_city[city].append(comp)

    return {
        "ain": latest.get("AIN"), "address": addr, "city": city,
        "lat": lat, "lon": lon, "sqft": sqft, "year_built": year_built,
        "assessed_total": assessed_total,
    }


def _strip_nul_lines(f):
    """A handful of rows deep in the real file (~15M rows in) contain a stray
    NUL byte, which Python's csv module rejects outright ('line contains
    NUL') regardless of the errors= setting on open() -- that only affects
    decoding, not this separate check. Strip NULs per line before csv ever
    sees them."""
    for line in f:
        if "\0" in line:
            line = line.replace("\0", "")
        yield line


def stream_parcels(tsv_path):
    """Yields completed per-AIN row groups from the AIN-sorted bulk file,
    buffering only the current AIN's rows at any time."""
    with open(tsv_path, encoding=ENCODING, newline="", errors="replace") as f:
        reader = csv.DictReader(_strip_nul_lines(f), delimiter="\t")
        current_ain = None
        buf = []
        n = 0
        for row in reader:
            n += 1
            ain = row.get("AIN")
            if ain != current_ain:
                if buf:
                    yield buf
                buf = []
                current_ain = ain
            buf.append(row)
            if n % 5_000_000 == 0:
                print(f"  ...{n:,} rows streamed", file=sys.stderr)
        if buf:
            yield buf


def main():
    t0 = time.time()
    hpi = json.load(open(HPI_PATH)) if HPI_PATH.exists() else {}
    hpi_current = hpi.get(str(CURRENT_YEAR))
    has_hpi = bool(hpi) and hpi_current

    print("streaming full bulk file (single pass, AIN-buffered)...", file=sys.stderr)
    parcels = {}
    all_comps = []
    comps_by_city = defaultdict(list)
    groups_seen = 0
    for group in stream_parcels(FULL_TSV):
        groups_seen += 1
        p = process_parcel_group(group, all_comps, comps_by_city)
        if p:
            parcels[p["ain"]] = p
        if groups_seen % 200_000 == 0:
            print(f"  ...{groups_seen:,} parcels grouped, {len(parcels):,} usable SFR so far, "
                  f"{len(all_comps):,} comps so far ({time.time()-t0:.0f}s elapsed)", file=sys.stderr)

    print(f"DONE streaming: {groups_seen:,} distinct AINs, {len(parcels):,} usable SFR parcels, "
          f"{len(all_comps):,} jump-confirmed comps ({time.time()-t0:.0f}s elapsed)", file=sys.stderr)

    # Appreciation-adjust every comp to 2025-equivalent.
    if has_hpi:
        for c in all_comps:
            hpi_at_comp = hpi.get(str(c["comp_year"]), hpi_current)
            c["psf"] = c["raw_psf"] * (hpi_current / hpi_at_comp)
    else:
        print("WARNING: no usable HPI -- comps used at nominal (un-adjusted) value", file=sys.stderr)
        for c in all_comps:
            c["psf"] = c["raw_psf"]

    # Rank cities into price tiers from their own comps (however few), for the fallback pool.
    city_median_psf = {
        city: statistics.median(c["psf"] for c in comps)
        for city, comps in comps_by_city.items() if comps
    }
    ranked_cities = sorted(city_median_psf, key=lambda c: city_median_psf[c])
    tier_of_city = {}
    if ranked_cities:
        tier_size = math.ceil(len(ranked_cities) / NUM_PRICE_TIERS)
        for i, city in enumerate(ranked_cities):
            tier_of_city[city] = min(i // tier_size, NUM_PRICE_TIERS - 1)
    comps_by_tier = defaultdict(list)
    for city, comps in comps_by_city.items():
        t = tier_of_city.get(city)
        if t is not None:
            comps_by_tier[t].extend(comps)

    all_lat = np.array([c["lat"] for c in all_comps])
    all_lon = np.array([c["lon"] for c in all_comps])
    all_psf = np.array([c["psf"] for c in all_comps])
    all_ain = np.array([c["ain"] for c in all_comps])

    by_city = defaultdict(list)
    for p in parcels.values():
        by_city[p["city"]].append(p)

    print(f"cities represented: {len(by_city)}; comps found in {len(comps_by_city)} of them", file=sys.stderr)

    fieldnames = ["ain", "address", "city", "lat", "lon", "sqft", "year_built",
                  "assessed_total", "est_market_value", "est_price_per_sqft",
                  "comp_count", "comp_source", "current_tax_est",
                  "subsidy_vs_market_today", "tax_under_reform_est", "change_under_reform"]

    rows_written = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0
    source_counts = defaultdict(int)

    with open(OUT_FULL_CSV, "w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fieldnames)
        writer.writeheader()

        cities_done = 0
        for city, members in by_city.items():
            local_comps = comps_by_city.get(city, [])
            tier_comps = comps_by_tier.get(tier_of_city.get(city))
            if len(local_comps) >= MIN_COMPS_FOR_LOCAL_GROUP:
                comp_lat = np.array([c["lat"] for c in local_comps])
                comp_lon = np.array([c["lon"] for c in local_comps])
                comp_psf = np.array([c["psf"] for c in local_comps])
                comp_ain = np.array([c["ain"] for c in local_comps])
                source_label = "same_city"
            elif tier_comps and len(tier_comps) >= MIN_COMPS_FOR_LOCAL_GROUP:
                comp_lat = np.array([c["lat"] for c in tier_comps])
                comp_lon = np.array([c["lon"] for c in tier_comps])
                comp_psf = np.array([c["psf"] for c in tier_comps])
                comp_ain = np.array([c["ain"] for c in tier_comps])
                source_label = "same_price_tier"
            else:
                comp_lat, comp_lon, comp_psf, comp_ain = all_lat, all_lon, all_psf, all_ain
                source_label = "countywide_fallback"

            lat0 = math.radians(34.0)
            batch_rows = []
            for p in members:
                dx = (comp_lon - p["lon"]) * math.cos(lat0)
                dy = comp_lat - p["lat"]
                d2 = dx * dx + dy * dy
                mask = comp_ain != p["ain"]
                d2 = np.where(mask, d2, np.inf)
                if len(d2) <= K:
                    idx = np.argsort(d2)[:K]
                else:
                    idx = np.argpartition(d2, K)[:K]
                valid = idx[np.isfinite(d2[idx])]
                if len(valid) == 0:
                    continue
                med_psf = float(np.median(comp_psf[valid]))
                est_market_value = med_psf * p["sqft"]
                est_market_value = max(est_market_value, p["assessed_total"])

                current_tax = p["assessed_total"] * (GENERAL_RATE_CURRENT + BOND_RATE_SF) / 100
                market_tax_current_law = est_market_value * (GENERAL_RATE_CURRENT + BOND_RATE_SF) / 100
                reform_tax = est_market_value * (GENERAL_RATE_PROPOSED + BOND_RATE_SF) / 100
                subsidy = market_tax_current_law - current_tax
                change = reform_tax - current_tax
                subsidy_all.append(subsidy)
                change_all.append(change)
                if change > 0:
                    increases += 1
                elif change < 0:
                    decreases += 1
                source_counts[source_label] += 1

                batch_rows.append({
                    "ain": p["ain"], "address": p["address"], "city": city,
                    "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                    "sqft": p["sqft"], "year_built": p["year_built"],
                    "assessed_total": round(p["assessed_total"]), "est_market_value": round(est_market_value),
                    "est_price_per_sqft": round(med_psf, 2), "comp_count": len(valid), "comp_source": source_label,
                    "current_tax_est": round(current_tax), "subsidy_vs_market_today": round(subsidy),
                    "tax_under_reform_est": round(reform_tax), "change_under_reform": round(change),
                })
            writer.writerows(batch_rows)
            rows_written += len(batch_rows)
            cities_done += 1
            if cities_done % 20 == 0:
                print(f"  [{cities_done}/{len(by_city)}] cities processed, {rows_written:,} rows written so far",
                      file=sys.stderr)

    print(f"WROTE {rows_written:,} rows -> {OUT_FULL_CSV}", file=sys.stderr)
    print(f"comp source breakdown: {dict(source_counts)}", file=sys.stderr)

    # ---- slim to the shared map-data schema (gzipped, see OUT_MAP_CSV comment) ----
    with open(OUT_FULL_CSV) as fin, gzip.open(OUT_MAP_CSV, "wt", newline="", compresslevel=9) as fout:
        reader = csv.DictReader(fin)
        writer = csv.writer(fout)
        writer.writerow(["lat", "lon", "addr", "sqft", "assessed", "market", "subsidy", "change", "county"])
        for row in reader:
            writer.writerow([
                row["lat"], row["lon"], row["address"], row["sqft"],
                row["assessed_total"], row["est_market_value"],
                row["subsidy_vs_market_today"], row["change_under_reform"], "Los Angeles",
            ])
    print(f"wrote map data -> {OUT_MAP_CSV}", file=sys.stderr)

    summary = {
        "methodology": {
            "source_assessed_values": "LA County Assessor: Assessor Parcel Data (Rolls 2006-Present), bulk file",
            "source_url": "https://data.lacounty.gov/datasets/2231275cebd6426897bb9c2a7aaf9840",
            "scope": "Full Los Angeles County, Single Family Residential (UseType='SFR'), all parcels with usable sqft/value/geometry, rolls 2006-2025",
            "market_value_estimation": (
                "Comps are identified by detecting actual reassessment events across each parcel's own "
                "2006-2025 roll history (this bulk file uniquely includes full multi-year history AND a real "
                "RecordingDate field in one place): a parcel whose total assessed value (Roll_TotalValue) jumps "
                ">=8% year-over-year, with a recorded transfer date within about a year of that jump, is treated "
                "as a confirmed market reset -- its post-jump value is used as a real price-per-sqft signal, "
                "appreciation-adjusted to today's-equivalent price via FRED's house price index for LA County. "
                f"For each home, the {K} nearest confirmed comps by location set the estimate: median $/sqft x "
                "subject sqft. 'By location' means same city first (LA County spans very different submarkets, "
                f"so a flat countywide pool would badly misprice most of them); a city with fewer than "
                f"{MIN_COMPS_FOR_LOCAL_GROUP} comps of its own falls back to a price-tier pool (cities ranked into "
                f"{NUM_PRICE_TIERS} quartiles by their own comps' median $/sqft); a tier with too few comps falls "
                "back further to the full county. Estimated market value is floored at the home's own current "
                "assessed value, since a below-assessed estimate is almost always the comp model undershooting, "
                "not a real declining-value home."
            ),
            "known_limitations": (
                "This is now the FULL county (not the earlier version's ~105k-parcel San Fernando Valley slice). "
                "Same weak point as San Francisco's own model: still undershoots at the very top of the market "
                "(large/luxury homes), since nearest-comp $/sqft blends in typical nearby homes rather than "
                "modeling a price tier directly."
            ),
            "tax_assumptions": {
                "current_general_rate_pct": GENERAL_RATE_CURRENT,
                "sf_bond_rate_pct": BOND_RATE_SF,
                "proposed_general_rate_pct": GENERAL_RATE_PROPOSED,
            },
            "generated": time.strftime("%Y-%m-%d"),
        },
        "counts": {
            "total_ains_seen": groups_seen,
            "usable_sfr_parcels": len(parcels),
            "jump_confirmed_comps": len(all_comps),
            "cities": len(by_city),
            "cities_with_own_comps": len(comps_by_city),
            "estimated_rows_written": rows_written,
            "comp_source_breakdown": dict(source_counts),
        },
        "stats": {
            "subsidy_vs_market_today": {
                "p10": round(np.percentile(subsidy_all, 10)), "median": round(statistics.median(subsidy_all)),
                "mean": round(statistics.mean(subsidy_all)), "p90": round(np.percentile(subsidy_all, 90)),
                "min": round(min(subsidy_all)), "max": round(max(subsidy_all)),
            },
            "under_reform": {
                "would_pay_more": increases, "would_pay_less": decreases,
                "pct_pay_more": round(100 * increases / rows_written, 1) if rows_written else None,
                "pct_pay_less": round(100 * decreases / rows_written, 1) if rows_written else None,
            },
        },
    }
    with open(OUT_METHOD, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"wrote methodology -> {OUT_METHOD}", file=sys.stderr)
    print(f"TOTAL TIME: {time.time()-t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
