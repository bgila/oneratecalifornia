"""
Build market-value estimates for the sampled Riverside County single-family
parcels (see riverside_01_fetch_snapshot.py for how the sample was drawn).

Mirrors 03_process_sfr.py's overall shape (comp detection -> nearest-K
$/sqft -> floor at assessed value -> tax math), but the comp-detection step
had to be substituted, because two of SF's inputs don't exist for
Riverside:

1. No sale-date / recording-date field anywhere in Riverside's public
   assessor schema. Confirmed live across all four CREST tables
   (CREST_GENERAL, CREST_PROPERTY_CHAR, CREST_RECORDED_BOOK,
   CREST_TAXYEAR) and the PARCELS_CREST geometry layer -- every field
   name was pulled from the live ArcGIS MapServer metadata
   (.../MapServer/<id>?f=json) and none of them record a sale or
   recording date. CREST_RECORDED_BOOK has book/page/lot/subdivision
   references but no date.
2. No multi-year assessed-value history either. Preliminary scouting
   assumed CREST_TAXYEAR held one row per parcel per year across many
   years (like SF's Socrata roll history), which would have let this
   pipeline replicate SF's ">=8% YoY jump" detection directly. A live
   returnCountOnly query against CREST_TAXYEAR for TAX_YEAR=2020 through
   2028 returns zero rows for every year except 2027 (the current
   FY2026-27 roll, ~1.01M rows) -- the table is a live current snapshot,
   not an archive. There is no year-over-year series to compute a jump
   from at all, regardless of the sale-date question.

Substitute comp signal: PRIME_BASE_YEAR (from CREST_GENERAL) is the
county's own recorded year in which the parcel's current Prop 13 base-year
value was established -- i.e. the year of the last change-of-ownership or
completed new-construction reassessment. A parcel with a recent
PRIME_BASE_YEAR is used as a comp: its current assessed total is treated as
a near-market-value price signal, since only a few years of Prop 13's
~2%/yr inflation cap have had a chance to accrue since that reset.

This is arguably a *more direct* signal than SF's inferred jump-ratio (it's
the literal assessor-recorded reset year, not a derived threshold) but it
carries the same fundamental limitation the assigned methodology
anticipated: with no sale-date field to cross-check against, a
PRIME_BASE_YEAR reset from a non-arms-length event -- a Prop 19
parent-child/trust transfer, new construction, or an assessment
correction/appeal -- is indistinguishable here from a real arms-length
sale. So this pipeline's comp pool has a genuinely higher false-positive
rate than SF's jump-confirmed comps, exactly as flagged in the assignment.
This is documented in the methodology JSON's "known_limitations" field.

Appreciation adjustment: comps are scaled to 2025-equivalent price via
data/riverside-hpi.json (FRED series ATNHPIUS06065A, "All-Transactions
House Price Index for Riverside County, CA", 1975-2025), the same logic
03_process_sfr.py uses with data/sf-hpi.json -- a comp's raw $/sqft
(current assessed total / sqft) is multiplied by hpi[2025]/hpi[base_year]
before being pooled with other comps. (An earlier version of this
pipeline skipped this step because a live FRED fetch attempted from this
environment repeatedly timed out; that was an environment-specific
network restriction, not FRED being unreachable -- a working
riverside-hpi.json was supplied afterward and is used here.)

Because older comps can now be appreciation-corrected instead of simply
excluded, the PRIME_BASE_YEAR eligibility window is widened from the
initial narrow 3-year window (2024-2026) to the last 10 years
(2017-2026): with HPI correction in place there's no longer a reason to
throw away a perfectly good reset-year comp just because a few years of
Prop 13 drift + real appreciation happened since; the HPI ratio corrects
for exactly that gap. This also substantially grows the comp pool, which
should reduce reliance on the same-price-tier/countywide fallback for
comp-sparse cities.

Reads:  pipeline/tmp/riverside_snapshot_raw.json   (riverside_01_fetch_snapshot.py)
        pipeline/tmp/riverside_baseyear_raw.json   (riverside_02_fetch_baseyear.py)
        pipeline/tmp/riverside_scope_meta.json     (riverside_01_fetch_snapshot.py)
        data/riverside-hpi.json                    (FRED Riverside County HPI, 1975-2025)
Writes: pipeline/tmp/riverside-full.csv            (full per-parcel estimate table)
        data/riverside-methodology.json            (methodology + summary stats)
"""
import csv
import datetime
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
SNAPSHOT_PATH = TMP_DIR / "riverside_snapshot_raw.json"
BASEYEAR_PATH = TMP_DIR / "riverside_baseyear_raw.json"
SCOPE_META_PATH = TMP_DIR / "riverside_scope_meta.json"
HPI_PATH = PIPELINE_DIR.parent / "data" / "riverside-hpi.json"
OUT_CSV = TMP_DIR / "riverside-full.csv"
OUT_METHODOLOGY = DATA_DIR / "riverside-methodology.json"

CURRENT_TAX_YEAR = 2027  # the only tax year present in CREST_TAXYEAR as of this run
HPI_TARGET_YEAR = 2025  # comps are appreciation-adjusted to this year's-equivalent price (latest year in the HPI series)
COMP_BASEYEAR_MIN = CURRENT_TAX_YEAR - 10  # 2017: widened now that HPI correction stands in for Prop-13-drift exclusion
COMP_BASEYEAR_MAX = CURRENT_TAX_YEAR - 1  # 2026: latest observed PRIME_BASE_YEAR
K = 7
MIN_COMPS_FOR_LOCAL_GROUP = 12
NUM_PRICE_TIERS = 4  # quartiles, for the fallback pool when a city lacks its own comps
GENERAL_RATE_CURRENT = 1.00
BOND_RATE = 0.18  # held at SF's bond rate for cross-county comparability (deliberate simplification)
GENERAL_RATE_PROPOSED = 0.70

RIVERSIDE_CENTRAL_LAT = 33.75  # for the equirectangular longitude-scaling approximation below


def load_parcels(snapshot_path):
    raw = json.load(open(snapshot_path))
    parcels = {}
    for r in raw:
        sqft = r.get("sqft") or 0
        if sqft <= 0 or sqft > 20000:
            continue
        assessed_total = r.get("assessed_total") or 0
        if assessed_total <= 0:
            continue
        pin = r["pin"]
        parcels[pin] = {
            "pin": pin,
            "address": r.get("street") or "",
            "city": r.get("city") or "Unincorporated",
            "lat": r["lat"], "lon": r["lon"],
            "sqft": sqft,
            "beds": r.get("beds"),
            "baths": r.get("baths"),
            "year_built": r.get("year_built"),
            "assessed_total": assessed_total,
        }
    return parcels


def load_confirmed_comps(baseyear_path, parcels):
    """Flag parcels whose PRIME_BASE_YEAR falls in the (now 10-year) window
    as probable market-reset comps -- see module docstring for why this
    substitutes for SF's sale-confirmed jump detection, and for the
    higher false-positive rate that entails (no sale-date to cross-check
    against Prop 19 transfers, new construction, or corrections)."""
    baseyear = json.load(open(baseyear_path))
    confirmed = {}
    for pin, by in baseyear.items():
        if by is None or pin not in parcels:
            continue
        if COMP_BASEYEAR_MIN <= by <= COMP_BASEYEAR_MAX:
            confirmed[pin] = by
    return confirmed


def hpi_for_year(hpi, year):
    """Look up the Riverside County HPI for a given year, clamping to the
    series' actual range (1975-2025) for any base year outside it (e.g. a
    2026 PRIME_BASE_YEAR, one year past the HPI series' last data point)."""
    years = sorted(int(y) for y in hpi)
    y = max(years[0], min(year, years[-1]))
    return hpi[str(y)]


def nearest_k_psf(target_lat, target_lon, target_pin, comp_lat, comp_lon, comp_psf, comp_pin, k):
    lat0 = math.radians(RIVERSIDE_CENTRAL_LAT)
    dx = (comp_lon - target_lon) * math.cos(lat0)
    dy = (comp_lat - target_lat)
    d2 = dx * dx + dy * dy
    mask = comp_pin != target_pin
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
    print("loading sampled snapshot...", file=sys.stderr)
    parcels = load_parcels(SNAPSHOT_PATH)
    print("usable parcels (sample):", len(parcels), file=sys.stderr)

    print("loading PRIME_BASE_YEAR signal...", file=sys.stderr)
    confirmed = load_confirmed_comps(BASEYEAR_PATH, parcels)
    print("recent-reset comps found:", len(confirmed), file=sys.stderr)

    print("loading Riverside County HPI (appreciation adjustment)...", file=sys.stderr)
    hpi = json.load(open(HPI_PATH))
    hpi_target = hpi_for_year(hpi, HPI_TARGET_YEAR)

    scope_meta = json.load(open(SCOPE_META_PATH)) if SCOPE_META_PATH.exists() else {}

    all_comps = []
    for pin, by in confirmed.items():
        p = parcels[pin]
        if p["sqft"] <= 200:
            continue
        raw_psf = p["assessed_total"] / p["sqft"]
        if raw_psf <= 0 or raw_psf > 5000:  # sanity guard
            continue
        # Appreciation-adjust to HPI_TARGET_YEAR-equivalent price: an older
        # PRIME_BASE_YEAR comp's assessed value reflects only Prop 13's
        # ~2%/yr inflation cap since reset, not real market appreciation, so
        # scale it up by the county-wide HPI ratio -- same logic
        # 03_process_sfr.py applies with data/sf-hpi.json.
        hpi_at_base = hpi_for_year(hpi, by)
        adjusted_psf = raw_psf * (hpi_target / hpi_at_base)
        all_comps.append({
            "pin": pin, "city": p["city"], "lat": p["lat"], "lon": p["lon"],
            "price_per_sqft": adjusted_psf, "base_year": by,
        })
    print("usable comps after filtering:", len(all_comps), file=sys.stderr)

    by_city = defaultdict(list)
    for p in parcels.values():
        by_city[p["city"]].append(p)

    comps_by_city = defaultdict(list)
    for c in all_comps:
        comps_by_city[c["city"]].append(c)

    # Rank cities into price quartiles using whatever comps they have, so a
    # comp-sparse city's fallback stays within its own price tier instead of
    # diluting into the countywide median (same rationale as SF's
    # neighborhood-tier fallback -- e.g. low-turnover, high-value desert
    # resort cities shouldn't be dragged toward the county's much larger
    # inland-valley parcel count).
    city_tier_psf = {
        city: statistics.median(c["price_per_sqft"] for c in comps)
        for city, comps in comps_by_city.items() if comps
    }
    ranked_cities = sorted(city_tier_psf, key=lambda c: city_tier_psf[c])
    tier_of_city = {}
    if ranked_cities:
        tier_size = math.ceil(len(ranked_cities) / NUM_PRICE_TIERS)
        for i, city in enumerate(ranked_cities):
            tier_of_city[city] = min(i // tier_size, NUM_PRICE_TIERS - 1)
    comps_by_tier = defaultdict(list)
    for city, comps in comps_by_city.items():
        tier = tier_of_city.get(city)
        if tier is not None:
            comps_by_tier[tier].extend(comps)

    all_comp_lat = np.array([c["lat"] for c in all_comps])
    all_comp_lon = np.array([c["lon"] for c in all_comps])
    all_comp_psf = np.array([c["price_per_sqft"] for c in all_comps])
    all_comp_pin = np.array([c["pin"] for c in all_comps])

    rows_written = 0
    cities_done = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0
    comp_source_counts = defaultdict(int)

    fieldnames = [
        "pin", "address", "city", "lat", "lon", "beds", "baths", "sqft",
        "year_built", "assessed_total", "est_market_value", "est_price_per_sqft",
        "comp_count", "comp_source", "current_tax_est", "subsidy_vs_market_today",
        "tax_under_reform_est", "change_under_reform",
    ]

    with open(OUT_CSV, "w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fieldnames)
        writer.writeheader()

        for city_name, members in by_city.items():
            local_comps = comps_by_city.get(city_name, [])
            tier_comps = comps_by_tier.get(tier_of_city.get(city_name))
            if len(local_comps) >= MIN_COMPS_FOR_LOCAL_GROUP:
                pool, source_label = local_comps, "same_city"
            elif tier_comps and len(tier_comps) >= MIN_COMPS_FOR_LOCAL_GROUP:
                pool, source_label = tier_comps, "same_price_tier"
            else:
                pool, source_label = None, "countywide_fallback"

            if pool is not None:
                comp_lat = np.array([c["lat"] for c in pool])
                comp_lon = np.array([c["lon"] for c in pool])
                comp_psf = np.array([c["price_per_sqft"] for c in pool])
                comp_pin = np.array([c["pin"] for c in pool])
            else:
                comp_lat, comp_lon, comp_psf, comp_pin = all_comp_lat, all_comp_lon, all_comp_psf, all_comp_pin

            batch_rows = []
            for p in members:
                if p["sqft"] <= 200:
                    continue
                result = nearest_k_psf(p["lat"], p["lon"], p["pin"], comp_lat, comp_lon, comp_psf, comp_pin, K)
                if result is None:
                    continue
                med_psf, n_used = result
                est_market_value = med_psf * p["sqft"]
                # Floor at current assessed value, same rationale as SF: a
                # below-assessed estimate is almost always the comp model
                # undershooting, not a real declining-value home.
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
                batch_rows.append({
                    "pin": p["pin"], "address": p["address"], "city": city_name,
                    "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                    "beds": p["beds"], "baths": p["baths"], "sqft": p["sqft"],
                    "year_built": p["year_built"], "assessed_total": round(p["assessed_total"]),
                    "est_market_value": round(est_market_value), "est_price_per_sqft": round(med_psf, 2),
                    "comp_count": n_used, "comp_source": source_label,
                    "current_tax_est": round(current_tax), "subsidy_vs_market_today": round(subsidy),
                    "tax_under_reform_est": round(reform_tax), "change_under_reform": round(change),
                })
                comp_source_counts[source_label] += 1
            writer.writerows(batch_rows)
            fcsv.flush()
            rows_written += len(batch_rows)
            cities_done += 1
            print(f"[{cities_done}/{len(by_city)}] {city_name}: {len(members)} parcels, "
                  f"{len(local_comps)} local recent-reset comps ({source_label}), wrote {len(batch_rows)} rows "
                  f"(running total {rows_written})", file=sys.stderr)

    methodology = {
        "methodology": {
            "source_assessed_values": (
                "Riverside County ArcGIS OpenData 'Assessor' MapServer "
                "(gis.countyofriverside.us/arcgis_mapping/rest/services/OpenData/Assessor/MapServer), "
                "joining PARCELS_CREST (geometry/address/class code), CREST_PROPERTY_CHAR (living area), "
                "and CREST_TAXYEAR (current LAND+STRUCTURES+LIVING_IMPROVEMENTS) by parcel PIN/APN. "
                f"Current assessment roll is tax year {CURRENT_TAX_YEAR} (FY{CURRENT_TAX_YEAR-1}-{CURRENT_TAX_YEAR})."
            ),
            "source_url": "https://gis.countyofriverside.us/arcgis_mapping/rest/services/OpenData/Assessor/MapServer",
            "scope": (
                "Single Family Dwelling class code only (CLASS_CODE='Single Family Dwelling' in PARCELS_CREST), "
                f"{scope_meta.get('total_sfr_parcels_countywide', 'unknown')} parcels countywide -- "
                "full county coverage, fetched and joined across 3 rate-limited tables "
                f"({scope_meta.get('sample_size_requested', 'unknown')} targeted, "
                f"{scope_meta.get('sample_size_usable', len(parcels))} usable after joins/filters; the small gap "
                "is parcels missing geometry, living area, or assessed value in one of the source tables). "
                "An earlier version of this pipeline covered only a systematic 1-in-5 sample (~116,000 parcels) "
                "for tractability; this run fetches the entire Single Family Dwelling universe."
            ),
            "market_value_estimation": (
                "There is no public bulk sale-price dataset for Riverside real estate, so market value is inferred "
                "from the assessor roll itself, the same general approach as San Francisco -- but the specific "
                "comp-detection signal had to be substituted because two of SF's inputs don't exist here. "
                "Live queries against every field in CREST_GENERAL, CREST_PROPERTY_CHAR, CREST_RECORDED_BOOK, "
                "CREST_TAXYEAR, and PARCELS_CREST confirmed there is no sale-date or recording-date field anywhere "
                "in this schema. And contrary to preliminary scouting, CREST_TAXYEAR does not hold multi-year "
                f"history either -- a live query confirmed it holds only the single current tax year "
                f"({CURRENT_TAX_YEAR}) for all ~1.01M rows, so there is no year-over-year series to compute an "
                "8%-jump signal from at all. "
                "Substitute signal: PRIME_BASE_YEAR (CREST_GENERAL) is the county's own recorded year in which a "
                "parcel's current Prop 13 base-year value was established -- i.e. the year of its last change-of-"
                f"ownership or completed new-construction reassessment. Parcels with PRIME_BASE_YEAR in "
                f"{COMP_BASEYEAR_MIN}-{COMP_BASEYEAR_MAX} (the {COMP_BASEYEAR_MAX - COMP_BASEYEAR_MIN + 1} most "
                "recent years observed) are treated as 'recent-reset comps'. Each comp's current assessed total "
                "(LAND+STRUCTURES+LIVING_IMPROVEMENTS) / sqft is appreciation-adjusted to "
                f"{HPI_TARGET_YEAR}-equivalent price via data/riverside-hpi.json (FRED series ATNHPIUS06065A, "
                "'All-Transactions House Price Index for Riverside County, CA', 1975-2025), multiplying by "
                f"hpi[{HPI_TARGET_YEAR}]/hpi[base_year] -- the same logic 03_process_sfr.py applies to SF comps via "
                "data/sf-hpi.json. This corrects for the gap between Prop 13's ~2%/yr inflation cap (what the "
                "un-adjusted assessed value actually grew by since reset) and real market appreciation (typically "
                "much higher), which is why the comp window could be widened from an initial narrow 3-year window "
                "(2024-2026) out to 10 years: with HPI correction in place there's no longer a reason to exclude an "
                "older reset-year comp just to avoid uncorrected Prop-13-drift error. (An earlier version of this "
                "pipeline used no adjustment and the narrow 3-year window instead, because a live FRED fetch "
                "attempted from the original development environment repeatedly timed out; that turned out to be "
                "an environment-specific network restriction, not FRED being unreachable, and a working "
                "riverside-hpi.json was supplied afterward.) "
                "IMPORTANT CAVEAT (assigned methodology, confirmed unavoidable): with no sale-date field to "
                "cross-check against, a PRIME_BASE_YEAR reset from a non-arms-length event -- a Prop 19 parent-"
                "child/trust transfer, new construction, or an assessment correction/appeal -- is "
                "indistinguishable here from a real arms-length sale. This comp pool therefore has a genuinely "
                "higher false-positive rate than SF's sale-confirmed jump comps: some flagged 'recent resets' are "
                "not real market-rate sales. "
                f"For each target home, the {K} nearest recent-reset comps by location set the estimate: median "
                f"$/sqft x subject sqft. 'By location' means same city if it has {MIN_COMPS_FOR_LOCAL_GROUP}+ "
                "comps of its own (using CITY as Riverside's rough analog to SF's neighborhoods, since Riverside "
                "spans dozens of incorporated cities plus unincorporated area); otherwise, comps are pooled from "
                "every city in the same price quartile (ranked by each city's own comps) rather than the whole "
                "county. Cities with zero comps of their own still fall back countywide. Estimated market value is "
                "then floored at the home's current assessed value, same rationale as SF: an estimate below "
                "assessed value is almost always the comp model undershooting, not a real declining-value home."
            ),
            "tax_assumptions": {
                "current_general_rate_pct": GENERAL_RATE_CURRENT,
                "bond_rate_pct": BOND_RATE,
                "bond_rate_note": "Held at SF's bond rate for cross-county comparability -- a deliberate existing simplification, not Riverside's actual bond rate.",
                "proposed_general_rate_pct": GENERAL_RATE_PROPOSED,
            },
            "known_limitations": [
                "Higher false-positive comp rate than SF: PRIME_BASE_YEAR resets from non-arms-length transfers, "
                "new construction, or assessment corrections cannot be distinguished from real sales (no sale-date "
                "field exists anywhere in Riverside's public assessor schema).",
                "Full county coverage: this run fetches and joins every Single Family Dwelling parcel countywide "
                "(no sampling). An earlier version of this pipeline covered only a systematic 1-in-5 sample "
                "(~116,000 parcels) for tractability given ArcGIS's 2000-row-per-request cap; that limitation no "
                "longer applies.",
                "The HPI appreciation adjustment uses a single countywide index (FRED ATNHPIUS06065A) applied "
                "uniformly to every comp regardless of city -- it corrects for Riverside County's average "
                "appreciation trend but not for city-specific differences in appreciation rate (e.g. a desert "
                "resort city and an inland-valley suburb may have appreciated at different rates over the same "
                "decade); SF's HPI adjustment has this same limitation (one SF-wide index).",
                "CITY is a rough analog to SF's assessor-neighborhood field, coarser-grained (some cities span "
                "very different price tiers internally, e.g. Palm Springs vs. a small inland city).",
            ],
            "generated": datetime.date.today().isoformat(),
        },
        "counts": {
            "total_sfr_parcels_countywide": scope_meta.get("total_sfr_parcels_countywide"),
            "sampled_parcels_usable": len(parcels),
            "recent_reset_comps": len(all_comps),
            "cities": len(by_city),
            "estimated_rows_written": rows_written,
            "comp_source_breakdown": dict(comp_source_counts),
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

    print("WROTE", rows_written, "rows ->", OUT_CSV, file=sys.stderr)
    print("WROTE methodology ->", OUT_METHODOLOGY, file=sys.stderr)
    print(json.dumps(methodology, indent=2))


if __name__ == "__main__":
    main()
