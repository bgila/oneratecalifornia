"""
Build market-value estimates for every Inyo County single-family parcel.

WHY THIS PIPELINE LOOKS DIFFERENT FROM SF'S (03_process_sfr.py)
-----------------------------------------------------------------
SF's estimator works from *internal* signal: it detects a parcel's own
assessed-value jump that coincides with a recorded sale (a "jump-confirmed
comp"), then borrows nearby comps' $/sqft. Inyo's county ArcGIS parcel API
(gisdata.inyo.gov -> services.arcgis.com/.../ParcelsPublic/FeatureServer/0)
has NEITHER ingredient SF's model needs:
  - No multi-year history endpoint -- only a single current-year snapshot
    (AssessYear is just the current roll year for every row).
  - No sale-date / document-date field at all.
So there is no way to detect a reassessment event here, and therefore no
way to build a single "jump-confirmed comp" anywhere in this county. This
pipeline instead anchors market value on an EXTERNAL benchmark: Zillow
Research's ZHVI (Zillow Home Value Index), a free, publicly downloadable,
ZIP-level time series for single-family homes (see inyo_fetch.py for how
that CSV's live download URL was verified).

THE SQFT PROBLEM (read this before trusting the "sqft" column)
-----------------------------------------------------------------
The originally assigned method for counties in this situation was:
    zip_dollar_per_sqft = ZHVI[zip] / median_building_sqft[zip]
    parcel_market_value = zip_dollar_per_sqft * parcel_building_sqft
That requires a BUILDING/LIVING-AREA square-footage field. Inyo's ArcGIS
layer does not have one anywhere in its schema -- verified against the
live service's full field list (see inyo_fetch.py's docstring for the
complete list). The only size-ish fields are LotSqFeet, LotAcres, Width,
Depth, and the GIS-computed Acres_gis/SqFeet_gis, and a live spot-check
confirms these are all PARCEL/LOT geometry, not the structure: e.g. APN
0010110600, a single-family home at 751 Home St, Bishop, carries
LotSqFeet=23,600 (roughly half an acre) -- clearly a lot size, not a home's
floor area. Dividing ZHVI by a LOT-size median would produce a nonsense
"$/sqft of dirt" figure, then multiplying that by the subject's own lot
size would double down on the error in a way that has nothing to do with
the size of the actual house.

DEVIATION FROM THE ASSIGNED METHODOLOGY (this is the important part):
Per that method's own documented contingency plan for exactly this case,
this script falls back to a PER-PROPERTY (not per-sqft) estimate:
    parcel_market_value = ZIP's own ZHVI value (single-family, latest month)
applied directly to every single-family parcel in that ZIP, with no sqft
adjustment at all (every home in a ZIP gets the same starting estimate --
the ZIP's typical/median single-family value -- before the assessed-value
floor is applied). This is a real accuracy cost: it can't tell a large
home from a small one within the same ZIP the way a $/sqft model would.
It is the most defensible option available given the data that actually
exists, and is far better than fabricating a building-sqft figure or
skipping the county's ~3,500 single-family parcels entirely.

The output "sqft" column (present because the committed CSV schema is
shared across counties) is filled with LotSqFeet -- LOT size, not building
size -- and this is called out explicitly here and in inyo-methodology.json
so nobody downstream mistakes it for the same quantity SF's "sqft" column
represents.

SCOPE: only PropClass == 'SFR (SINGLE FAMILY RESIDENCE)' rows are priced.
Excluded: manufactured/mobile homes (PropClass MH ON FOUNDATION / MH IN
PARK / MH ON FEE LAND / etc, ~2,000 parcels countywide -- a large share of
Inyo's actual housing stock, but a distinct assessor property class not
clearly represented by ZHVI's single-family index, and Inyo's parcel layer
gives no way to size or comp them separately); "SFR AND SFR" / "SFR AND
DUPLEX" (multiple structures on one parcel -- one TotalVal can't be
cleanly split between them); condos and 2-4 unit residences (different
ZHVI series, out of scope for this pass).

ZIP-LEVEL EXTERNAL-INDEX COVERAGE AND FALLBACK
-----------------------------------------------------------------
Of the ~16 ZIP codes/labels present on Inyo's own single-family parcels,
only 10 have direct ZHVI coverage (checked against Zillow's ZIP list
regardless of which county *Zillow* attributes the ZIP to, since a few of
these are unincorporated desert communities Zillow/USPS files under a
neighboring county or even Nevada): 93514 (Bishop), 93545 (Lone Pine),
93513 (Big Pine), 93526 (Independence), 93527 (Inyokern), 89060 & 89061
(Pahrump, NV -- the real postal ZIPs for Stewart Valley / Charleston View,
CA), 93562 (Trona, mostly San Bernardino County), plus two single-parcel
ZIP VALUES (95313 Crows Landing / 93546 Mammoth Lakes) that are corrected
below, not used as-is (see DATA CLEANING).

Six ZIPs on Inyo's own roll (93549 Cartago, 92389 Tecopa/Tecopa Hot
Springs, 93522 Darwin, 93530 Keeler, 93592 Homewood Canyon, 92384 Shoshone)
plus a handful of parcels with no ZIP recorded at all have NO ZHVI
coverage -- unsurprising for single-digit-population unincorporated desert
places. For these, market value falls back to the ZHVI of the geographically
NEAREST covered ZIP (by straight-line distance from the parcel's own
centroid to each covered ZIP's own parcel-centroid average), the same
nearest-by-location spirit as SF's neighborhood-tier fallback. This affects
a meaningful minority of the county's single-family parcels -- the exact
count is printed and written into the methodology/summary output; see
inyo-methodology.json's "zhvi_fallback" section for the actual number
rather than trusting a hardcoded estimate here.

DATA CLEANING: two single-family parcels carry an internally inconsistent
ZIP (situs city says BIG PINE but ZIP is 95313 -- a Central Valley ZIP
1200 numeric digits away only by transposing "93513"'s two middle digits;
situs city says LONE PINE but ZIP is 93546, Mammoth Lakes' ZIP, ~60 miles
north). Both read as assessor data-entry typos rather than real addresses,
so both are corrected to the ZIP implied by their own city field (93513
and 93545 respectively) before ZHVI lookup, rather than either trusting
the literal (wrong) ZIP or leaving them uncovered.

FLOOR: market value is floored at assessed value (LandVal + ImproveVal +
FixtureVal), for the same reason as SF's pipeline -- a below-assessed
estimate is virtually always the model undershooting, not a real
declining-value home. Note this uses LandVal+ImproveVal+FixtureVal, NOT
the API's own TotalVal field: TotalVal = LandVal+ImproveVal+FixtureVal
MINUS ExemptVal (verified across every parcel with a nonzero ExemptVal --
it is almost always exactly $7,000, California's flat homeowner's
exemption). SF's "assessed_total" never subtracts any exemption, so using
TotalVal here would understate Inyo's assessed baseline relative to SF's
for no principled reason and inflate every owner-occupied Inyo parcel's
apparent subsidy by exactly the exemption amount.

HONEST CAVEAT (surface this anywhere these numbers are shown): this
entire estimate is anchored on ZHVI, which is itself Zillow's own
smoothed/seasonally-adjusted statistical model, not the county's own
confirmed sales or reassessment events. That makes it one layer more
removed from ground truth than SF's jump-confirmed-comps approach (which
is anchored on the county's own real reassessment data). Treat Inyo's
"market" figures as an estimate of an estimate.

Reads:  pipeline/tmp/inyo_parcels_raw.json  (inyo_fetch.py)
        pipeline/tmp/inyo_zhvi_raw.csv      (inyo_fetch.py)
Writes: pipeline/tmp/inyo-full.csv           (full per-parcel estimate table)
        pipeline/tmp/inyo-summary.json       (methodology + summary stats)
"""
import csv
import datetime
import json
import math
import statistics
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
PARCELS_RAW = TMP_DIR / "inyo_parcels_raw.json"
ZHVI_RAW = TMP_DIR / "inyo_zhvi_raw.csv"
OUT_CSV = TMP_DIR / "inyo-full.csv"
OUT_SUMMARY = TMP_DIR / "inyo-summary.json"

GENERAL_RATE_CURRENT = 1.00
BOND_RATE = 0.18
GENERAL_RATE_PROPOSED = 0.70

TARGET_PROPCLASS = "SFR (SINGLE FAMILY RESIDENCE)"

# Two single-family parcels carry an internally inconsistent ZIP (see
# module docstring, "DATA CLEANING"). Keyed by APN -> corrected ZIP.
ZIP_CORRECTIONS = {
    # 105 Pine Rd, Big Pine -- ZIP field says 95313 (Crows Landing, Stanislaus
    # County); city field says BIG PINE. 95313 is "93513" with its middle two
    # digits transposed -- treated as a typo, corrected to Big Pine's real ZIP.
    "0182600300": "93513",
    # 290 Tuttle Creek Rd, Lone Pine -- ZIP field says 93546 (Mammoth Lakes,
    # ~60mi north); city field says LONE PINE. Corrected to Lone Pine's ZIP.
    "0263100700": "93545",
}


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_parcels(path):
    raw = json.load(open(path))
    out = []
    for r in raw:
        a = r["attributes"]
        if (a.get("PropClass") or "").strip() != TARGET_PROPCLASS:
            continue
        c = r.get("centroid")
        if not c:
            continue
        apn = a.get("APN")
        zip_code = (a.get("ParcZIP") or "").strip()
        if apn in ZIP_CORRECTIONS:
            zip_code = ZIP_CORRECTIONS[apn]
        land = to_float(a.get("LandVal"))
        impr = to_float(a.get("ImproveVal"))
        fix = to_float(a.get("FixtureVal"))
        assessed = land + impr + fix
        if assessed <= 0:
            continue
        addr = " ".join((a.get("ParcAdd1") or "").split())
        city = " ".join((a.get("ParcCity") or "").split())
        out.append({
            "apn": apn,
            "addr": addr,
            "city": city,
            "zip": zip_code,
            "lat": c["y"], "lon": c["x"],
            "lot_sqft": to_float(a.get("LotSqFeet")),
            "assessed": assessed,
        })
    return out


def load_zhvi(path):
    """RegionName (ZIP, as zero-padded string) -> latest available value."""
    rows = list(csv.DictReader(open(path)))
    date_cols = [c for c in rows[0].keys()
                 if c[:4].isdigit() and len(c) == 10 and c[4] == "-"]
    date_cols.sort()
    latest_col = date_cols[-1]
    zhvi = {}
    for r in rows:
        zc = r.get("RegionName")
        if not zc:
            continue
        # walk backward from the newest month to the most recent one this
        # ZIP actually has a value for (small ZIPs sometimes lag by a month)
        val = None
        for col in reversed(date_cols):
            v = r.get(col)
            if v not in (None, "", "NaN"):
                val = float(v)
                used_col = col
                break
        if val is not None:
            zhvi[zc] = {"value": val, "as_of": used_col}
    return zhvi, latest_col


def nearest_donor_zip(lat, lon, donor_centroids):
    lat0 = math.radians(37.0)
    best_zip, best_d2 = None, None
    for z, (dlat, dlon) in donor_centroids.items():
        dx = (dlon - lon) * math.cos(lat0)
        dy = dlat - lat
        d2 = dx * dx + dy * dy
        if best_d2 is None or d2 < best_d2:
            best_d2, best_zip = d2, z
    return best_zip, math.sqrt(best_d2) if best_d2 is not None else None


def main():
    print("loading Inyo parcel snapshot...", file=sys.stderr)
    parcels = load_parcels(PARCELS_RAW)
    print(f"single-family parcels (PropClass == {TARGET_PROPCLASS!r}): {len(parcels)}", file=sys.stderr)

    print("loading ZHVI...", file=sys.stderr)
    zhvi, latest_col = load_zhvi(ZHVI_RAW)
    print(f"ZHVI loaded, {len(zhvi)} ZIPs nationally, newest column={latest_col}", file=sys.stderr)

    # ZIP centroid (mean of this county's own SFR parcels in that ZIP),
    # used both to identify which of Inyo's ZIPs have direct coverage and
    # as fallback donor points for uncovered ZIPs.
    zip_pts = {}
    for p in parcels:
        zip_pts.setdefault(p["zip"], []).append((p["lat"], p["lon"]))
    zip_centroid = {
        z: (sum(pt[0] for pt in pts) / len(pts), sum(pt[1] for pt in pts) / len(pts))
        for z, pts in zip_pts.items()
    }

    covered_zips = {z for z in zip_centroid if z in zhvi}
    uncovered_zips = {z for z in zip_centroid if z not in zhvi}
    donor_centroids = {z: zip_centroid[z] for z in covered_zips}
    print(f"Inyo ZIPs with direct ZHVI coverage: {sorted(covered_zips)}", file=sys.stderr)
    print(f"Inyo ZIPs WITHOUT ZHVI coverage (fallback needed): {sorted(uncovered_zips)}", file=sys.stderr)

    fallback_used = {}  # target_zip -> donor_zip
    rows_written = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0
    direct_count = fallback_count = 0

    fieldnames = [
        "apn", "addr", "city", "zip", "lat", "lon", "lot_sqft", "assessed",
        "zhvi_value", "zhvi_as_of", "zhvi_source_zip", "zhvi_coverage",
        "est_market_value", "current_tax_est", "subsidy_vs_market_today",
        "tax_under_reform_est", "change_under_reform",
    ]

    with open(OUT_CSV, "w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fieldnames)
        writer.writeheader()
        batch_rows = []
        for p in parcels:
            z = p["zip"]
            if z in zhvi:
                source_zip = z
                coverage = "direct"
                direct_count += 1
            else:
                if z not in fallback_used:
                    donor, dist = nearest_donor_zip(p["lat"], p["lon"], donor_centroids)
                    fallback_used[z] = donor
                    print(f"  fallback: ZIP {z!r} ({len(zip_pts[z])} parcels) -> nearest covered ZIP {donor!r} "
                          f"(~{dist * 69:.0f} mi)", file=sys.stderr)
                source_zip = fallback_used[z]
                coverage = "fallback_nearest_zip"
                fallback_count += 1
            zv = zhvi[source_zip]
            est_market = max(zv["value"], p["assessed"])
            current_tax = p["assessed"] * (GENERAL_RATE_CURRENT + BOND_RATE) / 100
            market_tax_current_law = est_market * (GENERAL_RATE_CURRENT + BOND_RATE) / 100
            reform_tax = est_market * (GENERAL_RATE_PROPOSED + BOND_RATE) / 100
            subsidy = market_tax_current_law - current_tax
            change = reform_tax - current_tax
            subsidy_all.append(subsidy)
            change_all.append(change)
            if change > 0:
                increases += 1
            elif change < 0:
                decreases += 1
            batch_rows.append({
                "apn": p["apn"], "addr": p["addr"], "city": p["city"], "zip": z,
                "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                "lot_sqft": p["lot_sqft"], "assessed": round(p["assessed"]),
                "zhvi_value": round(zv["value"]), "zhvi_as_of": zv["as_of"],
                "zhvi_source_zip": source_zip, "zhvi_coverage": coverage,
                "est_market_value": round(est_market),
                "current_tax_est": round(current_tax), "subsidy_vs_market_today": round(subsidy),
                "tax_under_reform_est": round(reform_tax), "change_under_reform": round(change),
            })
        writer.writerows(batch_rows)
        rows_written = len(batch_rows)

    print(f"wrote {rows_written} rows -> {OUT_CSV}", file=sys.stderr)
    print(f"direct ZHVI coverage: {direct_count}, fallback: {fallback_count} "
          f"({100*fallback_count/rows_written:.1f}%)", file=sys.stderr)

    summary = {
        "methodology": {
            "source_parcels": "Inyo County public ArcGIS FeatureServer (ParcelsPublic/0), current-year snapshot only",
            "source_parcels_url": "https://services.arcgis.com/0jRlQ17Qmni5zEMr/arcgis/rest/services/ParcelsPublic/FeatureServer/0",
            "source_market_index": "Zillow Research ZHVI, Single-Family Homes, Smoothed & Seasonally Adjusted, by ZIP Code",
            "source_market_index_url": "https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_sfr_tier_0.33_0.67_sm_sa_month.csv",
            "source_market_index_asof": latest_col,
            "scope": f"PropClass == {TARGET_PROPCLASS!r} only -- see inyo_process.py docstring for excluded classes",
            "no_internal_signal_reason": (
                "Inyo's parcel API has no multi-year history endpoint and no sale/document-date field, so "
                "unlike SF's pipeline there is no way to detect a reassessment event or build any 'jump-confirmed "
                "comp' from this county's own data -- market value is anchored entirely on the external ZHVI index."
            ),
            "sqft_deviation": (
                "The assigned method (zip_dollar_per_sqft = ZHVI / median_building_sqft, then "
                "parcel_market_value = zip_dollar_per_sqft * parcel_building_sqft) requires a building/living-area "
                "field. Inyo's ArcGIS layer has none anywhere in its schema -- LotSqFeet/LotAcres/SqFeet_gis/Acres_gis "
                "are all parcel/lot geometry, confirmed live against sample parcels (e.g. a single-family home with "
                "LotSqFeet=23,600, clearly a half-acre lot, not a home's floor area). Per that method's own documented "
                "fallback for this exact case, market value here is instead a PER-PROPERTY (not per-sqft) estimate: "
                "every single-family parcel in a ZIP gets that ZIP's own ZHVI value directly, floored at assessed "
                "value. The output 'sqft' column is filled with LotSqFeet (lot size, NOT building size) purely to "
                "satisfy the shared cross-county CSV schema -- it is not used anywhere in the market-value math and "
                "should not be read as comparable to SF's 'sqft' column (building/living area)."
            ),
            "assessed_value_definition": (
                "assessed = LandVal + ImproveVal + FixtureVal (matches SF's assessed_total convention). "
                "Deliberately NOT the API's own TotalVal field, which nets out ExemptVal (California's flat "
                "homeowner's exemption, almost always exactly $7,000) -- SF's pipeline never subtracts any "
                "exemption, so using TotalVal here would understate Inyo's assessed baseline for no principled "
                "reason and inflate every owner-occupied parcel's apparent subsidy by the exemption amount."
            ),
            "zip_corrections": (
                "Two single-family parcels had an internally inconsistent ZIP (situs city contradicted the ZIP "
                "field, in both cases matching a simple data-entry-typo pattern) and were corrected to the ZIP "
                "implied by their own city field before ZHVI lookup -- see ZIP_CORRECTIONS in inyo_process.py."
            ),
            "zhvi_fallback": (
                "ZIPs on Inyo's own roll with no direct ZHVI coverage (unsurprising for single-digit-population "
                "unincorporated desert communities) fall back to the ZHVI of the geographically nearest covered "
                "ZIP, matched by straight-line distance between parcel-centroid and ZIP-centroid averages."
            ),
            "external_index_caveat": (
                "IMPORTANT: this entire market-value estimate is anchored on ZHVI, which is itself Zillow's own "
                "smoothed/seasonally-adjusted statistical model of home values, not Inyo County's own confirmed "
                "sales or reassessment events. This is one layer further removed from ground truth than SF's "
                "jump-confirmed-comps approach, which is anchored on the county's own real reassessment data. "
                "Treat every 'market' figure in this dataset as an estimate of an estimate."
            ),
            "market_value_floor": (
                "Estimated market value is floored at the parcel's own assessed value, for the same reason as "
                "SF's pipeline: a below-assessed estimate is virtually always the model undershooting (here, "
                "ZHVI reflecting a ZIP-wide typical value that happens to sit below one specific parcel's "
                "already-grown Prop 13 assessment), not a real declining-value home."
            ),
            "tax_assumptions": {
                "current_general_rate_pct": GENERAL_RATE_CURRENT,
                "bond_rate_pct": BOND_RATE,
                "proposed_general_rate_pct": GENERAL_RATE_PROPOSED,
            },
            "generated": datetime.date.today().isoformat(),
        },
        "counts": {
            "total_single_family_parcels": rows_written,
            "direct_zhvi_coverage": direct_count,
            "fallback_zhvi_coverage": fallback_count,
            "fallback_pct": round(100 * fallback_count / rows_written, 1),
            "zips_present_on_inyo_roll": len(zip_centroid),
            "zips_with_direct_coverage": len(covered_zips),
            "zips_needing_fallback": len(uncovered_zips),
        },
        "zip_fallback_map": fallback_used,
        "zip_zhvi_values": {z: round(zhvi[z]["value"]) for z in sorted(covered_zips)},
        "stats": {
            "subsidy_vs_market_today": {
                "median": round(statistics.median(subsidy_all)),
                "mean": round(statistics.mean(subsidy_all)),
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
