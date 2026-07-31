"""
Build market-value estimates for every Placer County single-family/condo
parcel.

Placer County's public parcel layer (unlike SF's Socrata roll) is a single
current-year snapshot with no queryable multi-year assessed-value history,
so the SF approach (detect a year-over-year assessed-value jump, confirm it
against a nearby sale date) isn't available here. Instead, market value is
inferred from "confirmed recent reset" comps identified within the single
snapshot itself:

1. Comp confirmation signal. Two fields matter:
   - Tax_Cd_N: a plain-text assessment/exemption status. Live values include
     "NORMAL OWNERSHIP" (the ordinary case, ~90% of parcels), "PROP 8
     REDUCTION NO CPI" (assessed value temporarily reduced to the
     assessor's own opinion of a *declining* market value -- not a
     sale-confirmed reset), "PROP 19 BASE YEAR TRANSFER" and "PROP 19
     INTERGENERATIONAL TRANSFER" (the current assessed value is a prior
     owner's *preserved* lower base-year value carried onto this parcel,
     not a reassessment to purchase price -- the direct Placer-County
     analog of the parent-child/trust transfers SF's pipeline explicitly
     excludes). Only "NORMAL OWNERSHIP" parcels are eligible as comps: the
     other codes each mean the enrolled value is, by definition, NOT
     today's market value.
   - TransactionDt: "last recorded document date". Investigated live before
     use (see below) because naively trusting "recent TransactionDt" would
     have been wrong.
2. TransactionDt data-quality finding (deviation from the assigned
   methodology, see module-level NOTE below): a huge share of parcels
   (111,469 of 150,995 qualifying parcels, or ~74%) all carry the exact
   same TransactionDt timestamp, down to the second -- an unmistakable
   one-time system/migration timestamp, not 111k coincidentally same-day
   sales. Every date before that timestamp is sparse (a few hundred to ~1300
   a year, 2015-2024) in a pattern that looks more like parcel-map/lot-line
   recording noise than home sales. Every date *after* it shows a
   plausible, weekday-only cadence of ~50-250 parcels/business-day, spread
   across every community in the county in proportion to population (Roseville
   heaviest, then Lincoln/Rocklin/Auburn, down to Tahoe-area communities) --
   consistent with a live, granular, per-transaction recorder feed that
   started shortly after that migration event. So: the migration timestamp
   itself, and everything before it, is treated as "no reliable date," and
   only TransactionDt values strictly after that (single, auto-detected)
   sentinel timestamp are trusted as real recent transactions.
3. Comps: for each parcel meeting both filters (Tax_Cd_N == "NORMAL
   OWNERSHIP", TransactionDt after the sentinel), its own $/sqft
   (LandValue + Structure, divided by StructureSF -- verified to be the
   building/living-area field; LandSF is lot size) is used as a direct,
   recent, real-transaction market-value signal, unadjusted for
   appreciation since the whole window is only a few months wide.
4. Every parcel's estimate = median $/sqft of its K=7 geographically
   nearest comps (Euclidean approx. in lat/lon, cosine-corrected -- same
   method as 03_process_sfr.py), x its own StructureSF.
5. Estimated market value is floored at the parcel's own current assessed
   value (LandValue + Structure), for the same reason as SF: a home's true
   value essentially never sits below what Prop 13 has grown its
   assessment to, so a below-assessed estimate is almost always the model
   undershooting rather than a real decline.

NOTE -- deviation from the assigned methodology: the brief anticipated using
"TransactionDt recency (last ~2-3 years)" directly, refined if needed by a
Tax_Cd_N value that "explicitly denotes a recent base-year transfer/
reassessment." Live inspection found no such explicit "just reassessed to
market" code (Tax_Cd_N's codes are ownership/exemption *categories*, not
reassessment-event flags), and found that raw TransactionDt recency is
dominated by the single mass-migration timestamp described above -- using a
naive "last 2-3 years" cutoff without excluding it would have wrongly
treated ~74% of all parcels as "just confirmed at market value," badly
diluting the comp pool with ordinary Prop-13-capped homes. The auto-detected
sentinel-exclusion above is the fix, and produces a comp pool (~11,000-11,300
parcels depending on the exact snapshot pulled, ~7.4-7.5% of the qualifying
roll) comparable in relative size to SF's jump-confirmed comp share (~10.7%).

Reads:  pipeline/tmp/placer_parcels_raw.json (placer_fetch.py)
Writes: pipeline/tmp/placer-full.csv       (full per-parcel estimate table)
        data/placer-methodology.json       (methodology + summary stats)
"""
import csv
import datetime
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
RAW_PATH = TMP_DIR / "placer_parcels_raw.json"
OUT_CSV = TMP_DIR / "placer-full.csv"
OUT_METHODOLOGY = DATA_DIR / "placer-methodology.json"

K = 7
MIN_SQFT = 200  # drop degenerate/placeholder structure-sqft values, same guard as SF (sqft<=200)
MAX_SQFT = 20000
NORMAL_OWNERSHIP = "NORMAL OWNERSHIP"
# A single calendar day whose TransactionDt count is more than this many
# times the median non-zero day's count is treated as a mass-migration
# artifact, not real transaction activity (see module docstring).
SENTINEL_SPIKE_MULTIPLE = 20
GENERAL_RATE_CURRENT = 1.00
BOND_RATE_SF = 0.18  # held at SF's bond rate for cross-county consistency (deliberate simplification)
GENERAL_RATE_PROPOSED = 0.70


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_parcels(raw_path):
    raw = json.load(open(raw_path))
    parcels = {}
    for r in raw:
        attrs = r.get("attributes", {})
        centroid = r.get("centroid")
        if not centroid or centroid.get("x") is None or centroid.get("y") is None:
            continue
        lon, lat = centroid["x"], centroid["y"]
        sqft = to_float(attrs.get("StructureSF"))
        if sqft <= MIN_SQFT or sqft > MAX_SQFT:
            continue
        land = to_float(attrs.get("LandValue"))
        structure = to_float(attrs.get("Structure"))
        assessed_total = land + structure
        if assessed_total <= 0:
            continue
        apn = attrs.get("APN")
        # Prefer the street-only field (mirrors SF's convention of a clean
        # street address with no city name); fall back to the combined
        # field only if the street-only one is missing.
        addr = attrs.get("FormattedSitus1") or attrs.get("SitusAddressFull") or ""
        parcels[apn] = {
            "apn": apn,
            "address": " ".join(addr.split()),
            "community": attrs.get("Community") or "Unknown",
            "lat": lat, "lon": lon,
            "sqft": sqft,
            "lot_sqft": to_float(attrs.get("LandSF")),
            "assessed_total": assessed_total,
            "use_cd_n": attrs.get("Use_Cd_N"),
            "tax_cd_n": attrs.get("Tax_Cd_N"),
            "transaction_dt": attrs.get("TransactionDt"),  # epoch ms, or None
            "effective_yr": attrs.get("EffectiveYr"),
        }
    return parcels


def find_sentinel_cutoff(parcels):
    """Auto-detect the mass-migration TransactionDt timestamp (see module
    docstring) by finding any single calendar day whose count of
    TransactionDt values is a huge outlier vs. the typical day, among
    NORMAL OWNERSHIP parcels. Returns the epoch-ms of the *end* of the
    latest such sentinel day (exclusive cutoff: only TransactionDt values
    strictly after this count as a confirmed real transaction), or None if
    no such spike is found (in which case callers should fall back to a
    plain recency window).
    """
    by_day = Counter()
    for p in parcels.values():
        if p["tax_cd_n"] != NORMAL_OWNERSHIP or not p["transaction_dt"]:
            continue
        day = int(p["transaction_dt"]) // 86400000  # day index since epoch
        by_day[day] += 1
    if not by_day:
        return None
    counts = sorted(by_day.values())
    median_count = statistics.median(counts)
    threshold = max(median_count * SENTINEL_SPIKE_MULTIPLE, 5000)
    sentinel_days = [day for day, cnt in by_day.items() if cnt >= threshold]
    if not sentinel_days:
        return None
    latest_sentinel_day = max(sentinel_days)
    return (latest_sentinel_day + 1) * 86400000  # ms at the start of the next day


def build_comps(parcels, cutoff_ms):
    comps = []
    for apn, p in parcels.items():
        if p["tax_cd_n"] != NORMAL_OWNERSHIP:
            continue
        dt = p["transaction_dt"]
        if not dt or dt <= cutoff_ms:
            continue
        psf = p["assessed_total"] / p["sqft"]
        if psf <= 0 or psf > 5000:  # sanity guard, mirrors SF's psf sanity bound
            continue
        comps.append({
            "apn": apn, "community": p["community"],
            "lat": p["lat"], "lon": p["lon"], "price_per_sqft": psf,
            "transaction_dt": dt,
        })
    return comps


def nearest_k_psf(target_lat, target_lon, target_apn, comp_lat, comp_lon, comp_psf, comp_apn, k):
    lat0 = math.radians(39.0)  # Placer County center-ish latitude, for the cos() distance correction
    dx = (comp_lon - target_lon) * math.cos(lat0)
    dy = (comp_lat - target_lat)
    d2 = dx * dx + dy * dy
    mask = comp_apn != target_apn
    d2 = np.where(mask, d2, np.inf)
    if len(d2) <= k:
        idx = np.argsort(d2)[:k]
    else:
        idx = np.argpartition(d2, k)[:k]
    valid = idx[np.isfinite(d2[idx])]
    if len(valid) == 0:
        return None
    return float(np.median(comp_psf[valid])), len(valid)


def main():
    print("loading Placer parcel snapshot...", file=sys.stderr)
    parcels = load_parcels(RAW_PATH)
    print("usable single-family/condo parcels:", len(parcels), file=sys.stderr)

    cutoff_ms = find_sentinel_cutoff(parcels)
    if cutoff_ms is None:
        # Fallback if a future re-run of this pipeline doesn't reproduce the
        # mass-migration artifact: just use a 2-year recency window.
        cutoff_dt = datetime.datetime.utcnow() - datetime.timedelta(days=730)
        cutoff_ms = int(cutoff_dt.timestamp() * 1000)
        cutoff_source = "fallback_2yr_window"
    else:
        cutoff_source = "auto_detected_sentinel_migration_day"
    cutoff_iso = datetime.datetime.utcfromtimestamp(cutoff_ms / 1000).isoformat()
    print(f"comp-confirmation TransactionDt cutoff: {cutoff_iso} ({cutoff_source})", file=sys.stderr)

    all_comps = build_comps(parcels, cutoff_ms)
    print("confirmed recent-transaction comps found:", len(all_comps), file=sys.stderr)

    by_community = defaultdict(list)
    for p in parcels.values():
        by_community[p["community"]].append(p)

    all_comp_lat = np.array([c["lat"] for c in all_comps])
    all_comp_lon = np.array([c["lon"] for c in all_comps])
    all_comp_psf = np.array([c["price_per_sqft"] for c in all_comps])
    all_comp_apn = np.array([c["apn"] for c in all_comps])

    fieldnames = [
        "apn", "address", "community", "lat", "lon", "sqft", "lot_sqft",
        "assessed_total", "est_market_value", "est_price_per_sqft", "comp_count",
        "current_tax_est", "subsidy_vs_market_today", "tax_under_reform_est", "change_under_reform",
    ]

    rows_written = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0

    with open(OUT_CSV, "w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fieldnames)
        writer.writeheader()

        batch_rows = []
        for apn, p in parcels.items():
            result = nearest_k_psf(p["lat"], p["lon"], apn, all_comp_lat, all_comp_lon, all_comp_psf, all_comp_apn, K)
            if result is None:
                continue
            med_psf, n_used = result
            est_market_value = med_psf * p["sqft"]
            # Floor at current assessed value -- see module docstring, same
            # rationale as SF's 03_process_sfr.py.
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

            batch_rows.append({
                "apn": apn, "address": p["address"], "community": p["community"],
                "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                "sqft": p["sqft"], "lot_sqft": p["lot_sqft"],
                "assessed_total": round(p["assessed_total"]), "est_market_value": round(est_market_value),
                "est_price_per_sqft": round(med_psf, 2), "comp_count": n_used,
                "current_tax_est": round(current_tax), "subsidy_vs_market_today": round(subsidy),
                "tax_under_reform_est": round(reform_tax), "change_under_reform": round(change),
            })
            if len(batch_rows) >= 5000:
                writer.writerows(batch_rows)
                rows_written += len(batch_rows)
                print(f"  wrote {rows_written} rows so far...", file=sys.stderr)
                batch_rows = []
        writer.writerows(batch_rows)
        rows_written += len(batch_rows)

    print("WROTE", rows_written, "rows ->", OUT_CSV, file=sys.stderr)

    summary = {
        "methodology": {
            "source_assessed_values": "Placer County GIS Services: \"Parcels Public\" feature layer (Parcels_Public/FeatureServer, layer 4)",
            "source_url": "https://gis-placercounty.opendata.arcgis.com/ (search \"Parcels Public\"); service: https://services6.arcgis.com/PArfeTGcwA9RGNzN/arcgis/rest/services/Parcels_Public/FeatureServer/4",
            "scope": "Countywide, Single Family Residential (Use_Cd_N = 'SINGLE FAM RES, HALF PLEX') + individually-deeded condos (Use_Cd_N = 'SINGLE FAM RES, CONDO')",
            "data_shape_vs_sf": (
                "Placer's public parcel data is a single current-roll snapshot with no queryable "
                "multi-year assessed-value history, so SF's year-over-year 'jump-confirmed comp' "
                "detection (03_process_sfr.py) isn't possible here. Comps are instead identified "
                "from within the single snapshot using two live-verified fields: Tax_Cd_N (a "
                "plain-text ownership/exemption status) and TransactionDt (last recorded-document date)."
            ),
            "market_value_estimation": (
                "A parcel is a confirmed comp if Tax_Cd_N == 'NORMAL OWNERSHIP' (excluding "
                "'PROP 8 REDUCTION' codes, whose enrolled value is the assessor's own opinion of a "
                "*declining* market value rather than a sale-confirmed reset, and 'PROP 19 BASE YEAR "
                "TRANSFER'/'PROP 19 INTERGENERATIONAL TRANSFER' codes, whose enrolled value is a prior "
                "owner's preserved lower base-year value carried onto the parcel rather than a "
                "reassessment to purchase price -- the Placer-County analog of the Prop 19 parent-child/"
                "trust transfers SF's own pipeline excludes) AND its TransactionDt falls after an "
                "auto-detected cutoff. That cutoff exists because live inspection found ~74% of all "
                "qualifying parcels share one exact TransactionDt timestamp down to the second -- a "
                "one-time system/migration artifact, not a real mass sale event -- while every "
                "TransactionDt value after that timestamp shows a plausible weekday-only cadence "
                "spread proportionally across every community in the county, consistent with a live "
                "per-transaction recorder feed. Only TransactionDt values strictly after that "
                "auto-detected day are trusted. Each confirmed comp's own $/sqft "
                "(LandValue + Structure, divided by StructureSF, the assessor's building/living-area "
                "field -- LandSF is lot size and is not used for pricing) is used directly, "
                "unadjusted for appreciation since the whole confirmed window spans only a few months. "
                f"For each target parcel, the {K} nearest confirmed comps by straight-line distance set "
                "the estimate: median $/sqft x the parcel's own StructureSF. Estimated market value is "
                "then floored at the parcel's current assessed value (LandValue + Structure), for the "
                "same reason as SF: a home's true value essentially never sits below what Prop 13 has "
                "grown its assessment to, so a below-assessed estimate is almost always the model "
                "undershooting rather than a real declining-value home."
            ),
            "deviation_from_assigned_methodology": (
                "The assignment anticipated using raw TransactionDt recency (a ~2-3 year window), "
                "optionally sharpened by a Tax_Cd_N value that explicitly flags 'just reassessed at a "
                "new base year following a sale.' Live data had neither in the form expected: Tax_Cd_N's "
                "values are ownership/exemption *categories* (there is no distinct 'recently sold' code "
                "beyond the default 'NORMAL OWNERSHIP'), and raw TransactionDt recency is dominated by a "
                "single mass-migration timestamp shared by ~74% of parcels, which a naive 'last 2-3 "
                "years' filter would have wrongly counted as ~111k simultaneous market resets. This "
                "script instead auto-detects that migration day (any single day whose TransactionDt "
                f"count exceeds {SENTINEL_SPIKE_MULTIPLE}x the median day's count, floored at 5,000) and "
                "excludes it and everything before it, keeping only the post-migration era's per-"
                "transaction dates as the comp signal. This produced "
                f"{len(all_comps):,} confirmed comps (~{100 * len(all_comps) / len(parcels):.1f}% of the "
                "qualifying roll), a comp density comparable to SF's own jump-confirmed share (~10.7%)."
            ),
            "tax_assumptions": {
                "current_general_rate_pct": GENERAL_RATE_CURRENT,
                "bond_rate_pct_note": "held at SF's bond rate for cross-county comparability (deliberate simplification, not Placer's actual bond rate)",
                "bond_rate_pct": BOND_RATE_SF,
                "proposed_general_rate_pct": GENERAL_RATE_PROPOSED,
            },
            "transaction_dt_cutoff_used_iso": cutoff_iso,
            "transaction_dt_cutoff_source": cutoff_source,
            "generated": datetime.date.today().isoformat(),
        },
        "counts": {
            "total_parcels": len(parcels),
            "confirmed_recent_transaction_comps": len(all_comps),
            "communities": len(by_community),
            "estimated_rows_written": rows_written,
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
    DATA_DIR.mkdir(exist_ok=True)
    with open(OUT_METHODOLOGY, "w") as f:
        json.dump(summary, f, indent=2)

    print("WROTE methodology ->", OUT_METHODOLOGY, file=sys.stderr)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
