"""
Build market-value estimates for every Santa Barbara County single-family
residential parcel, and emit the map-ready CSV + a methodology summary.

WHY THIS DIFFERS FROM SAN FRANCISCO'S 03_process_sfr.py
--------------------------------------------------------
SF's comp detection relies on multi-year (2010-2025) assessed-value history:
a parcel whose total assessed value jumps >=8% year-over-year alongside a
recorded sale is a "jump-confirmed comp". Santa Barbara County's public
FeatureServer (see santabarbara_fetch.py) is a SINGLE current-snapshot table
-- there is no per-year history to diff, so a year-over-year "jump" genuinely
cannot be observed here.

Instead we use RECENCY OF LAST TRANSFER as the reset-detection signal,
tightened with the roll's own ValReason code:

  - ValReason is the code explaining why the CURRENT roll value is what it
    is. Inspecting the live data (78,337 SFR parcels) turned up 19 distinct
    codes. The two that plausibly mean "this year's value was set by an
    actual change-of-ownership reassessment to full cash value" are:
      'S' (Sale)      -- 2,830 SFR parcels, ~98% of them dated 2018-2019
      'T' (Transfer)  -- 244 SFR parcels, ~100% dated 2018-2019
    These are used as the comp set. Codes that were considered and
    deliberately EXCLUDED because they represent a value that is NOT a
    fresh market reset:
      'BVT' (base value transfer) and 'AGE' -- Prop 60/90/19-style transfers
        of an OLD base value onto a new parcel (e.g. senior replacement
        dwelling); the resulting assessed value is intentionally below
        market, not a market signal.
      'BEX' (base exclusion) -- parent-child/spousal-type exclusions; the
        recorded document changed ownership but the value was deliberately
        NOT reassessed, so it still reflects a stale prior base year.
      'NC' (new construction) -- only the incremental new-construction
        value is reassessed; the land (and any existing improvements)
        stays on its old base, so total $/sqft is not a clean market read.
      'IF' (inflation factor, the ordinary ~2%/yr Prop-13 growth -- the
        default/no-event case, by far the largest bucket at 65,257 parcels),
        'V', 'INC', 'NVA', 'BV', 'NS', 'DRT', 'PT', 'CXL', 'NP', 'BEX',
        'MA', and blank -- none of these reliably indicate a fresh
        change-of-ownership reset to market value; several (e.g. PT/DRT,
        Prop 8 decline-in-value reviews) are plausible but too rare (126
        and 139 SFR parcels respectively) and too ambiguous to include with
        confidence, so they are left out rather than risk contaminating the
        comp pool.

  - DocDate recency: comps additionally require a DocDate (last recorded
    document date) within RECENCY_YEARS of the snapshot's own most recent
    DocDate. IMPORTANT DATA QUIRK: every single DocDate in this entire
    132,472-parcel countywide feed -- regardless of ValReason -- tops out at
    2019-12-31, even though the service's own edit metadata shows it was
    last refreshed in late 2025. In other words, this feed's document-
    recording linkage appears to have stopped updating years ago even as
    other fields (assessed values, etc.) keep getting refreshed. Using a
    "within the last 2-3 years of TODAY" cutoff against this field would
    therefore find zero comps, so the cutoff is computed relative to the
    snapshot's OWN observed max DocDate instead (2019-12-31 as fetched;
    computed dynamically below in case that ever changes). This is a
    deliberate, documented deviation from a literal "last 2-3 years" reading
    of the assignment -- the intent (comps whose value was JUST reset by a
    real transfer) is preserved, only the reference point moves to match
    what the data actually contains. Because of this, every comp used here
    reflects a transfer clustered around 2018-2019.

  - Appreciation adjustment (added after an initial run flagged the above as
    stale): each comp's raw $/sqft is scaled from its OWN transaction year
    (the year of its DocDate, not a blanket 2019 assumption) to today's
    ("2025")-equivalent price via data/santabarbara-hpi.json -- FRED series
    ATNHPIUS06083A, "All-Transactions House Price Index for Santa Barbara
    County, CA" -- multiplying by hpi[CURRENT_YEAR] / hpi[comp_year]. This is
    the same technique and rationale as SF's 03_process_sfr.py uses against
    data/sf-hpi.json for its pre-2025 comps. Comp years outside the HPI
    series' range fall back to the nearest available year. This adjustment
    is what brings the countywide estimates up from ~2019-2020-equivalent
    market conditions to ~2025-equivalent, since essentially every comp here
    is 2018-2019 vintage (see the DocDate quirk above) and would otherwise
    systematically undervalue every estimate by however much Santa Barbara
    County home prices outran the ~2%/yr Prop-13 cap since then.

  - No SF-style neighborhood price-tier fallback: this schema has no
    neighborhood label field, and the assigned methodology for this county
    only calls for "nearest K=7 comps by location", so nearest-neighbor
    search runs directly against the full countywide comp pool (~3,000
    comps spread across the county) rather than a tiered/grouped pool.

  - Market value estimate = median $/sqft of the K=7 nearest (by lat/lon)
    comps x the subject's own sqft, floored at the subject's own current
    assessed value (identical floor logic and rationale to SF's script: a
    below-assessed estimate is essentially always the model undershooting,
    not a real declining-value home).

  - Tax-rate constants are the exact ones specified for cross-county
    comparability (see GENERAL_RATE_CURRENT / BOND_RATE / GENERAL_RATE_PROPOSED
    below), NOT San Francisco's own script's in-repo constants (which use a
    0.65 proposed rate); this pipeline uses the 0.70 proposed rate so every
    county's numbers line up on the same reform assumption.

Reads:  pipeline/tmp/santabarbara_snapshot_raw.json (santabarbara_fetch.py)
        data/santabarbara-hpi.json                  (FRED ATNHPIUS06083A, fetched by coordinator)
Writes: data/santabarbara-map-data.csv       (lat,lon,addr,sqft,assessed,market,subsidy,change,county)
        data/santabarbara-methodology.json   (methodology + summary stats)
"""
import csv
import datetime
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
RAW_SNAPSHOT = TMP_DIR / "santabarbara_snapshot_raw.json"
HPI_PATH = DATA_DIR / "santabarbara-hpi.json"
OUT_CSV = DATA_DIR / "santabarbara-map-data.csv"
OUT_SUMMARY = DATA_DIR / "santabarbara-methodology.json"

COUNTY_NAME = "Santa Barbara"

# ValReason codes treated as "this row's current value was just reset by an
# actual change-of-ownership sale/transfer" -- see module docstring for the
# reasoning behind including these two and excluding the rest.
CONFIRMED_VALREASONS = {"S", "T"}
RECENCY_YEARS = 3  # comps must have DocDate within this many years of the snapshot's own max DocDate
CURRENT_YEAR = 2025  # target year for the HPI appreciation adjustment, matching santabarbara-hpi.json's latest year

MIN_SQFT = 200
MAX_SQFT = 20000
MIN_PSF = 1.0
MAX_PSF = 10000.0

K = 7
GENERAL_RATE_CURRENT = 1.00
BOND_RATE = 0.18
GENERAL_RATE_PROPOSED = 0.70


def to_float(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_docdate(ms):
    """ArcGIS date fields come back as epoch milliseconds (UTC) or None."""
    if not ms:
        return None
    try:
        return datetime.datetime.utcfromtimestamp(ms / 1000).date()
    except (TypeError, ValueError, OSError):
        return None


def clean_addr(situs1):
    """Situs1 comes back as e.g. '1210 LOMITA LN' -- already clean, just
    title-case it to match the site's existing address style."""
    return " ".join((situs1 or "").split()).title()


def city_from_situs2(situs2):
    """Situs2 is like 'CARPINTERIA, CA 93013' or 'CARPINTERIA, CA 93013 3106'.
    Pull just the city so multi-city addresses in the county are distinguishable."""
    if not situs2:
        return ""
    city = situs2.split(",")[0]
    return city.strip().title()


def load_parcels(raw_path):
    raw = json.load(open(raw_path))
    parcels = {}
    for r in raw:
        lat, lon = r.get("_lat"), r.get("_lon")
        if lat is None or lon is None:
            continue
        sqft = to_float(r.get("SqFootage"))
        if sqft < MIN_SQFT or sqft > MAX_SQFT:
            continue
        land = to_float(r.get("LandValue"))
        impr = to_float(r.get("StrImpr"))
        fix = to_float(r.get("TradeFix"))
        liv = to_float(r.get("LivImpr"))
        assessed_total = land + impr + fix + liv
        if assessed_total <= 0:
            continue
        apn = r.get("APN")
        parcels[apn] = {
            "apn": apn,
            "address": clean_addr(r.get("Situs1")),
            "city": city_from_situs2(r.get("Situs2")),
            "lat": lat, "lon": lon,
            "sqft": sqft,
            "year_built": r.get("YearBuilt"),
            "beds": to_float(r.get("Bedrooms")),
            "baths": to_float(r.get("Bathrooms")),
            "doc_date": parse_docdate(r.get("DocDate")),
            "val_reason": (r.get("ValReason") or "").strip(),
            "assessed_total": assessed_total,
        }
    return parcels


def build_comps(parcels, cutoff_date, hpi):
    """Detect recency+ValReason confirmed comps and appreciation-adjust each
    one's raw $/sqft to CURRENT_YEAR-equivalent via the Santa Barbara HPI,
    using the comp's OWN DocDate year (not a blanket assumption) so a comp
    from, say, 2016 is scaled up more than one from 2019."""
    hpi_years = sorted(int(y) for y in hpi)
    hpi_current = hpi[str(CURRENT_YEAR)]

    def hpi_for_year(year):
        y = min(max(year, hpi_years[0]), hpi_years[-1])  # clamp to series range
        return hpi[str(y)]

    comps = []
    for apn, p in parcels.items():
        if p["val_reason"] not in CONFIRMED_VALREASONS:
            continue
        if not p["doc_date"] or p["doc_date"] < cutoff_date:
            continue
        raw_psf = p["assessed_total"] / p["sqft"]
        if raw_psf < MIN_PSF or raw_psf > MAX_PSF:
            continue
        comp_year = p["doc_date"].year
        adjusted_psf = raw_psf * (hpi_current / hpi_for_year(comp_year))
        comps.append({
            "apn": apn, "lat": p["lat"], "lon": p["lon"],
            "price_per_sqft": adjusted_psf, "raw_price_per_sqft": raw_psf,
            "doc_date": p["doc_date"], "comp_year": comp_year,
        })
    return comps


def nearest_k_indices(target_lat, target_lon, target_apn, comp_lat, comp_lon, comp_apn, k):
    lat0 = math.radians(34.5)  # Santa Barbara County's approximate latitude
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
    return valid if len(valid) > 0 else None


def main():
    print("loading Santa Barbara SFR snapshot...", file=sys.stderr)
    parcels = load_parcels(RAW_SNAPSHOT)
    print("usable parcels:", len(parcels), file=sys.stderr)

    hpi = json.load(open(HPI_PATH))

    max_doc_date = max((p["doc_date"] for p in parcels.values() if p["doc_date"]), default=None)
    if max_doc_date is None:
        raise RuntimeError("no parcels have a DocDate; cannot establish a recency cutoff")
    cutoff_date = max_doc_date - datetime.timedelta(days=365 * RECENCY_YEARS)
    print(f"snapshot's own max DocDate: {max_doc_date} -> recency cutoff: {cutoff_date}", file=sys.stderr)

    comps = build_comps(parcels, cutoff_date, hpi)
    print("recency+ValReason confirmed comps:", len(comps), file=sys.stderr)
    if len(comps) < K:
        raise RuntimeError(f"only {len(comps)} comps found, need at least {K}")
    comp_years = sorted(set(c["comp_year"] for c in comps))
    print(f"comp DocDate years span: {comp_years[0]}-{comp_years[-1]}", file=sys.stderr)

    comp_lat = np.array([c["lat"] for c in comps])
    comp_lon = np.array([c["lon"] for c in comps])
    comp_psf = np.array([c["price_per_sqft"] for c in comps])       # HPI-adjusted, used for the actual estimate
    comp_raw_psf = np.array([c["raw_price_per_sqft"] for c in comps])  # pre-adjustment, kept only to report the shift
    comp_apn = np.array([c["apn"] for c in comps])

    fieldnames = ["lat", "lon", "addr", "sqft", "assessed", "market", "subsidy", "change", "county"]
    rows_written = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0
    market_before_hpi_all, market_after_hpi_all = [], []  # for reporting how much the HPI adjustment shifted estimates

    with open(OUT_CSV, "w", newline="") as fcsv:
        writer = csv.writer(fcsv)
        writer.writerow(fieldnames)

        for apn, p in parcels.items():
            idx = nearest_k_indices(p["lat"], p["lon"], apn, comp_lat, comp_lon, comp_apn, K)
            if idx is None:
                continue
            med_psf = float(np.median(comp_psf[idx]))
            med_raw_psf = float(np.median(comp_raw_psf[idx]))
            est_market_value = med_psf * p["sqft"]
            est_market_value_unadjusted = med_raw_psf * p["sqft"]
            # Same floor rationale as SF: an estimate below the home's own current
            # assessed value is essentially always the comp model undershooting,
            # not a real declining-value home.
            est_market_value = max(est_market_value, p["assessed_total"])
            est_market_value_unadjusted = max(est_market_value_unadjusted, p["assessed_total"])
            market_before_hpi_all.append(est_market_value_unadjusted)
            market_after_hpi_all.append(est_market_value)

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

            addr = p["address"]
            if p["city"]:
                addr = f"{addr}, {p['city']}"

            writer.writerow([
                round(p["lat"], 5), round(p["lon"], 5), addr,
                int(p["sqft"]), round(p["assessed_total"]), round(est_market_value),
                round(subsidy), round(change), COUNTY_NAME,
            ])
            rows_written += 1

    print(f"WROTE {rows_written} rows -> {OUT_CSV}", file=sys.stderr)

    hpi_shift_ratio = statistics.mean(
        after / before for before, after in zip(market_before_hpi_all, market_after_hpi_all) if before > 0
    )
    print(f"mean HPI-adjustment shift in market value: {round(100 * (hpi_shift_ratio - 1), 1)}%", file=sys.stderr)

    summary = {
        "methodology": {
            "source_assessed_values": "Santa Barbara County AssessorParcels FeatureServer (ArcGIS REST), layer 0",
            "source_url": (
                "https://services6.arcgis.com/STxBI5x7lq6k9HIB/arcgis/rest/services/"
                "AssessorParcels/FeatureServer/0"
            ),
            "scope": "Countywide, LandUse='SINGLE FAMILY RESIDENCE'",
            "no_multiyear_history": (
                "Unlike San Francisco's assessor roll, this county's public feed is a single "
                "current-snapshot table with no per-year history, so a year-over-year assessed-"
                "value 'jump' cannot be detected here."
            ),
            "market_value_estimation": (
                "Comps are parcels whose current roll value was just reset by an actual change-of-"
                "ownership transfer, identified via two signals combined: (1) ValReason (the code "
                f"explaining why this year's value is what it is) is one of {sorted(CONFIRMED_VALREASONS)} "
                "-- 'S' (Sale) or 'T' (Transfer), the two codes among the 19 observed live values that "
                "plausibly mean a full reassessment to market value occurred, as opposed to routine ~2%/yr "
                "Prop-13 growth ('IF', the majority case), a transferred-in low base value from a Prop "
                "60/90/19-style transfer ('BVT', 'AGE'), a parent-child/spousal exclusion that keeps the "
                "prior base ('BEX'), or a partial new-construction-only reassessment ('NC'). (2) DocDate "
                "(last recorded document date) falls within "
                f"{RECENCY_YEARS} years of the snapshot's own most recent observed DocDate "
                f"({max_doc_date.isoformat()}), i.e. on/after {cutoff_date.isoformat()}. Data quirk: every "
                "DocDate in the entire 132k-parcel countywide feed tops out at 2019-12-31 regardless of "
                "ValReason, even though the service's edit metadata shows a late-2025 refresh -- the "
                "document-recording linkage in this particular feed appears to have stopped updating years "
                "ago even as assessed values keep getting refreshed. The recency cutoff is therefore anchored "
                "to the snapshot's own max DocDate rather than to today's date (which would find zero comps), "
                "so comps end up concentrated around 2018-2019 transfers. CORRECTION APPLIED: because "
                "essentially every comp is 2018-2019 vintage, each comp's raw $/sqft is appreciation-adjusted "
                f"to {CURRENT_YEAR}-equivalent using its own DocDate year against data/santabarbara-hpi.json "
                "(FRED series ATNHPIUS06083A, 'All-Transactions House Price Index for Santa Barbara County, "
                f"CA') -- multiplying by hpi[{CURRENT_YEAR}]/hpi[comp_year], identical technique to how SF's "
                "03_process_sfr.py adjusts its own older comps via data/sf-hpi.json. This is what brings the "
                f"countywide estimates up to ~{CURRENT_YEAR}-equivalent market conditions instead of the "
                "~2019-2020 conditions an unadjusted read of these comps would imply. "
                f"For each parcel, the {K} nearest confirmed comps by straight-line lat/lon distance set the "
                "estimate: median $/sqft of those comps x the subject's own sqft. There is no neighborhood-"
                "tier fallback grouping (unlike SF) since this schema has no neighborhood label field and "
                "the assigned method for this county only calls for nearest-K comps; the comp pool is "
                "instead searched directly, countywide. Estimated market value is floored at the parcel's "
                "own current assessed value (LandValue+StrImpr+TradeFix+LivImpr): a below-assessed estimate "
                "is essentially always the comp model undershooting rather than a real declining-value home, "
                "same rationale as SF's script."
            ),
            "excluded_valreason_codes": {
                "IF": "Inflation Factor -- ordinary ~2%/yr Prop 13 growth, the default/no-event case (65,257 of 78,337 SFR parcels)",
                "BVT": "Base Value Transfer -- Prop 60/90/19-style transferred-in low base value, intentionally below market",
                "AGE": "age-based (55+) base year transfer -- same as BVT, transferred base is not a market signal",
                "BEX": "base year exclusion (e.g. parent-child/spousal) -- ownership changed but value was NOT reassessed",
                "NC": "New Construction -- only the incremental new-construction value is reassessed, not the whole parcel",
                "other": "V, INC, NVA, BV, NS, DRT, PT, CXL, NP, MA, blank -- none reliably indicate a fresh change-of-"
                         "ownership market reset, and/or too rare to use with confidence",
            },
            "tax_assumptions": {
                "current_general_rate_pct": GENERAL_RATE_CURRENT,
                "bond_rate_pct_used": BOND_RATE,
                "bond_rate_note": "Held at San Francisco's bond rate for cross-county consistency, per the shared methodology; a deliberate simplification, not Santa Barbara County's actual bond rate.",
                "proposed_general_rate_pct": GENERAL_RATE_PROPOSED,
            },
            "appreciation_adjustment": {
                "source": "data/santabarbara-hpi.json (FRED ATNHPIUS06083A, Santa Barbara County All-Transactions HPI)",
                "target_year": CURRENT_YEAR,
                "comp_docdate_year_range": f"{comp_years[0]}-{comp_years[-1]}",
                "mean_market_value_shift_from_adjustment_pct": round(100 * (hpi_shift_ratio - 1), 1),
            },
            "generated": datetime.date.today().isoformat(),
        },
        "counts": {
            "total_sfr_parcels_fetched": len(parcels),
            "recency_and_valreason_confirmed_comps": len(comps),
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
    with open(OUT_SUMMARY, "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
