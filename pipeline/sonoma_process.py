"""
Build market-value estimates for every Sonoma County single-family-dwelling
parcel.

Unlike San Francisco (no public bulk sale-price dataset -> 03_process_sfr.py
has to infer market value from assessed-value "jump-confirmed" reassessment
events), Sonoma County's public ArcGIS parcel layer carries real recorded
transaction prices directly (SaleRecordingDate/SaleSalesPrice), so this
script skips the jump-detection step entirely and builds comps straight from
actual sales:

1. Sold comps: every parcel with a recorded SaleSalesPrice > 0 and a
   SaleRecordingDate within the last SALE_WINDOW_YEARS years (a recent-enough
   sale that it's a reasonable proxy for today's market, without discarding
   so much history that rural/low-turnover areas end up with too few comps)
   contributes a real $/sqft data point: SaleSalesPrice / BuildingPrimarySize.
   A loose sanity guard (0 < $/sqft <= $10,000) drops data-entry outliers.
2. Comps are appreciation-adjusted to today's-equivalent price before use,
   the same way 03_process_sfr.py does with data/sf-hpi.json: a comp from
   2016 is scaled up by the FRED Sonoma County house price index's growth
   from 2016 to the latest available index year (data/sonoma-hpi.json),
   so an older comp in the window doesn't drag the estimate down just
   because it's stale.
3. For every parcel (not just the ones with a recent sale of their own),
   the market-value estimate is the median $/sqft of the K=7 nearest sold
   comps by straight-line lat/lon distance, times the subject's own sqft.
   Unlike 03_process_sfr.py, there's no neighborhood/price-tier fallback
   hierarchy here: Sonoma has no neighborhood field in this dataset, and
   with ~30k real sold comps spread across the county a plain nearest-K
   search has enough density everywhere to not need one (see counts in the
   methodology JSON this script emits).
4. Estimated market value is floored at the parcel's current total assessed
   value (Value601Land + Value601Structure + Value601Fixtures -- the
   pre-exemption gross value, to match SF's assessed_total convention of
   summing raw land+improvement+fixtures with no homeowner exemption
   subtracted), for the same reason 03_process_sfr.py does: a below-assessed
   estimate is virtually always the comp model undershooting, not a real
   declining-value home.

Known weak point: same as SF, likely undershoots at the very top of the
market, since nearest-neighbor $/sqft blends in typical nearby homes rather
than modeling a price tier. Also: because sold comps and target parcels are
the same pool (every parcel, sold or not), a parcel that itself sold
recently is excluded as its own comp (by APN) but still uses its neighbors'
comps to set its estimate, rather than simply reusing its own sale price --
this keeps the estimation logic uniform across every parcel instead of
special-casing recently-sold ones.

Reads:  pipeline/tmp/sonoma_parcels_raw.json  (sonoma_fetch_parcels.py)
        data/sonoma-hpi.json                  (sonoma_fetch_hpi.py)
Writes: pipeline/tmp/sonoma-full.csv          (full per-parcel estimate table)
        data/sonoma-methodology.json          (methodology + summary stats)
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
RAW_PARCELS = TMP_DIR / "sonoma_parcels_raw.json"
HPI_PATH = DATA_DIR / "sonoma-hpi.json"
OUT_CSV = TMP_DIR / "sonoma-full.csv"
OUT_METHODOLOGY = DATA_DIR / "sonoma-methodology.json"

MIN_SQFT = 200
MAX_SQFT = 20000
MAX_PSF = 10000  # sanity guard, mirrors 03_process_sfr.py's raw_psf cap
SALE_WINDOW_YEARS = 10
K = 7
GENERAL_RATE_CURRENT = 1.00
BOND_RATE_SF = 0.18  # SF's bond rate, held fixed across counties for comparability (see task brief)
GENERAL_RATE_PROPOSED = 0.70
REFERENCE_LAT_RAD = math.radians(38.5)  # rough county center, for lon/lat distance scaling only


def to_float(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def epoch_ms_to_date(ms):
    if not ms:
        return None
    try:
        return datetime.date.fromtimestamp(ms / 1000).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def clean_addr(street, city_state):
    """SitusFormatted1/2 e.g. '137 NORTH ST' / 'CLOVERDALE CA' -> '137 North St, Cloverdale, CA'."""
    street = (street or "").strip()
    city_state = (city_state or "").strip()
    city = city_state
    if city_state.upper().endswith(" CA"):
        city = city_state[:-3].strip()
    parts = [p for p in (street.title(), city.title()) if p]
    addr = ", ".join(parts)
    if addr:
        addr += ", CA"
    return addr


def load_parcels(raw_path):
    raw = json.load(open(raw_path))
    parcels = {}
    for r in raw:
        lat, lon = r.get("Lat"), r.get("Long")
        if lat is None or lon is None:
            continue
        sqft = to_float(r.get("BuildingPrimarySize"))
        if sqft < MIN_SQFT or sqft > MAX_SQFT:
            continue
        land = to_float(r.get("Value601Land"))
        structure = to_float(r.get("Value601Structure"))
        fixtures = to_float(r.get("Value601Fixtures"))
        assessed_total = land + structure + fixtures
        if assessed_total <= 0:
            continue
        apn = r.get("APN")
        sale_price = to_float(r.get("SaleSalesPrice"), default=None) if r.get("SaleSalesPrice") is not None else None
        sale_date = epoch_ms_to_date(r.get("SaleRecordingDate"))
        parcels[apn] = {
            "apn": apn,
            "address": clean_addr(r.get("SitusFormatted1"), r.get("SitusFormatted2")),
            "lat": float(lat), "lon": float(lon),
            "beds": to_float(r.get("BuildingPrimaryBedRooms")),
            "baths": to_float(r.get("BuildingPrimaryBaths")),
            "sqft": sqft,
            "land_sqft": to_float(r.get("LandSizeSqft")),
            "year_built": r.get("BuildingPrimaryYearBuilt"),
            "sale_date": sale_date,
            "sale_price": sale_price,
            "assessed_total": assessed_total,
        }
    return parcels


def build_sold_comps(parcels, hpi, current_year, window_start_year):
    hpi_anchor_year = max(int(y) for y in hpi)
    hpi_anchor = hpi[str(hpi_anchor_year)]
    comps = []
    for p in parcels.values():
        if not p["sale_date"] or not p["sale_price"] or p["sale_price"] <= 0:
            continue
        sale_year = int(p["sale_date"][:4])
        if sale_year < window_start_year or sale_year > current_year:
            continue
        raw_psf = p["sale_price"] / p["sqft"]
        if raw_psf <= 0 or raw_psf > MAX_PSF:
            continue
        hpi_at_sale = hpi.get(str(sale_year), hpi_anchor)
        adjusted_psf = raw_psf * (hpi_anchor / hpi_at_sale)
        comps.append({
            "apn": p["apn"], "lat": p["lat"], "lon": p["lon"],
            "price_per_sqft": adjusted_psf, "sale_year": sale_year,
        })
    return comps, hpi_anchor_year


def nearest_k_median_psf_batch(target_lat, target_lon, target_apn, comp_lat, comp_lon, comp_psf, comp_apn, k, batch_size=1000):
    """Vectorized nearest-K by lat/lon (equirectangular approx, fine for ranking) in batches
    so the full (n_targets x n_comps) distance matrix never has to materialize at once."""
    n = len(target_lat)
    med_psf = np.full(n, np.nan)
    n_used = np.zeros(n, dtype=int)
    cos_lat = math.cos(REFERENCE_LAT_RAD)

    for start in range(0, n, batch_size):
        end = min(start + batch_size, n)
        t_lat = target_lat[start:end][:, None]
        t_lon = target_lon[start:end][:, None]
        t_apn = target_apn[start:end][:, None]

        dx = (comp_lon[None, :] - t_lon) * cos_lat
        dy = comp_lat[None, :] - t_lat
        d2 = dx * dx + dy * dy
        self_mask = comp_apn[None, :] == t_apn
        d2 = np.where(self_mask, np.inf, d2)

        ncomps = d2.shape[1]
        kk = min(k, ncomps)
        idx = np.argpartition(d2, kk - 1, axis=1)[:, :kk]
        chosen_d2 = np.take_along_axis(d2, idx, axis=1)
        chosen_psf = comp_psf[idx]
        valid = np.isfinite(chosen_d2)
        for i in range(end - start):
            v = valid[i]
            cnt = int(v.sum())
            if cnt == 0:
                continue
            med_psf[start + i] = np.median(chosen_psf[i][v])
            n_used[start + i] = cnt

    return med_psf, n_used


def main():
    print("loading Sonoma parcel snapshot...", file=sys.stderr)
    parcels = load_parcels(RAW_PARCELS)
    print("usable parcels:", len(parcels), file=sys.stderr)

    hpi = json.load(open(HPI_PATH))
    current_year = datetime.date.today().year
    window_start_year = current_year - SALE_WINDOW_YEARS

    all_comps, hpi_anchor_year = build_sold_comps(parcels, hpi, current_year, window_start_year)
    print(f"sold comps in last {SALE_WINDOW_YEARS} years ({window_start_year}-{current_year}):", len(all_comps), file=sys.stderr)

    target_list = list(parcels.values())
    target_lat = np.array([p["lat"] for p in target_list])
    target_lon = np.array([p["lon"] for p in target_list])
    target_apn = np.array([p["apn"] for p in target_list])

    comp_lat = np.array([c["lat"] for c in all_comps])
    comp_lon = np.array([c["lon"] for c in all_comps])
    comp_psf = np.array([c["price_per_sqft"] for c in all_comps])
    comp_apn = np.array([c["apn"] for c in all_comps])

    print("computing nearest-K comps for all targets...", file=sys.stderr)
    med_psf, n_used = nearest_k_median_psf_batch(
        target_lat, target_lon, target_apn, comp_lat, comp_lon, comp_psf, comp_apn, K
    )

    fieldnames = [
        "apn", "address", "lat", "lon", "beds", "baths", "sqft", "land_sqft",
        "year_built", "last_sale_date", "last_sale_price", "assessed_total",
        "est_market_value", "est_price_per_sqft", "comp_count",
        "current_tax_est", "subsidy_vs_market_today",
        "tax_under_reform_est", "change_under_reform",
    ]

    rows_written = 0
    rows_skipped_no_comps = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0

    with open(OUT_CSV, "w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fieldnames)
        writer.writeheader()
        batch_rows = []
        for i, p in enumerate(target_list):
            if not np.isfinite(med_psf[i]):
                rows_skipped_no_comps += 1
                continue
            psf = float(med_psf[i])
            est_market_value = psf * p["sqft"]
            # Floor at assessed value: see module docstring point 4.
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
                "apn": p["apn"], "address": p["address"],
                "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                "beds": p["beds"], "baths": p["baths"], "sqft": p["sqft"], "land_sqft": p["land_sqft"],
                "year_built": p["year_built"], "last_sale_date": p["sale_date"] or "",
                "last_sale_price": round(p["sale_price"]) if p["sale_price"] else "",
                "assessed_total": round(p["assessed_total"]), "est_market_value": round(est_market_value),
                "est_price_per_sqft": round(psf, 2), "comp_count": int(n_used[i]),
                "current_tax_est": round(current_tax), "subsidy_vs_market_today": round(subsidy),
                "tax_under_reform_est": round(reform_tax), "change_under_reform": round(change),
            })
            if len(batch_rows) >= 5000:
                writer.writerows(batch_rows)
                rows_written += len(batch_rows)
                print(f"  ...{rows_written} rows written", file=sys.stderr)
                batch_rows = []
        writer.writerows(batch_rows)
        rows_written += len(batch_rows)

    print("rows written:", rows_written, "skipped (no comps found):", rows_skipped_no_comps, file=sys.stderr)

    summary = {
        "methodology": {
            "source_parcel_data": "Sonoma County CRA Public Parcels ArcGIS FeatureServer (ParcelsPublic/FeatureServer/0)",
            "source_url": "https://socogis.sonomacounty.ca.gov/map/rest/services/CRAPublic/ParcelsPublic/FeatureServer/0",
            "scope": "Countywide, UseCode='0010' (SINGLE FAMILY DWELLING) -- Sonoma's single dominant SFR use code",
            "market_value_estimation": (
                "Unlike San Francisco, which has no public bulk sale-price dataset and must infer market value from "
                "assessed-value 'jump-confirmed' reassessment events, this parcel layer carries real recorded "
                "transaction prices directly (SaleRecordingDate/SaleSalesPrice), so comps are built straight from "
                "actual sales rather than an assessed-value proxy. Every parcel with a recorded sale price in the "
                f"last {SALE_WINDOW_YEARS} years ({window_start_year}-{current_year}) contributes a real $/sqft data "
                "point (SaleSalesPrice / BuildingPrimarySize), sanity-guarded to 0 < $/sqft <= $10,000 to drop "
                "data-entry outliers. Each comp is appreciation-adjusted to today's-equivalent price via the FRED "
                f"Sonoma County house price index (ATNHPIUS06097A, data/sonoma-hpi.json), anchored to its latest "
                f"available year ({hpi_anchor_year}), so an older comp within the window doesn't read as artificially "
                f"cheap. For every parcel (not just recently-sold ones), the {K} nearest sold comps by straight-line "
                "lat/lon distance set the estimate: median $/sqft x subject sqft. Unlike 03_process_sfr.py, there is "
                "no neighborhood/price-tier fallback hierarchy here -- this dataset has no neighborhood field, and "
                "with tens of thousands of real sold comps spread across the county, a plain nearest-K search has "
                "enough density everywhere that the fallback SF needed for its comp-sparse high-value neighborhoods "
                "wasn't necessary here (see counts below). Estimated market value is then floored at the parcel's "
                "current total assessed value (Value601Land + Value601Structure + Value601Fixtures, the pre-exemption "
                "gross value -- matching SF's convention of summing raw land+improvement+fixtures with no homeowner "
                "exemption subtracted): in today's market a home's true value essentially never sits below what "
                "Prop 13 has grown its assessment to, so an estimate below assessed value is almost always a sign "
                "the comp model undershot rather than a real declining-value home. "
                "Known weak point: likely undershoots at the very top of the market (large/luxury homes and "
                "vineyard estates), since nearest-neighbor $/sqft blends in typical nearby homes rather than "
                "modeling a price tier."
            ),
            "sale_recency_window_years": SALE_WINDOW_YEARS,
            "nearest_comps_k": K,
            "assessed_value_definition": "Value601Land + Value601Structure + Value601Fixtures (pre-exemption gross value; Value601NetValue, which subtracts homeowner/other exemptions, was NOT used, for consistency with SF's un-exempted assessed_total)",
            "hpi_source": "FRED ATNHPIUS06097A: All-Transactions House Price Index for Sonoma County, CA",
            "hpi_anchor_year": hpi_anchor_year,
            "tax_assumptions": {
                "current_general_rate_pct": GENERAL_RATE_CURRENT,
                "bond_rate_pct": BOND_RATE_SF,
                "proposed_general_rate_pct": GENERAL_RATE_PROPOSED,
                "note": "bond_rate_pct is SF's bond rate, deliberately held fixed across counties for cross-county comparability, per project convention.",
            },
            "deviations_from_sf_methodology": [
                "No jump-detection / multi-year assessed-value history needed: real recorded sale prices "
                "(SaleSalesPrice) are used directly as the comp signal, which is a strictly better market-value "
                "proxy than SF's assessed-value-jump heuristic.",
                "No neighborhood/price-tier comp-pooling fallback: Sonoma's parcel layer has no neighborhood field, "
                "and comp density from real sales is high enough countywide that a single global nearest-K pool "
                "was sufficient (unlike SF, where a citywide fallback badly undervalued low-turnover neighborhoods).",
                "Sale recency window (10 years) is a new parameter with no SF equivalent, since SF's comps come from "
                "reassessment events across its full 2010-2025 history window rather than a sale-price recency cutoff.",
            ],
            "generated": datetime.date.today().isoformat(),
        },
        "counts": {
            "total_parcels_considered": len(parcels),
            "sold_comps_in_window": len(all_comps),
            "estimated_rows_written": rows_written,
            "rows_skipped_no_comps_found": rows_skipped_no_comps,
        },
        "stats": {
            "subsidy_vs_market_today": {
                "p10": round(np.percentile(subsidy_all, 10)), "median": round(statistics.median(subsidy_all)),
                "mean": round(statistics.mean(subsidy_all)), "p90": round(np.percentile(subsidy_all, 90)),
                "p99": round(np.percentile(subsidy_all, 99)),
                "min": round(min(subsidy_all)), "max": round(max(subsidy_all)),
            } if subsidy_all else {},
            "under_reform": {
                "would_pay_more": increases, "would_pay_less": decreases,
                "pct_pay_more": round(100 * increases / rows_written, 1) if rows_written else 0,
                "pct_pay_less": round(100 * decreases / rows_written, 1) if rows_written else 0,
            },
        },
    }
    with open(OUT_METHODOLOGY, "w") as f:
        json.dump(summary, f, indent=2)

    print("WROTE", rows_written, "rows ->", OUT_CSV, file=sys.stderr)
    print("WROTE methodology ->", OUT_METHODOLOGY, file=sys.stderr)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
