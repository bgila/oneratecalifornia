"""
Build market-value estimates for every SF single-family/condo parcel.

There is no public bulk sale-price dataset for SF real estate, so market
value has to be inferred from the assessor roll itself. The approach:

1. "Jump-confirmed comps": Prop 13 caps the ordinary annual inflation
   adjustment on assessed value at ~2%/yr, so any parcel whose total
   assessed value jumps >=8% year-over-year, AND has a recorded sale date
   within a year of that jump (allowing for the ~1yr lag between a sale
   and its reassessment appearing on the roll), is treated as a confirmed
   market reset -- its post-jump assessed value is used as a real
   price-per-sqft signal. This filters out non-arms-length transfers
   (e.g. Prop 19 parent-child/trust transfers, batch administrative
   recordings) that have a sale date but no real value reset, which a
   naive "has a recent sale date" filter would wrongly include.
2. Comps are appreciation-adjusted to today's-equivalent price before use:
   a comp from 2012 is scaled up by the FRED SF house price index's growth
   from 2012 to 2025 (data/sf-hpi.json), so an older comp doesn't drag the
   estimate down just because it's stale.
3. For each target home, the 7 nearest jump-confirmed comps by location set
   the estimate: median $/sqft of those comps x the subject's sqft. "By
   location" means same neighborhood if it has 12+ comps of its own;
   otherwise, comps are pooled from every neighborhood in the same price
   quartile (ranked by each neighborhood's own comps' median $/sqft) rather
   than the whole city -- a citywide fallback was pulling low-turnover,
   high-value neighborhoods (Sea Cliff, Presidio Heights, etc.) toward the
   city's much lower median. Neighborhoods with zero comps of their own
   still fall back to the full city, since there's no way to rank them.
4. Estimated market value is floored at the current assessed value: in
   today's market a home's true value essentially never sits below what
   Prop 13 has grown its assessment to, so an estimate below assessed value
   is almost always a sign the comp model undershot rather than a real
   declining-value home. This trades away accuracy for the rare legitimate
   exception (e.g. some SoMa condos that have genuinely lost value since
   purchase) in exchange for not showing an obviously-wrong "underwater"
   result for the vastly more common case.

Known weak point: still undershoots at the very top of the market (large/
luxury homes), since nearest-neighbor $/sqft blends in typical nearby homes
rather than modeling a price tier.

Reads:  pipeline/tmp/sfr_snapshot_raw.json       (01_fetch_sfr_snapshot.py)
        pipeline/tmp/sfr_history_2010_2025.json  (02_fetch_sfr_history.py)
        data/sf-hpi.json                         (13_fetch_hpi.py)
Writes: pipeline/tmp/sf-citywide-sfr-full.csv     (full per-parcel estimate table)
        pipeline/tmp/sf-citywide-summary.json     (methodology + summary stats)
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
RAW_SNAPSHOT = TMP_DIR / "sfr_snapshot_raw.json"
HISTORY = TMP_DIR / "sfr_history_2010_2025.json"
HPI_PATH = PIPELINE_DIR.parent / "data" / "sf-hpi.json"
OUT_CSV = TMP_DIR / "sf-citywide-sfr-full.csv"
OUT_SUMMARY = TMP_DIR / "sf-citywide-summary.json"

JUMP_THRESHOLD = 1.08
HISTORY_START_YEAR = 2010
CURRENT_YEAR = 2025
K = 7
MIN_COMPS_FOR_LOCAL_GROUP = 12
NUM_PRICE_TIERS = 4  # quartiles, for the fallback pool when a neighborhood lacks its own comps
GENERAL_RATE_CURRENT = 1.00
BOND_RATE_SF = 0.18
GENERAL_RATE_PROPOSED = 0.65


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_parcels(raw_snapshot_path):
    """Parse the current-roll snapshot into a dict of parcel_number -> parcel info."""
    raw = json.load(open(raw_snapshot_path))
    parcels = {}
    for r in raw:
        geom = r.get("the_geom")
        if not geom or geom.get("type") != "Point":
            continue
        lon, lat = geom["coordinates"]
        area = to_float(r.get("property_area"))
        if area > 20000:
            continue
        land = to_float(r.get("assessed_land_value"))
        impr = to_float(r.get("assessed_improvement_value"))
        fix = to_float(r.get("assessed_fixtures_value"))
        assessed_total = land + impr + fix
        if assessed_total <= 0:
            continue
        sale_date = r.get("current_sales_date")
        pn = r.get("parcel_number")
        parcels[pn] = {
            "parcel_number": pn,
            "address": " ".join((r.get("property_location") or "").split()),
            "neighborhood": r.get("assessor_neighborhood") or "Unknown",
            "lat": lat, "lon": lon,
            "beds": to_float(r.get("number_of_bedrooms")),
            "baths": to_float(r.get("number_of_bathrooms")),
            "sqft": area,
            "lot_sqft": to_float(r.get("lot_area")),
            "year_built": r.get("year_property_built"),
            "sale_date": sale_date[:10] if sale_date else None,
            "assessed_total": assessed_total,
        }
    return parcels


def load_confirmed_comps(history_path):
    """Detect jump-confirmed reassessment events per parcel across the full
    history window (HISTORY_START_YEAR-CURRENT_YEAR).

    Keeps the most recent confirmed jump per parcel as its comp price signal.
    """
    history = json.load(open(history_path))
    by_parcel_year = defaultdict(dict)
    for r in history:
        y = int(r["closed_roll_year"])
        total = (to_float(r.get("assessed_land_value"))
                 + to_float(r.get("assessed_improvement_value"))
                 + to_float(r.get("assessed_fixtures_value")))
        by_parcel_year[r["parcel_number"]][y] = {
            "total": total,
            "sale_date": r.get("current_sales_date"),
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
            sale_date = years[y].get("sale_date")
            sale_near = bool(sale_date) and int(sale_date[:4]) in (y - 1, y)
            if sale_near and ratio >= JUMP_THRESHOLD:
                if best is None or y > best[0]:
                    best = (y, cur)
        if best:
            confirmed[pn] = {"year": best[0], "total": best[1]}
    return confirmed


def nearest_k_psf(target_lat, target_lon, target_parcel, comp_lat, comp_lon, comp_psf, comp_parcel, k):
    lat0 = math.radians(37.77)
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
    return float(np.median(comp_psf[valid])), len(valid)


def main():
    print("loading 2025 snapshot...", file=sys.stderr)
    parcels = load_parcels(RAW_SNAPSHOT)
    print("usable parcels (2025 snapshot):", len(parcels), file=sys.stderr)

    print("loading multi-year history...", file=sys.stderr)
    confirmed = load_confirmed_comps(HISTORY)
    print("jump-confirmed comps found:", len(confirmed), file=sys.stderr)

    hpi = json.load(open(HPI_PATH))
    hpi_current = hpi[str(CURRENT_YEAR)]

    # attach comps (needs matching sqft from 2025 snapshot; assume sqft stable over time),
    # appreciation-adjusting each comp's psf to today's-equivalent via the FRED index so an
    # older comp (this window now reaches back to 2010) doesn't read as artificially cheap.
    all_comps = []
    for pn, info in confirmed.items():
        p = parcels.get(pn)
        if not p or p["sqft"] <= 200:
            continue
        raw_psf = info["total"] / p["sqft"]
        if raw_psf <= 0 or raw_psf > 10000:  # sanity guard
            continue
        comp_year = info["year"]
        hpi_at_comp = hpi.get(str(comp_year), hpi_current)
        adjusted_psf = raw_psf * (hpi_current / hpi_at_comp)
        all_comps.append({
            "parcel_number": pn,
            "neighborhood": p["neighborhood"],
            "lat": p["lat"], "lon": p["lon"],
            "price_per_sqft": adjusted_psf,
            "comp_year": comp_year,
        })
    print("usable comps after join:", len(all_comps), file=sys.stderr)

    by_nb = defaultdict(list)
    for p in parcels.values():
        by_nb[p["neighborhood"]].append(p)

    comps_by_nb = defaultdict(list)
    for c in all_comps:
        comps_by_nb[c["neighborhood"]].append(c)

    # Rank neighborhoods into price quartiles using whatever comps they have (even if too
    # few to estimate from directly) so the fallback for a comp-sparse neighborhood can stay
    # within its own price tier instead of diluting into the whole city's median. A citywide
    # fallback was the main reason low-turnover, high-value neighborhoods like Sea Cliff were
    # coming out badly undervalued.
    nb_tier_psf = {
        nb: statistics.median(c["price_per_sqft"] for c in comps)
        for nb, comps in comps_by_nb.items() if comps
    }
    ranked_nbs = sorted(nb_tier_psf, key=lambda nb: nb_tier_psf[nb])
    tier_of_nb = {}
    if ranked_nbs:
        tier_size = math.ceil(len(ranked_nbs) / NUM_PRICE_TIERS)
        for i, nb in enumerate(ranked_nbs):
            tier_of_nb[nb] = min(i // tier_size, NUM_PRICE_TIERS - 1)
    comps_by_tier = defaultdict(list)
    for nb, comps in comps_by_nb.items():
        tier = tier_of_nb.get(nb)
        if tier is not None:
            comps_by_tier[tier].extend(comps)

    all_comp_lat = np.array([c["lat"] for c in all_comps])
    all_comp_lon = np.array([c["lon"] for c in all_comps])
    all_comp_psf = np.array([c["price_per_sqft"] for c in all_comps])
    all_comp_parcel = np.array([c["parcel_number"] for c in all_comps])

    rows_written = 0
    neighborhoods_done = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0

    fieldnames = [
        "parcel_number", "address", "neighborhood", "lat", "lon", "beds", "baths", "sqft", "lot_sqft",
        "year_built", "last_sale_date", "assessed_total", "est_market_value", "est_price_per_sqft",
        "comp_count", "comp_source", "current_tax_est", "subsidy_vs_market_today",
        "tax_under_reform_est", "change_under_reform",
    ]

    with open(OUT_CSV, "w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fieldnames)
        writer.writeheader()

        for nb_name, members in by_nb.items():
            local_comps = comps_by_nb.get(nb_name, [])
            tier_comps = comps_by_tier.get(tier_of_nb.get(nb_name))
            if len(local_comps) >= MIN_COMPS_FOR_LOCAL_GROUP:
                pool, source_label = local_comps, "same_neighborhood"
            elif tier_comps and len(tier_comps) >= MIN_COMPS_FOR_LOCAL_GROUP:
                pool, source_label = tier_comps, "same_price_tier"
            else:
                pool, source_label = None, "citywide_fallback"

            if pool is not None:
                comp_lat = np.array([c["lat"] for c in pool])
                comp_lon = np.array([c["lon"] for c in pool])
                comp_psf = np.array([c["price_per_sqft"] for c in pool])
                comp_parcel = np.array([c["parcel_number"] for c in pool])
            else:
                comp_lat, comp_lon, comp_psf, comp_parcel = all_comp_lat, all_comp_lon, all_comp_psf, all_comp_parcel

            batch_rows = []
            for p in members:
                if p["sqft"] <= 200:
                    continue
                result = nearest_k_psf(p["lat"], p["lon"], p["parcel_number"], comp_lat, comp_lon, comp_psf, comp_parcel, K)
                if result is None:
                    continue
                med_psf, n_used = result
                est_market_value = med_psf * p["sqft"]
                # Floor market value at the current assessed value: in today's market,
                # a home's true value essentially never sits below what Prop 13 has grown
                # its assessment to, so a below-assessed estimate here is almost always a
                # sign the comp-based model has undershot, not a real declining-value case.
                # This trades away accuracy for the rare legitimate case (e.g. some SoMa
                # condos that have genuinely lost value) in exchange for not showing an
                # obviously-wrong negative subsidy for the vastly more common case.
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
                    "parcel_number": p["parcel_number"], "address": p["address"], "neighborhood": nb_name,
                    "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                    "beds": p["beds"], "baths": p["baths"], "sqft": p["sqft"], "lot_sqft": p["lot_sqft"],
                    "year_built": p["year_built"], "last_sale_date": p["sale_date"] or "",
                    "assessed_total": round(p["assessed_total"]), "est_market_value": round(est_market_value),
                    "est_price_per_sqft": round(med_psf, 2), "comp_count": n_used, "comp_source": source_label,
                    "current_tax_est": round(current_tax), "subsidy_vs_market_today": round(subsidy),
                    "tax_under_reform_est": round(reform_tax), "change_under_reform": round(change),
                })
            writer.writerows(batch_rows)
            fcsv.flush()
            rows_written += len(batch_rows)
            neighborhoods_done += 1
            print(f"[{neighborhoods_done}/{len(by_nb)}] {nb_name}: {len(members)} parcels, "
                  f"{len(local_comps)} local jump-confirmed comps ({source_label}), wrote {len(batch_rows)} rows "
                  f"(running total {rows_written})", file=sys.stderr)

    summary = {
        "methodology": {
            "source_assessed_values": "DataSF: Assessor Historical Secured Property Tax Rolls (wv5m-vpq2), closed_roll_year=2025",
            "source_url": "https://data.sfgov.org/Housing-and-Buildings/Assessor-Historical-Secured-Property-Tax-Rolls/wv5m-vpq2",
            "scope": "Citywide, Single Family Residential (includes individually-deeded condos)",
            "market_value_estimation": (
                f"Comps are identified by detecting actual reassessment events in {HISTORY_START_YEAR}-{CURRENT_YEAR} "
                "multi-year assessor history, not just a recorded sale date: Prop 13 caps the ordinary annual "
                "inflation adjustment at ~2%/yr, so any parcel whose total assessed value jumps >=8% year-over-year, "
                "with a recorded sale date within a year of that jump (allowing for the ~1yr lag between a sale and "
                "its reassessment appearing on the roll), is treated as a confirmed market reset -- its post-jump "
                "assessed value is used as a real price-per-sqft signal, appreciation-adjusted to today's-equivalent "
                "via the FRED SF house price index so an older comp doesn't read as artificially cheap. This catches "
                "non-arms-length transfers (e.g. Prop 19 parent-child/trust transfers, or batch administrative "
                "recordings) that have a sale date but no real value reset, which a naive 'has a recent sale date' "
                "filter would wrongly include. "
                f"For each target home, the {K} nearest confirmed comps by location set the estimate: median $/sqft "
                f"x subject sqft. 'By location' means same neighborhood if it has {MIN_COMPS_FOR_LOCAL_GROUP}+ comps "
                "of its own; otherwise, comps are pooled from every neighborhood in the same price quartile (ranked "
                "by each neighborhood's own comps) rather than the whole city, so a comp-sparse but expensive "
                "neighborhood isn't dragged toward the citywide median. Neighborhoods with zero comps of their own "
                "still fall back to citywide, since there's no way to rank them. Estimated market value is then "
                "floored at the home's current assessed value: in today's market a home's true value essentially "
                "never sits below what Prop 13 has grown its assessment to, so an estimate below assessed value is "
                "almost always a sign the comp model undershot rather than a real declining-value home. This trades "
                "away accuracy for the rare legitimate exception (e.g. some SoMa condos that have genuinely lost "
                "value since purchase) in exchange for not showing an obviously-wrong 'underwater' result for the "
                "vastly more common case. "
                "Known weak point: still undershoots at the very top of the market (large/luxury homes), since "
                "nearest-neighbor $/sqft blends in typical nearby homes rather than modeling a price tier."
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
            "neighborhoods": len(by_nb),
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

    print("WROTE", rows_written, "rows ->", OUT_CSV, file=sys.stderr)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
