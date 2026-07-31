"""
Build market-value estimates for every Alameda County single-family
residential (Use_Code=1100) parcel.

This is a direct adaptation of 03_process_sfr.py's jump-confirmed-comps
approach, with two real adaptations forced by what Alameda's public data
actually contains (both verified live against the county's ArcGIS org, not
assumed from the initial scouting brief):

1. NO BUILDING SQUARE FOOTAGE ANYWHERE. SF's dataset has a property_area
   field; Alameda's does not -- checked every field on every relevant
   layer (the 7 Secured Tax Roll tables and the Parcels layer) and the
   county's full ~160-dataset open-data catalog, and there is no
   building-characteristics / improvement-records dataset at all. So the
   SF approach of "median $/sqft of nearest comps x subject's own sqft"
   has no sqft to multiply by. Adaptation: comps and estimates here work
   in whole-parcel dollars instead of $/sqft -- for each home, the
   estimate is the median TOTAL assessed value (post-jump, nominal) of
   the K=7 nearest jump-confirmed comps, directly, with no per-sqft
   normalization step. This is coarser than SF's per-sqft approach (two
   nearby same-city homes of very different sizes now pull toward the
   same estimate), but it's the only estimate this data supports without
   fabricating a size figure. The `lot_sqft` column carried into the
   output CSV is the parcel's LOT square footage (from its boundary
   polygon in the Parcels layer), not building square footage -- it is
   informational only and plays no role in the estimation math.

2. APPRECIATION ADJUSTMENT VIA FRED'S ALAMEDA COUNTY HPI. Same logic as
   SF's 03_process_sfr.py: each comp's nominal post-jump value is scaled to
   today's-equivalent price via data/alameda-hpi.json (FRED series
   ATNHPIUS06001A, "All-Transactions House Price Index for Alameda County,
   CA"), so a comp confirmed in 2019-2020 doesn't read as artificially
   cheap next to a 2025-26 subject just because the county-wide market
   moved in between. (An earlier version of this script skipped this step
   because FRED wasn't reachable from the build sandbox at the time; that
   turned out to be a sandbox-specific network restriction, not a real
   FRED outage, so it's now wired in the same way SF does it.)

Otherwise the approach matches SF's exactly:

3. "Jump-confirmed comps": a parcel whose Total_Net_Value jumps >=8%
   year-over-year, AND has a Latest_Document_Date within a year of that
   jump (allowing for the lag between a recorded transfer and its
   reassessment appearing on the roll), is treated as a confirmed market
   reset. Alameda's Latest_Document_Date updates on ANY recorded document
   affecting the parcel (not exclusively sales -- e.g. it can reflect a
   deed of trust, easement, or other recording), so this filter is a
   slightly noisier version of SF's "current_sales_date" signal; requiring
   it to line up with an 8%+ value jump is what keeps it from just
   matching routine paperwork.
4. For each target home, the 7 nearest confirmed comps by location set the
   estimate: median value of those comps. "By location" means same city
   (Situs_City is the closest Alameda equivalent to SF's neighborhood
   field) if it has 12+ comps of its own; otherwise comps are pooled from
   every city in the same price quartile (ranked by each city's own
   comps' median value) rather than the whole county. Cities with zero
   comps of their own still fall back countywide.
5. Estimated market value is floored at the parcel's current assessed
   value (Total_Net_Value from the 2025-26 roll), for the same reason as
   SF: a below-assessed estimate here is almost always the comp model
   undershooting, not a real declining-value home.

Scope: Use_Code=1100 (single family residential) countywide, ~265,500
parcels -- no further down-sampling was needed since the fetch (see
alameda_01/02_fetch_*.py) completed in full within a reasonable runtime.

Reads:  pipeline/tmp/alameda_parcels_raw.json        (alameda_01_fetch_snapshot.py)
        pipeline/tmp/alameda_history_2019_2025.json  (alameda_02_fetch_history.py)
        data/alameda-hpi.json                        (FRED ATNHPIUS06001A, Alameda County HPI)
Writes: pipeline/tmp/alameda-full.csv   (full per-parcel estimate table)
        data/alameda-methodology.json   (methodology + summary stats)
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
RAW_SNAPSHOT = TMP_DIR / "alameda_parcels_raw.json"
HISTORY = TMP_DIR / "alameda_history_2019_2025.json"
HPI_PATH = PIPELINE_DIR.parent / "data" / "alameda-hpi.json"
OUT_CSV = TMP_DIR / "alameda-full.csv"
OUT_SUMMARY = DATA_DIR / "alameda-methodology.json"

JUMP_THRESHOLD = 1.08
HISTORY_START_YEAR = 2019
CURRENT_YEAR = 2025
K = 7
MIN_COMPS_FOR_LOCAL_GROUP = 12
NUM_PRICE_TIERS = 4  # quartiles, for the fallback pool when a city lacks its own comps
GENERAL_RATE_CURRENT = 1.00
BOND_RATE_SF = 0.18  # SF's bond rate, held fixed across counties for cross-county comparability
GENERAL_RATE_PROPOSED = 0.70

SQM_TO_SQFT = 10.7639104167  # 1 m^2 in ft^2, for Shape__Area (parcel LOT area, not building area)
MIN_LOT_SQFT = 200
MAX_LOT_SQFT = 500_000  # ~11.5 acres -- generous ceiling, just to drop obvious polygon errors


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_parcels(raw_snapshot_path):
    """Parse the current (2025-26) parcel snapshot into a dict of
    parcel_number -> parcel info."""
    raw = json.load(open(raw_snapshot_path))
    parcels = {}
    for r in raw:
        a = r["attributes"]
        lat, lon = a.get("lat"), a.get("lon")
        if lat is None or lon is None:
            continue
        lot_sqft = to_float(a.get("Shape__Area")) * SQM_TO_SQFT
        if lot_sqft < MIN_LOT_SQFT or lot_sqft > MAX_LOT_SQFT:
            continue
        assessed_total = to_float(a.get("TotalNetValue"))
        if assessed_total <= 0:
            continue
        pn = a.get("SortParcel")
        if not pn:
            continue
        street = " ".join(str(x) for x in (a.get("SitusStreetNumber"), a.get("SitusStreetName")) if x)
        unit = (a.get("SitusUnit") or "").strip()
        city = (a.get("SitusCity") or "Unknown").strip().title() or "Unknown"
        addr = street.strip()
        if unit:
            addr = f"{addr} #{unit}"
        parcels[pn] = {
            "parcel_number": pn,
            "address": addr,
            "city": city,
            "lat": lat, "lon": lon,
            "lot_sqft": lot_sqft,
            "assessed_total": assessed_total,
        }
    return parcels


def load_confirmed_comps(history_path):
    """Detect jump-confirmed reassessment events per parcel across the full
    history window (HISTORY_START_YEAR-CURRENT_YEAR).

    Keeps the most recent confirmed jump per parcel as its comp price signal
    (its nominal Total_Net_Value in that jump year -- appreciation-adjusted
    to today's-equivalent price in main(), same as SF's comps).
    """
    history = json.load(open(history_path))
    by_parcel_year = defaultdict(dict)
    for r in history:
        y = r["year"]
        total = to_float(r.get("total_net_value"))
        by_parcel_year[r["parcel_number"]][y] = {
            "total": total,
            "doc_date": r.get("doc_date"),
        }

    confirmed = {}  # parcel_number -> {"year": y, "total": assessed_total at year y}
    for pn, years in by_parcel_year.items():
        best = None
        for y in range(HISTORY_START_YEAR + 1, CURRENT_YEAR + 1):
            if y not in years or (y - 1) not in years:
                continue
            prev, cur = years[y - 1]["total"], years[y]["total"]
            if prev <= 0 or cur <= 0:
                continue
            ratio = cur / prev
            doc_date = years[y].get("doc_date")
            doc_near = bool(doc_date) and int(doc_date[:4]) in (y - 1, y)
            if doc_near and ratio >= JUMP_THRESHOLD:
                if best is None or y > best[0]:
                    best = (y, cur)
        if best:
            confirmed[pn] = {"year": best[0], "total": best[1]}
    return confirmed


def nearest_k_value(target_lat, target_lon, target_parcel, comp_lat, comp_lon, comp_value, comp_parcel, k):
    lat0 = math.radians(37.65)  # roughly Alameda County's central latitude
    dx = (comp_lon - target_lon) * math.cos(lat0)
    dy = (comp_lat - target_lat)
    d2 = dx * dx + dy * dy
    mask = comp_parcel != target_parcel
    d2 = np.where(mask, d2, np.inf)
    if len(d2) <= k:
        idx = np.argsort(d2)[:k]
    else:
        idx = np.argpartition(d2, k)[:k]
    valid = idx[np.isfinite(d2[idx])]
    if len(valid) == 0:
        return None
    return float(np.median(comp_value[valid])), len(valid)


def main():
    print("loading 2025-26 parcel snapshot...", file=sys.stderr)
    parcels = load_parcels(RAW_SNAPSHOT)
    print("usable parcels (2025-26 snapshot):", len(parcels), file=sys.stderr)

    print("loading multi-year history...", file=sys.stderr)
    confirmed = load_confirmed_comps(HISTORY)
    print("jump-confirmed comps found:", len(confirmed), file=sys.stderr)

    hpi = json.load(open(HPI_PATH))
    hpi_current = hpi[str(CURRENT_YEAR)]

    # attach comps (needs a lat/lon match from the 2025-26 snapshot), appreciation-adjusting
    # each comp's nominal post-jump value to today's-equivalent via the FRED Alameda County
    # HPI so a comp from earlier in the 2019-2025 window doesn't read as artificially cheap
    # next to a 2025-26 subject (same approach as SF's 03_process_sfr.py).
    all_comps = []
    for pn, info in confirmed.items():
        p = parcels.get(pn)
        if not p:
            continue
        raw_value = info["total"]
        if raw_value <= 0:
            continue
        comp_year = info["year"]
        hpi_at_comp = hpi.get(str(comp_year), hpi_current)
        adjusted_value = raw_value * (hpi_current / hpi_at_comp)
        all_comps.append({
            "parcel_number": pn,
            "city": p["city"],
            "lat": p["lat"], "lon": p["lon"],
            "value": adjusted_value,
            "comp_year": comp_year,
        })
    print("usable comps after join:", len(all_comps), file=sys.stderr)

    by_city = defaultdict(list)
    for p in parcels.values():
        by_city[p["city"]].append(p)

    comps_by_city = defaultdict(list)
    for c in all_comps:
        comps_by_city[c["city"]].append(c)

    # Rank cities into price quartiles using whatever comps they have (even if too
    # few to estimate from directly), so a comp-sparse city's fallback stays within
    # its own price tier instead of diluting into the whole county's median.
    city_tier_value = {
        city: statistics.median(c["value"] for c in comps) for city, comps in comps_by_city.items() if comps
    }
    ranked_cities = sorted(city_tier_value, key=lambda city: city_tier_value[city])
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
    all_comp_value = np.array([c["value"] for c in all_comps])
    all_comp_parcel = np.array([c["parcel_number"] for c in all_comps])

    rows_written = 0
    cities_done = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0

    fieldnames = [
        "parcel_number", "address", "city", "lat", "lon", "lot_sqft",
        "assessed_total", "est_market_value", "comp_count", "comp_source",
        "current_tax_est", "subsidy_vs_market_today",
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
                comp_value = np.array([c["value"] for c in pool])
                comp_parcel = np.array([c["parcel_number"] for c in pool])
            else:
                comp_lat, comp_lon, comp_value, comp_parcel = (
                    all_comp_lat, all_comp_lon, all_comp_value, all_comp_parcel
                )

            batch_rows = []
            for p in members:
                result = nearest_k_value(p["lat"], p["lon"], p["parcel_number"],
                                          comp_lat, comp_lon, comp_value, comp_parcel, K)
                if result is None:
                    continue
                med_value, n_used = result
                est_market_value = med_value
                # Floor market value at the current assessed value -- see module
                # docstring point 5 (same reasoning as SF's 03_process_sfr.py).
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
                    "parcel_number": p["parcel_number"], "address": p["address"], "city": city_name,
                    "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                    "lot_sqft": round(p["lot_sqft"]),
                    "assessed_total": round(p["assessed_total"]), "est_market_value": round(est_market_value),
                    "comp_count": n_used, "comp_source": source_label,
                    "current_tax_est": round(current_tax), "subsidy_vs_market_today": round(subsidy),
                    "tax_under_reform_est": round(reform_tax), "change_under_reform": round(change),
                })
            writer.writerows(batch_rows)
            fcsv.flush()
            rows_written += len(batch_rows)
            cities_done += 1
            print(f"[{cities_done}/{len(by_city)}] {city_name}: {len(members)} parcels, "
                  f"{len(local_comps)} local jump-confirmed comps ({source_label}), wrote {len(batch_rows)} rows "
                  f"(running total {rows_written})", file=sys.stderr)

    summary = {
        "methodology": {
            "source_assessed_values": "Alameda County ArcGIS FeatureServer: Assessor Office Secured Tax Roll layers (2019-20 through 2025-26) + Parcels layer",
            "source_url": "https://data.acgov.org/datasets/54cc3c88fa384b28b5c456df0868fc67_0 (2025-26 roll), https://data.acgov.org/datasets/2b026350b5dd40b18ed7a321fdcdba81_0 (Parcels geometry)",
            "scope": "Countywide, Use_Code=1100 (\"Single family residential homes used as such\") -- no down-sampling; full universe fetched",
            "market_value_estimation": (
                f"Comps are identified the same way as San Francisco's model: by detecting actual reassessment "
                f"events in {HISTORY_START_YEAR}-{CURRENT_YEAR} multi-year assessor history, not just a recorded "
                "document date. Any parcel whose Total_Net_Value jumps >=8% year-over-year, with a Latest_Document_Date "
                "within a year of that jump, is treated as a confirmed market reset. Alameda's Latest_Document_Date "
                "reflects ANY recorded document (not exclusively sales), so requiring it to coincide with an 8%+ value "
                "jump is what filters out routine non-sale recordings. Each comp's nominal post-jump value is then "
                "appreciation-adjusted to today's-equivalent price via the FRED Alameda County house price index "
                "(ATNHPIUS06001A, data/alameda-hpi.json), the same approach SF uses with its own FRED series, so a "
                "comp confirmed early in the 2019-2025 window doesn't read as artificially cheap next to a 2025-26 "
                "subject. "
                "UNLIKE SAN FRANCISCO: Alameda's public assessor data has no building square footage field anywhere "
                "(verified against every relevant layer and the county's full open-data catalog), so this model works "
                "in whole-parcel dollars rather than $/sqft. For each target home, the estimate is the median "
                f"appreciation-adjusted TOTAL assessed value of the {K} nearest jump-confirmed comps by location. "
                "'By location' means same city (Situs_City) if it has "
                f"{MIN_COMPS_FOR_LOCAL_GROUP}+ comps of its own; otherwise comps are pooled from every city in the same "
                "price quartile (ranked by each city's own comps) rather than the whole county. Cities with zero comps "
                "of their own still fall back countywide. Estimated market value is then floored at the home's current "
                "assessed value, for the same reason as SF: a below-assessed estimate is almost always the comp model "
                "undershooting rather than a real declining-value home. "
                "Known weak points: (1) whole-parcel-dollar comps blend differently-sized nearby homes together, "
                "coarser than SF's per-sqft approach; (2) Latest_Document_Date is a noisier transfer signal than SF's "
                "dedicated sale-date field."
            ),
            "sqft_caveat": (
                "The 'sqft' figure in the final map CSV is parcel LOT square footage (from the parcel boundary "
                "polygon's Shape__Area on the Parcels layer, converted m^2 -> ft^2), NOT building square footage -- "
                "Alameda's public assessor data does not publish building square footage in any layer. It is "
                "informational only and is not used anywhere in the market-value estimation math above."
            ),
            "history_window_caveat": (
                "Aimed for 2018-19 through 2025-26 per the assignment; the earliest Secured Tax Roll FeatureServer "
                "layer that actually exists in Alameda's ArcGIS org is 2019-20 (there is a "
                "'Deleted_Parcel_List_2018_to_2019' layer, i.e. parcels removed from that year's roll, but no "
                "corresponding Secured_Tax_Roll_2018_to_2019 layer with values). Used 2019-20 through 2025-26 (7 years) instead."
            ),
            "tax_assumptions": {
                "current_general_rate_pct": GENERAL_RATE_CURRENT,
                "sf_bond_rate_pct": BOND_RATE_SF,
                "proposed_general_rate_pct": GENERAL_RATE_PROPOSED,
            },
            "generated": datetime.date.today().isoformat(),
        },
        "counts": {
            "total_parcels": len(parcels),
            "jump_confirmed_comps": len(all_comps),
            "cities": len(by_city),
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
    with open(OUT_SUMMARY, "w") as f:
        json.dump(summary, f, indent=2)

    print("WROTE", rows_written, "rows ->", OUT_CSV, file=sys.stderr)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
