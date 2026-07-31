"""
Build market-value estimates for the LA County single-family parcel sample
fetched by losangeles_fetch.py (a byte-range slice of the county's bulk
"Assessor Parcel Data (Rolls 2006-Present)" roll, covering ~106k SFR parcels
in a contiguous northwest San Fernando Valley swath -- see that file's
docstring for why the scope is bounded this way).

This mirrors 03_process_sfr.py's approach as closely as the source data
allows, with one simplification (see step 3 below):

1. "Jump-confirmed comps": same definition as SF. A parcel whose total
   assessed value (Roll_LandValue + Roll_ImpValue + Roll_FixtureValue)
   jumps >=8% year-over-year, AND has a RecordingDate within a year of that
   jump, is treated as a confirmed market reset -- its post-jump assessed
   value is used as a real price-per-sqft signal. Unlike some counties'
   assessor data, LA's bulk roll genuinely has a RecordingDate field
   (YYYYMMDD), confirmed by pulling and inspecting real rows before writing
   this, so this is the full method, not a value-jump-only fallback.
2. Comps are appreciation-adjusted to today's-equivalent price via the FRED
   LA County house price index (data/losangeles-hpi.json, series
   ATNHPIUS06037A), same idea and same hpi[target]/hpi[comp_year] formula as
   SF uses with data/sf-hpi.json. This matters more here than for SF: our
   jump-confirmed comps span the full 2006-2024 history window (18 years),
   so unadjusted nominal comps would meaningfully understate market value
   for anything whose only confirmed comp is from early in that window. (An
   earlier run in a network-restricted sandbox couldn't reach FRED and fell
   back to nominal $/sqft -- adjusted_psf == raw_psf -- purely a sandbox
   networking limitation; this script picks the adjustment up automatically
   whenever data/losangeles-hpi.json is present and non-empty, no code
   change needed. The methodology JSON records which happened.)
3. SIMPLIFICATION vs SF: SF pools comps by neighborhood (with a price-tier
   fallback) because it covers an entire, highly heterogeneous city. This
   run's scope is already a single contiguous ~106k-parcel swath of the
   northwest San Fernando Valley (Chatsworth/Winnetka/Canoga Park/West
   Hills), not all of LA County, so there's no citywide-diversity problem to
   solve. We skip neighborhood/price-tier bucketing entirely and pool ALL
   confirmed comps in scope for every target home's nearest-K search --
   the K=7 nearest-by-distance search itself keeps each estimate local. This
   is simpler than SF's approach and only valid because of the narrow scope;
   it would need SF's tiering logic reinstated before extending to all of
   LA County.
4. Same floor as SF: estimated market value is floored at the parcel's own
   current assessed value, for the same reason (a below-assessed estimate
   is virtually always the model undershooting, not a real declining-value
   home).

Reads:  pipeline/tmp/losangeles_sfr_raw.json  (losangeles_fetch.py)
        data/losangeles-hpi.json              (losangeles_fetch.py)
Writes: pipeline/tmp/losangeles-full.csv       (full per-parcel estimate table)
        data/losangeles-methodology.json       (methodology + summary stats)
"""
import csv
import datetime
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
RAW_PATH = TMP_DIR / "losangeles_sfr_raw.json"
HPI_PATH = DATA_DIR / "losangeles-hpi.json"
OUT_CSV = TMP_DIR / "losangeles-full.csv"
OUT_METHODOLOGY = DATA_DIR / "losangeles-methodology.json"

JUMP_THRESHOLD = 1.08
K = 7
HPI_TARGET_YEAR = 2025  # scale every comp to this year's-equivalent price, matching data/losangeles-hpi.json's latest year
GENERAL_RATE_CURRENT = 1.00
BOND_RATE = 0.18  # held at SF's rate for cross-county consistency (deliberate simplification)
GENERAL_RATE_PROPOSED = 0.70

MIN_SQFT = 200
MAX_SQFT = 20000
MAX_PSF = 10000  # sanity guard on comp $/sqft, same order of magnitude as SF's


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def clean_city(situs_city):
    """'LOS ANGELES CA' -> 'Los Angeles'"""
    c = (situs_city or "").strip()
    c = re.sub(r"\s+CA$", "", c)
    return c.title()


def build_address(row):
    parts = [row.get("SitusHouseNo"), row.get("SitusFraction"), row.get("SitusDirection"),
             row.get("SitusStreet"), row.get("SitusUnit")]
    parts = [p.strip() for p in parts if p and p.strip()]
    street = re.sub(r"\s+", " ", " ".join(parts)).strip().title()
    city = clean_city(row.get("SitusCity"))
    return f"{street}, {city}" if city else street


def has_usable_address(row):
    """A small number of parcels (~0.5% in this run) are PUD/common-area
    sub-parcels (UseCode 010D etc.) with a blank or '0' house number and no
    street -- not addressable homes a user could look up on the map, so
    they're dropped here rather than passed through with SF's looser
    (assessed_total > 0 only) filter."""
    house_no = (row.get("SitusHouseNo") or "").strip()
    street = (row.get("SitusStreet") or "").strip()
    return bool(house_no) and house_no != "0" and bool(street)


def load_rows(raw_path):
    return json.load(open(raw_path, encoding="utf-8"))


def main():
    print("loading fetched LA SFR rows...", file=sys.stderr)
    rows = load_rows(RAW_PATH)
    print("raw parcel-year rows:", len(rows), file=sys.stderr)

    by_ain = defaultdict(dict)  # AIN -> {year: row}
    for r in rows:
        ain = r.get("AIN")
        y = r.get("RollYear")
        if not ain or not y:
            continue
        try:
            y = int(y)
        except ValueError:
            continue
        by_ain[ain][y] = r

    all_years = sorted({y for years in by_ain.values() for y in years})
    HISTORY_START_YEAR, CURRENT_YEAR = all_years[0], all_years[-1]
    print(f"years present: {HISTORY_START_YEAR}-{CURRENT_YEAR}", file=sys.stderr)

    # --- current-snapshot parcel info (most recent year each parcel has) ---
    parcels = {}
    for ain, years in by_ain.items():
        latest_year = max(years)
        r = years[latest_year]
        if not has_usable_address(r):
            continue
        lat, lon = to_float(r.get("CENTER_LAT")), to_float(r.get("CENTER_LON"))
        if lat == 0 or lon == 0:
            continue
        sqft = to_float(r.get("SQFTmain"))
        if sqft <= MIN_SQFT or sqft > MAX_SQFT:
            continue
        assessed_total = (to_float(r.get("Roll_LandValue")) + to_float(r.get("Roll_ImpValue"))
                           + to_float(r.get("Roll_FixtureValue")))
        if assessed_total <= 0:
            continue
        parcels[ain] = {
            "ain": ain,
            "address": build_address(r),
            "city": clean_city(r.get("SitusCity")),
            "lat": lat, "lon": lon,
            "beds": to_float(r.get("Bedrooms")),
            "baths": to_float(r.get("Bathrooms")),
            "sqft": sqft,
            "year_built": r.get("YearBuilt"),
            "snapshot_year": latest_year,
            "assessed_total": assessed_total,
        }
    print("usable parcels (current snapshot):", len(parcels), file=sys.stderr)

    # --- jump-confirmed comps across full history window ---
    confirmed = {}
    for ain, years in by_ain.items():
        best = None
        for y in range(HISTORY_START_YEAR + 1, CURRENT_YEAR + 1):
            if y not in years or (y - 1) not in years:
                continue
            prev_total = (to_float(years[y - 1].get("Roll_LandValue")) + to_float(years[y - 1].get("Roll_ImpValue"))
                          + to_float(years[y - 1].get("Roll_FixtureValue")))
            cur_total = (to_float(years[y].get("Roll_LandValue")) + to_float(years[y].get("Roll_ImpValue"))
                         + to_float(years[y].get("Roll_FixtureValue")))
            if prev_total <= 0 or cur_total <= 0:
                continue
            ratio = cur_total / prev_total
            rec_date = years[y].get("RecordingDate")
            sale_near = bool(rec_date) and rec_date.strip() and rec_date[:4].isdigit() and int(rec_date[:4]) in (y - 1, y)
            if sale_near and ratio >= JUMP_THRESHOLD:
                if best is None or y > best[0]:
                    best = (y, cur_total)
        if best:
            confirmed[ain] = {"year": best[0], "total": best[1]}
    print("jump-confirmed comps found:", len(confirmed), file=sys.stderr)

    # --- HPI appreciation adjustment (best-effort; identity if unavailable) ---
    # data/losangeles-hpi.json is FRED series ATNHPIUS06037A ("All-Transactions House Price
    # Index for Los Angeles County, CA"), same convention as data/sf-hpi.json. Every comp's
    # post-jump assessed value is scaled from its own comp year to HPI_TARGET_YEAR-equivalent
    # price, same idea as 03_process_sfr.py does for SF, so a 2006 comp doesn't drag an
    # estimate down just because it's nominally stale -- this matters more here than for SF
    # since our jump-confirmed comps span a full 2006-2024 window (18 years) rather than SF's
    # narrower recent-year concentration.
    hpi = {}
    if HPI_PATH.exists():
        try:
            hpi = json.load(open(HPI_PATH))
        except Exception:
            hpi = {}
    hpi_current = hpi.get(str(HPI_TARGET_YEAR))
    hpi_available = bool(hpi) and hpi_current is not None
    if not hpi_available:
        print("WARNING: no usable LA County HPI data -- comps will NOT be appreciation-adjusted "
              "(using nominal $/sqft as-is).", file=sys.stderr)

    all_comps = []
    for ain, info in confirmed.items():
        p = parcels.get(ain)
        if not p:
            continue
        raw_psf = info["total"] / p["sqft"]
        if raw_psf <= 0 or raw_psf > MAX_PSF:
            continue
        if hpi_available:
            hpi_at_comp = hpi.get(str(info["year"]), hpi_current)
            adjusted_psf = raw_psf * (hpi_current / hpi_at_comp)
        else:
            adjusted_psf = raw_psf
        all_comps.append({
            "ain": ain, "lat": p["lat"], "lon": p["lon"],
            "price_per_sqft": adjusted_psf, "comp_year": info["year"],
        })
    print("usable comps after join:", len(all_comps), file=sys.stderr)

    comp_lat = np.array([c["lat"] for c in all_comps])
    comp_lon = np.array([c["lon"] for c in all_comps])
    comp_psf = np.array([c["price_per_sqft"] for c in all_comps])
    comp_ain = np.array([c["ain"] for c in all_comps])

    lat0 = math.radians(statistics.mean(p["lat"] for p in parcels.values()))

    def nearest_k_psf(target_lat, target_lon, target_ain, k):
        dx = (comp_lon - target_lon) * math.cos(lat0)
        dy = (comp_lat - target_lat)
        d2 = dx * dx + dy * dy
        mask = comp_ain != target_ain
        d2 = np.where(mask, d2, np.inf)
        if len(d2) <= k:
            idx = np.argsort(d2)[:k]
        else:
            idx = np.argpartition(d2, k)[:k]
        valid = idx[np.isfinite(d2[idx])]
        if len(valid) == 0:
            return None
        return float(np.median(comp_psf[valid])), len(valid)

    fieldnames = [
        "ain", "address", "city", "lat", "lon", "beds", "baths", "sqft", "year_built",
        "assessed_total", "est_market_value", "est_price_per_sqft", "comp_count",
        "current_tax_est", "subsidy_vs_market_today", "tax_under_reform_est", "change_under_reform",
    ]

    rows_written = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0

    with open(OUT_CSV, "w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fieldnames)
        writer.writeheader()
        batch = []
        for ain, p in parcels.items():
            result = nearest_k_psf(p["lat"], p["lon"], ain, K)
            if result is None:
                continue
            med_psf, n_used = result
            est_market_value = med_psf * p["sqft"]
            # Floor at current assessed value -- see module docstring, step 4.
            est_market_value = max(est_market_value, p["assessed_total"])

            current_tax = p["assessed_total"] * (GENERAL_RATE_CURRENT + BOND_RATE) / 100
            market_tax_current_law = est_market_value * (GENERAL_RATE_CURRENT + BOND_RATE) / 100
            reform_tax = est_market_value * (GENERAL_RATE_PROPOSED + BOND_RATE) / 100
            subsidy = market_tax_current_law - current_tax
            change = reform_tax - current_tax

            subsidy_all.append(subsidy)
            change_all.append(change)
            if change > 0:
                increases += 1
            elif change < 0:
                decreases += 1

            batch.append({
                "ain": ain, "address": p["address"], "city": p["city"],
                "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                "beds": p["beds"], "baths": p["baths"], "sqft": p["sqft"], "year_built": p["year_built"],
                "assessed_total": round(p["assessed_total"]), "est_market_value": round(est_market_value),
                "est_price_per_sqft": round(med_psf, 2), "comp_count": n_used,
                "current_tax_est": round(current_tax), "subsidy_vs_market_today": round(subsidy),
                "tax_under_reform_est": round(reform_tax), "change_under_reform": round(change),
            })
            if len(batch) >= 5000:
                writer.writerows(batch)
                rows_written += len(batch)
                print(f"  wrote {rows_written} rows so far...", file=sys.stderr)
                batch = []
        writer.writerows(batch)
        rows_written += len(batch)

    print("WROTE", rows_written, "rows ->", OUT_CSV, file=sys.stderr)

    methodology = {
        "methodology": {
            "source_assessed_values": (
                "LA County Assessor: 'Assessor Parcel Data (Rolls 2006-Present)' bulk CSV "
                "(ArcGIS item 2231275cebd6426897bb9c2a7aaf9840), a single flat ~17.7GB file "
                "(no queryable FeatureServer/Socrata endpoint exists for it -- confirmed by "
                "pulling the item's own metadata, which shows url: null, type: CSV)."
            ),
            "source_url": "https://data.lacounty.gov/datasets/2231275cebd6426897bb9c2a7aaf9840",
            "scope": (
                "NOT all of LA County. A single HTTP byte-range request pulled the first "
                f"{1_073_741_824 / 1e9:.2f}GB of the file. Because the file's rows are AIN-major "
                "(each parcel's ~19 years of history are contiguous) and AIN encodes assessor "
                "map-book/page (a geographic index), this contiguous byte range is also a "
                "contiguous geographic swath: the western San Fernando Valley and adjoining Santa "
                "Monica Mountains / Conejo corridor communities -- largely Los Angeles city "
                "neighborhoods (Woodland Hills, West Hills, Chatsworth, Canoga Park, Winnetka, "
                "Reseda, Tarzana, Encino, Van Nuys) plus the incorporated cities of Calabasas, "
                "Agoura Hills, Westlake Village, and Hidden Hills. This was a deliberate scoping "
                "decision (per instructions not to download/process "
                "the full 17.7GB file), not a random or representative sample of LA County as a "
                "whole -- a second run starting at a different byte offset, or raising the byte "
                "cap, would cover a different or larger area."
            ),
            "use_type_filter": "UseType == 'SFR' (LA Assessor's own single-family-residence tag); condos (UseType=='CND') are out of scope for this run, unlike SF's map which folds in individually-deeded condos.",
            "market_value_estimation": (
                "Same jump-confirmed-comps method as SF's 03_process_sfr.py: a parcel whose total "
                "assessed value (Roll_LandValue + Roll_ImpValue + Roll_FixtureValue) jumps >=8% "
                "year-over-year, with a RecordingDate within a year of that jump, is treated as a "
                "confirmed market reset -- its post-jump assessed value is a real price-per-sqft "
                "signal. Unlike some counties, LA's bulk roll has a genuine RecordingDate field "
                "(confirmed against real rows before writing this pipeline), so this is the full "
                "method, not a value-jump-only fallback. Comps are appreciation-adjusted to "
                "today's-equivalent price via the FRED LA County house price index "
                "(ATNHPIUS06037A) when available. "
                f"For each target home, the {K} nearest confirmed comps by straight-line distance "
                "set the estimate: median $/sqft x subject sqft. Unlike SF, comps are NOT bucketed "
                "by neighborhood/price-tier before the nearest-K search -- this run's scope is "
                "already a single contiguous ~106k-parcel swath (not all of heterogeneous LA "
                "County), so that extra layer wasn't needed; it would need to be reinstated before "
                "extending this pipeline countywide. Estimated market value is floored at the "
                "home's current assessed value, same reasoning as SF: a below-assessed estimate is "
                "almost always the model undershooting, not a real declining-value home."
            ),
            "house_price_index_adjustment": (
                f"applied: data/losangeles-hpi.json (FRED series ATNHPIUS06037A, 'All-Transactions "
                "House Price Index for Los Angeles County, CA', years 1975-2025, same convention as "
                f"data/sf-hpi.json) loaded successfully. Every jump-confirmed comp's post-jump "
                f"assessed $/sqft is scaled from its own comp year to {HPI_TARGET_YEAR}-equivalent "
                f"price via hpi[{HPI_TARGET_YEAR}]/hpi[comp_year], same logic 03_process_sfr.py uses "
                "for SF. This matters more here than for SF: our jump-confirmed comps span the full "
                "2006-2024 history window (18 years), so a meaningful share of comps are old enough "
                "that skipping this step would have materially understated market value -- a 2006 "
                "comp's nominal price-per-sqft needed scaling by roughly "
                f"{round(hpi_current / hpi.get('2006', hpi_current), 2) if hpi_available and hpi.get('2006') else 'N/A'}x "
                "to reach 2025-equivalent, versus roughly 1.0x for a 2024 comp. "
                "(Note: an earlier run of this pipeline in a network-restricted sandbox could not "
                "reach fred.stlouisfed.org and fell back to nominal $/sqft; that was a sandbox "
                "networking limitation, not a data-availability gap -- the coordinator fetched "
                "data/losangeles-hpi.json externally and this run uses it.)"
                if hpi_available else
                "NOT applied: data/losangeles-hpi.json is missing/empty; comps used as-is in "
                "nominal dollars. losangeles_process.py will pick up the adjustment automatically "
                "once that file is present, with no code change needed."
            ),
            "tax_assumptions": {
                "current_general_rate_pct": GENERAL_RATE_CURRENT,
                "bond_rate_pct_held_at_sf_rate": BOND_RATE,
                "proposed_general_rate_pct": GENERAL_RATE_PROPOSED,
                "note": "bond rate is deliberately held at SF's rate (not LA County's actual bond "
                        "rate) for cross-county comparability, matching the convention specified "
                        "for this whole multi-county build.",
            },
            "known_deviations_from_sf_pipeline": [
                "Scope is a bounded ~106k-parcel geographic swath of LA County (northwest San "
                "Fernando Valley), not the whole county, due to the 17.7GB source file having no "
                "query API (see 'scope' above).",
                "No neighborhood/price-tier comp bucketing (see 'market_value_estimation' above) "
                "-- valid only because of the narrow, roughly-homogeneous scope.",
                "Condos (UseType=='CND') excluded; SF's map includes individually-deeded condos.",
                "Added one filter beyond SF's (assessed_total > 0 only): parcels with a blank/'0' "
                "house number or blank street (~0.5% of parcels in scope, mostly PUD/common-area "
                "sub-parcels like shared driveways or HOA lots misfiled as UseType=='SFR') are "
                "dropped, since they aren't addressable homes a map user could look up.",
            ],
            "generated": datetime.date.today().isoformat(),
        },
        "counts": {
            "total_parcels_in_scope": len(parcels),
            "jump_confirmed_comps": len(all_comps),
            "estimated_rows_written": rows_written,
            "history_window": f"{HISTORY_START_YEAR}-{CURRENT_YEAR}",
        },
        "stats": {
            "subsidy_vs_market_today": {
                "p10": round(np.percentile(subsidy_all, 10)), "median": round(statistics.median(subsidy_all)),
                "mean": round(statistics.mean(subsidy_all)), "p90": round(np.percentile(subsidy_all, 90)),
                "p99": round(np.percentile(subsidy_all, 99)),
                "min": round(min(subsidy_all)), "max": round(max(subsidy_all)),
            },
            "under_reform": {
                "would_pay_more": increases, "would_pay_less": decreases,
                "pct_pay_more": round(100 * increases / rows_written, 1),
                "pct_pay_less": round(100 * decreases / rows_written, 1),
            },
        },
    }
    with open(OUT_METHODOLOGY, "w") as f:
        json.dump(methodology, f, indent=2)
    print("WROTE methodology ->", OUT_METHODOLOGY, file=sys.stderr)
    print(json.dumps(methodology, indent=2))


if __name__ == "__main__":
    main()
