"""
Build market-value estimates for every San Joaquin County single-family-
dwelling (SFD) parcel, using a genuinely different method than the rest of
this site (see pipeline/03_process_sfr.py's docstring for SF's approach).

WHY THIS COUNTY NEEDS A DIFFERENT METHOD
-----------------------------------------
SF's method finds "jump-confirmed comps": parcels whose assessed value jumped
far more than Prop 13's ~2%/yr cap allows, cross-checked against a recorded
sale date, to get a real price-per-sqft signal from the assessor's own
multi-year history. San Joaquin's public ArcGIS parcel feed
(services2.arcgis.com/.../Parcels/FeatureServer/0) is a single current-year
snapshot only -- no history endpoint -- and critically, its 72-field schema
has NO sale-date or document-date field anywhere (confirmed by inspecting
the service's field list and spot-querying live rows; see
sanjoaquin_fetch_parcels.py's docstring). VALUE_ROLL_YEAR is just "what year
is this roll", not "when did this parcel last transact". So there is no
internal signal in this county's data for whether a given parcel's assessed
value is fresh (a recent purchase, near market value under Prop 13) or
decades-stale (assessed value suppressed far below market value) -- the
jump-confirmed-comps method has nothing to detect a jump against.

THE METHOD USED INSTEAD: EXTERNAL ZIP-LEVEL BENCHMARK (ZHVI)
--------------------------------------------------------------
Rather than fabricate an internal signal that doesn't exist, this pipeline
anchors on Zillow Research's ZHVI (Zillow Home Value Index), single-family,
smoothed/seasonally-adjusted, by ZIP code (pipeline/sanjoaquin_fetch_zhvi.py)
-- a free, public, independently-updated read on typical home values that
requires no internal sale-date signal at all. Steps:

  1. For every ZIP present in San Joaquin's SFD parcel data, compute that
     ZIP's own latest ZHVI value (a typical/median single-family home value
     for that ZIP right now).
  2. Compute that ZIP's median TOTALLIV_AREA among its own SFD parcels --
     this part IS the county's own real sqft data, not an external estimate.
  3. Derive an implied $/sqft for the ZIP: zhvi_value / median_sqft.
  4. Estimate each parcel's market value as zip_dollar_per_sqft * that
     parcel's own TOTALLIV_AREA.
  5. Floor at assessed value (LAND_VALUE + IMPROVEMENT_VALUE +
     STRUCTURE_VALUE + FIXED_EQUIP_VALUE) -- same reasoning as SF: in
     today's market a home's true value essentially never sits below what
     Prop 13 has grown its assessment to, so a below-assessed estimate here
     is almost always the model undershooting, not a real declining-value
     home.

THIS IS ONE LAYER MORE REMOVED FROM GROUND TRUTH THAN SF'S METHOD. ZHVI is
itself Zillow's own smoothed, seasonally-adjusted *model*, fit to a mix of
public records and Zillow's internal estimates -- not a raw, confirmed
transaction feed. SF's jump-confirmed comps are anchored on the county's own
confirmed reassessment events (a real Prop 13 basis reset actually recorded
by the county). San Joaquin's estimate is anchored on an outside index that
itself involves modeling. It should not be mistaken for equally rigorous;
it is the best available substitute for a county whose own data has no
sale-recency signal whatsoever.

FALLBACK FOR ZIPS WITH NO ZHVI COVERAGE
-----------------------------------------
Zillow only publishes ZHVI for a ZIP once it has enough sale volume to
compute a stable index; sparsely-populated/rural ZIPs may have none. For
those, we borrow the $/sqft rate of the geographically nearest ZIP that DOES
have ZHVI coverage (nearest by the mean parcel centroid of each ZIP, great-
circle distance) rather than fabricate a number or silently drop the ZIP.
Every parcel affected by this fallback is tagged rate_source="nearest_zip_fallback"
in the full output table so it's auditable; the count is reported in
data/sanjoaquin-methodology.json.

ZIP ASSIGNMENT NOTE: the county's own SITUSZIP (property address ZIP) field
is populated for only ~15% of SFD parcels. The rest are assigned via nearest-
neighbor classification against SITUSZIP-confirmed parcels' own real lat/lon
-- see assign_zips() below for the full reasoning and why MAILZIPPREFIX
(owner mailing-address ZIP) was tried and rejected as a proxy (it produced
1,235 "distinct ZIPs" for one county -- clearly wrong).

DEVIATION FROM ASSIGNED METHODOLOGY -- ASSESSED VALUE FORMULA: the assigned
formula for assessed value was LAND_VALUE + IMPROVEMENT_VALUE + STRUCTURE_VALUE
+ FIXED_EQUIP_VALUE. Empirically, this double-counts: IMPROVEMENT_VALUE is a
rollup field that already equals STRUCTURE_VALUE + FIXED_EQUIP_VALUE +
TREE_VINE_VALUE + PERS_PROP_VALUE (verified with 0 mismatches on a 2,000-
parcel sample pulling all five fields; STRUCTURE_VALUE + FIXED_EQUIP_VALUE
alone matched IMPROVEMENT_VALUE on 178,941 of 178,991 parcels, 99.97%, county-
wide, with the remaining 0.03% explained by nonzero TREE_VINE_VALUE/
PERS_PROP_VALUE). Following the assigned formula literally would have roughly
doubled the non-land component of assessed value for virtually every parcel.
This pipeline instead uses assessed = LAND_VALUE + IMPROVEMENT_VALUE, the
county's actual total roll value. Flagged here, in load_parcels() below, and
in data/sanjoaquin-methodology.json.

Reads:  pipeline/tmp/sanjoaquin_parcels_raw.json  (sanjoaquin_fetch_parcels.py)
        pipeline/tmp/sanjoaquin_zhvi_ca.json      (sanjoaquin_fetch_zhvi.py)
Writes: pipeline/tmp/sanjoaquin-full.csv          (full per-parcel estimate table)
        data/sanjoaquin-methodology.json          (methodology + summary stats, committed)
"""
import csv
import datetime
import json
import math
import re
import statistics
import sys
from collections import defaultdict, Counter
from pathlib import Path

import numpy as np

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
REPO = PIPELINE_DIR.parent
PARCELS_RAW = TMP_DIR / "sanjoaquin_parcels_raw.json"
ZHVI_PATH = TMP_DIR / "sanjoaquin_zhvi_ca.json"
OUT_CSV = TMP_DIR / "sanjoaquin-full.csv"
OUT_METHODOLOGY = REPO / "data" / "sanjoaquin-methodology.json"

GENERAL_RATE_CURRENT = 1.00
BOND_RATE = 0.18   # held at SF's bond rate for cross-county comparability
GENERAL_RATE_PROPOSED = 0.70

MIN_SQFT = 200        # sanity floor, same spirit as SF's 200sqft cutoff
MAX_SQFT = 20000       # sanity ceiling, matches SF's 20,000sqft cutoff
MIN_PARCELS_FOR_ZIP_MEDIAN = 3  # need at least this many usable parcels in a zip to trust its median sqft
ANCHOR_MIN_COUNT = 3   # a SITUSZIP value needs at least this many parcels behind it to be trusted as a
                        # geographic anchor/classification target (guards against stray data-entry typos --
                        # e.g. a single parcel in the raw data carries SITUSZIP=95530, a real ZIP code but in
                        # Humboldt County, ~250 miles away -- clearly a typo, not a real situs address)


def to_float(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def valid_zip(z):
    if z is None:
        return None
    s = re.sub(r"\D", "", str(z))
    if len(s) >= 5:
        s = s[:5]
        if s[0] == "9":  # sanity check: CA zips start with 9
            return s
    return None


def clean_addr(row):
    parts = [row.get("SITUSNUMBER"), row.get("SITUSDIRECTION"), row.get("SITUSTREET"), row.get("SITUSTYPE")]
    addr = " ".join(p.strip() for p in parts if p and str(p).strip())
    if not addr:
        addr = (row.get("FULL_ADDRESS") or "").strip()
    addr = re.sub(r"\s+", " ", addr).strip()
    return addr.title()


def haversine(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_parcels(path):
    """Parse the raw snapshot. Zip assignment is intentionally NOT decided here:
    it needs the full parcel set loaded first (see assign_zips() below) since it's a
    geographic nearest-neighbor classification, not a per-row lookup. Each parcel keeps
    whatever raw, validated SITUSZIP it has (possibly None)."""
    raw = json.load(open(path))
    parcels = []
    n_no_geom = n_no_sqft = n_no_assessed = n_bad_sqft = 0
    for r in raw:
        lat, lon = r.get("_lat"), r.get("_lon")
        if lat is None or lon is None:
            n_no_geom += 1
            continue
        sqft = to_float(r.get("TOTALLIV_AREA"))
        if sqft is None or sqft <= 0:
            n_no_sqft += 1
            continue
        if sqft < MIN_SQFT or sqft > MAX_SQFT:
            n_bad_sqft += 1
            continue
        land = to_float(r.get("LAND_VALUE"), 0.0)
        impr = to_float(r.get("IMPROVEMENT_VALUE"), 0.0)
        # NOTE: assessed = LAND_VALUE + IMPROVEMENT_VALUE only. See the module docstring's
        # "DEVIATION FROM ASSIGNED METHODOLOGY" section: IMPROVEMENT_VALUE is a rollup that
        # already equals STRUCTURE_VALUE + FIXED_EQUIP_VALUE + TREE_VINE_VALUE + PERS_PROP_VALUE
        # (verified exactly, 0 mismatches, on a 2,000-parcel sample with all five fields pulled;
        # STRUCTURE_VALUE + FIXED_EQUIP_VALUE alone matched IMPROVEMENT_VALUE on 178,941 of
        # 178,991 parcels, i.e. 99.97%, in the full county pull -- the remaining 0.03% have
        # nonzero TREE_VINE_VALUE/PERS_PROP_VALUE, which is exactly what the rollup predicts).
        # Adding STRUCTURE_VALUE and FIXED_EQUIP_VALUE on top of IMPROVEMENT_VALUE, as the
        # originally-assigned formula (LAND_VALUE+IMPROVEMENT_VALUE+STRUCTURE_VALUE+
        # FIXED_EQUIP_VALUE) specified, would double-count the building value for virtually
        # every single-family parcel in the county -- roughly doubling the non-land component
        # of "assessed value" and badly distorting every downstream tax figure. This was caught
        # before shipping and corrected; see data/sanjoaquin-methodology.json for the same note.
        assessed = land + impr
        if assessed <= 0:
            n_no_assessed += 1
            continue

        parcels.append({
            "apn": r.get("APN"),
            "addr": clean_addr(r),
            "lat": lat, "lon": lon,
            "sqft": sqft,
            "assessed": assessed,
            "situs_zip": valid_zip(r.get("SITUSZIP")),
            "year_built": r.get("YEAR_BUILT"),
        })
    stats = {
        "input_rows": len(raw),
        "dropped_no_geometry": n_no_geom,
        "dropped_no_or_zero_sqft": n_no_sqft,
        "dropped_sqft_out_of_range": n_bad_sqft,
        "dropped_no_assessed_value": n_no_assessed,
        "usable_parcels": len(parcels),
    }
    return parcels, stats


def assign_zips(parcels):
    """Assign every parcel a ZIP code using ONLY the county's own situs address data and
    each parcel's own (accurate, GIS-derived) lat/lon -- never MAILZIPPREFIX.

    An earlier version of this pipeline fell back to MAILZIPPREFIX (the property owner's
    mailing-address ZIP) whenever SITUSZIP was blank -- which is true for the large
    majority of San Joaquin's parcels (SITUSZIP is populated for only ~15%). That turned
    out to be a bad proxy: MAILZIPPREFIX reflects wherever the OWNER receives mail, not
    where the property sits, so it's frequently a landlord's/bank's/trust's address
    somewhere else in California or even out of state (spot check: ~1,700 of ~179,000
    parcels had a MAILZIPPREFIX entirely outside California's ZIP range, and many more
    that pass a "starts with 9" sanity check are still nowhere near San Joaquin County --
    e.g. Bay Area or Southern California owner addresses). Using it produced 1,235
    "distinct ZIPs" for one county, which is nonsensical and would have silently fed
    wrong-city ZHVI rates into a meaningful share of parcels.

    Instead: parcels with a valid SITUSZIP keep it verbatim (that field, when present, IS
    the property's own address ZIP -- ground truth). ZIP values are used as a geographic
    classification anchor only if at least ANCHOR_MIN_COUNT parcels report that same
    SITUSZIP, which drops a small number of apparent data-entry typos (e.g. exactly one
    parcel in the raw data carries SITUSZIP=95530 -- a real ZIP, but in Humboldt County,
    ~250 miles from San Joaquin). For every parcel WITHOUT a usable SITUSZIP, we compute
    the centroid of each qualifying anchor ZIP (from its own SITUSZIP-labeled parcels'
    real lat/lon) and assign the parcel to whichever anchor ZIP's centroid its own lat/lon
    is nearest to -- a nearest-neighbor classification using the same coordinates already
    used to place the dot on the map, not the owner's mailing address.
    """
    anchor_counts = Counter(p["situs_zip"] for p in parcels if p["situs_zip"])
    qualifying_zips = {z for z, c in anchor_counts.items() if c >= ANCHOR_MIN_COUNT}
    dropped_typo_zips = {z: c for z, c in anchor_counts.items() if c < ANCHOR_MIN_COUNT}
    if dropped_typo_zips:
        print(f"SITUSZIP values dropped as likely typos (<{ANCHOR_MIN_COUNT} parcels): "
              f"{dropped_typo_zips}", file=sys.stderr)

    anchor_lat = defaultdict(list)
    anchor_lon = defaultdict(list)
    for p in parcels:
        if p["situs_zip"] in qualifying_zips:
            anchor_lat[p["situs_zip"]].append(p["lat"])
            anchor_lon[p["situs_zip"]].append(p["lon"])
    centroid_zips = sorted(qualifying_zips)
    centroid_lat = np.array([statistics.mean(anchor_lat[z]) for z in centroid_zips])
    centroid_lon = np.array([statistics.mean(anchor_lon[z]) for z in centroid_zips])
    lat0 = math.radians(statistics.mean(centroid_lat.tolist())) if len(centroid_zips) else 0.0

    to_classify = [p for p in parcels if p["situs_zip"] not in qualifying_zips]
    n_situs = len(parcels) - len(to_classify)
    if to_classify and len(centroid_zips):
        lat_arr = np.array([p["lat"] for p in to_classify])
        lon_arr = np.array([p["lon"] for p in to_classify])
        dx = (lon_arr[:, None] - centroid_lon[None, :]) * math.cos(lat0)
        dy = lat_arr[:, None] - centroid_lat[None, :]
        d2 = dx * dx + dy * dy
        nearest_idx = np.argmin(d2, axis=1)
        for p, idx in zip(to_classify, nearest_idx):
            p["zip"] = centroid_zips[idx]
            p["zip_source"] = "nearest_situs_centroid"
    else:
        for p in to_classify:
            p["zip"] = None
            p["zip_source"] = "unresolvable"

    for p in parcels:
        if p["situs_zip"] in qualifying_zips:
            p["zip"] = p["situs_zip"]
            p["zip_source"] = "situs"

    n_unresolvable = sum(1 for p in parcels if p.get("zip") is None)
    stats = {
        "anchor_ziplist": centroid_zips,
        "anchor_zip_count": len(centroid_zips),
        "dropped_typo_zips": dropped_typo_zips,
        "parcels_with_own_situszip": n_situs,
        "parcels_assigned_by_nearest_centroid": len(to_classify) - n_unresolvable,
        "parcels_with_no_resolvable_zip": n_unresolvable,
    }
    return [p for p in parcels if p.get("zip")], stats


def main():
    print("loading San Joaquin SFD parcel snapshot...", file=sys.stderr)
    parcels, load_stats = load_parcels(PARCELS_RAW)
    print(json.dumps(load_stats, indent=2), file=sys.stderr)

    print("assigning zips (situs where known, nearest-situs-centroid otherwise)...", file=sys.stderr)
    parcels, zip_stats = assign_zips(parcels)
    print(json.dumps({k: v for k, v in zip_stats.items() if k != "anchor_ziplist"}, indent=2), file=sys.stderr)
    load_stats.update(zip_stats)

    zhvi = json.load(open(ZHVI_PATH))

    by_zip = defaultdict(list)
    for p in parcels:
        by_zip[p["zip"]].append(p)
    print(f"distinct zips in usable parcel data: {len(by_zip)}", file=sys.stderr)

    # Step 2+3: per-zip median sqft (from the county's own data) and implied $/sqft (from ZHVI)
    zip_median_sqft = {}
    zip_psf_direct = {}
    for z, members in by_zip.items():
        if len(members) < MIN_PARCELS_FOR_ZIP_MEDIAN:
            continue  # too few parcels in this zip to trust a median -- will fall back below
        med_sqft = statistics.median(p["sqft"] for p in members)
        zip_median_sqft[z] = med_sqft
        zhvi_entry = zhvi.get(z)
        if zhvi_entry and med_sqft > 0:
            zip_psf_direct[z] = zhvi_entry["latest_value"] / med_sqft

    print(f"zips with direct ZHVI-derived $/sqft: {len(zip_psf_direct)} of {len(by_zip)}", file=sys.stderr)

    # zip centroids (mean of own parcels) for nearest-neighbor fallback
    zip_centroid = {
        z: (statistics.mean(p["lat"] for p in members), statistics.mean(p["lon"] for p in members))
        for z, members in by_zip.items()
    }

    zip_psf_final = dict(zip_psf_direct)
    zip_rate_source = {z: "zhvi_direct" for z in zip_psf_direct}
    fallback_zips = [z for z in by_zip if z not in zip_psf_direct]
    unresolvable_zips = []
    if zip_psf_direct:
        for z in fallback_zips:
            clat, clon = zip_centroid[z]
            best_z, best_d = None, None
            for cz in zip_psf_direct:
                if cz not in zip_centroid:
                    continue
                d = haversine(clat, clon, *zip_centroid[cz])
                if best_d is None or d < best_d:
                    best_d, best_z = d, cz
            if best_z is not None:
                zip_psf_final[z] = zip_psf_direct[best_z]
                zip_rate_source[z] = f"nearest_zip_fallback:{best_z}"
            else:
                unresolvable_zips.append(z)
    else:
        unresolvable_zips = fallback_zips

    print(f"zips resolved via nearest-zip fallback: {len(fallback_zips) - len(unresolvable_zips)}", file=sys.stderr)
    print(f"zips with NO resolvable rate (skipped entirely): {unresolvable_zips}", file=sys.stderr)

    fieldnames = [
        "apn", "addr", "lat", "lon", "sqft", "zip", "zip_source", "year_built",
        "assessed", "market", "price_per_sqft_used", "rate_source",
        "current_tax", "subsidy", "reform_tax", "change",
    ]

    rows_written = 0
    parcels_skipped_no_rate = 0
    fallback_parcel_count = 0
    subsidy_all, change_all = [], []
    increases = decreases = 0

    with open(OUT_CSV, "w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=fieldnames)
        writer.writeheader()
        for p in parcels:
            z = p["zip"]
            psf = zip_psf_final.get(z)
            if psf is None:
                parcels_skipped_no_rate += 1
                continue
            rate_source = zip_rate_source[z]
            if rate_source.startswith("nearest_zip_fallback"):
                fallback_parcel_count += 1

            market = psf * p["sqft"]
            # Floor at assessed value -- same reasoning as SF's pipeline (03_process_sfr.py):
            # a below-assessed estimate is virtually always the model undershooting, not a
            # real declining-value home.
            market = max(market, p["assessed"])

            current_tax = p["assessed"] * (GENERAL_RATE_CURRENT + BOND_RATE) / 100
            market_tax_current_law = market * (GENERAL_RATE_CURRENT + BOND_RATE) / 100
            reform_tax = market * (GENERAL_RATE_PROPOSED + BOND_RATE) / 100
            subsidy = market_tax_current_law - current_tax
            change = reform_tax - current_tax

            subsidy_all.append(subsidy)
            change_all.append(change)
            if change > 0:
                increases += 1
            elif change < 0:
                decreases += 1

            writer.writerow({
                "apn": p["apn"], "addr": p["addr"],
                "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                "sqft": p["sqft"], "zip": z, "zip_source": p["zip_source"],
                "year_built": p["year_built"],
                "assessed": round(p["assessed"]), "market": round(market),
                "price_per_sqft_used": round(psf, 2), "rate_source": rate_source,
                "current_tax": round(current_tax), "subsidy": round(subsidy),
                "reform_tax": round(reform_tax), "change": round(change),
            })
            rows_written += 1

    print(f"WROTE {rows_written} rows -> {OUT_CSV}", file=sys.stderr)
    print(f"parcels skipped for lack of any resolvable zip rate: {parcels_skipped_no_rate}", file=sys.stderr)

    methodology = {
        "county": "San Joaquin",
        "generated": datetime.date.today().isoformat(),
        "site_context": (
            "Extends oneratecalifornia.org (previously SF-only) to San Joaquin County. "
            "The map compares each home's current property tax (assessed value x current "
            "rate) to what it would owe if taxed on today's real market value, and to a "
            "modeled reform scenario."
        ),
        "data_sources": {
            "parcel_data": {
                "source": "San Joaquin County Assessor, via opendata.sjgov.org public ArcGIS REST API",
                "url": "https://services2.arcgis.com/GQhSReJEO6f7tsvy/arcgis/rest/services/Parcels/FeatureServer/0",
                "scope": "USECODE='10' (SINGLE FAMILY DWELLING(SFD)) parcels only -- excludes condos, duplexes, "
                         "rural residences, and all other residential use codes.",
                "snapshot_type": "Single current-year assessor roll snapshot (VALUE_ROLL_YEAR); no multi-year "
                                 "history endpoint is published for this county.",
            },
            "market_value_benchmark": {
                "source": "Zillow Research ZHVI, Single-Family Homes, Smoothed, Seasonally Adjusted, by ZIP Code",
                "url": "https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_sfr_tier_0.33_0.67_sm_sa_month.csv",
                "landing_page": "https://www.zillow.com/research/data/",
                "series_meaning": "Zillow's estimate of the typical single-family home value in the middle price "
                                   "tier (33rd-67th percentile) of each ZIP, smoothed and seasonally adjusted, "
                                   "updated monthly.",
            },
        },
        "methodology": {
            "why_a_different_method_than_sf": (
                "San Joaquin's public parcel data has no sale-date or document-date field anywhere in its "
                "72-field schema (verified directly against the live ArcGIS service's field list and sample "
                "rows) and no multi-year history endpoint, unlike SF's assessor roll. There is therefore no "
                "internal signal at all for whether any given parcel's assessed value is fresh (near a recent "
                "purchase, so close to market value under Prop 13) or decades-stale (assessed value far below "
                "market value). SF's 'jump-confirmed comps' method (03_process_sfr.py) depends entirely on "
                "detecting a reassessment jump in multi-year history cross-checked against a sale date -- "
                "neither ingredient exists here."
            ),
            "estimation_steps": [
                "1. Scope to USECODE='10' (Single Family Dwelling) parcels with usable geometry, sqft "
                "(TOTALLIV_AREA), assessed value, and a resolvable ZIP.",
                "2. For every ZIP present in the parcel data, compute that ZIP's median TOTALLIV_AREA among its "
                "own SFD parcels -- this step uses the county's own real sqft data, not an external estimate.",
                "3. Look up that ZIP's latest ZHVI value (a typical single-family home value for that ZIP right "
                "now) and derive an implied $/sqft = ZHVI value / median sqft.",
                "4. Estimate each parcel's market value as (its ZIP's $/sqft) x (its own TOTALLIV_AREA).",
                "5. Floor the estimate at the parcel's assessed value (LAND_VALUE + IMPROVEMENT_VALUE + "
                "STRUCTURE_VALUE + FIXED_EQUIP_VALUE): in today's market a home's true value essentially never "
                "sits below what Prop 13 has grown its assessment to, so a below-assessed estimate is virtually "
                "always the model undershooting rather than a real declining-value home (same reasoning SF's "
                "pipeline uses).",
            ],
            "rigor_caveat": (
                "THIS IS A LESS RIGOROUS ESTIMATE THAN SF'S. SF's jump-confirmed-comps method anchors on the "
                "county's own confirmed reassessment events -- an actual Prop 13 basis reset that really "
                "happened, recorded by the county itself. San Joaquin's estimate instead anchors on ZHVI, which "
                "is itself Zillow's own smoothed, seasonally-adjusted MODEL (fit to a blend of public records and "
                "Zillow's internal estimates), not a raw, confirmed transaction feed. That makes this estimate "
                "one additional layer removed from ground truth: an estimate built on top of another organization's "
                "estimate, rather than on the county's own confirmed sales. This should not be read as equally "
                "rigorous to the SF numbers on this site -- it is the best available substitute for a county "
                "whose own data provides no sale-recency signal whatsoever."
            ),
            "zip_coverage_fallback": (
                f"A ZIP falls back to a borrowed rate if either: (a) Zillow has no ZHVI value for it at all "
                "(Zillow only publishes a ZHVI value once a ZIP has enough sale volume to compute a stable "
                f"index, so some typically rural/sparsely-populated ZIPs have none), or (b) the ZIP has fewer "
                f"than {MIN_PARCELS_FOR_ZIP_MEDIAN} usable SFD parcels of its own in the county data, too few to "
                "trust a median-sqft figure computed from them. For those, the $/sqft rate is borrowed from the "
                "geographically nearest ZIP (by mean "
                "parcel centroid, great-circle distance) that DOES have a ZHVI-derived rate, rather than "
                "fabricating a number. Parcels using a borrowed rate are tagged rate_source=nearest_zip_fallback:<ZIP> "
                "in the full per-parcel table (pipeline/tmp/sanjoaquin-full.csv) for auditability. If a ZIP has no "
                "resolvable rate at all (no ZHVI coverage anywhere nearby), its parcels are skipped entirely "
                "rather than assigned a fabricated value."
            ),
            "zip_assignment_note": (
                "The parcel data's own SITUSZIP (property address ZIP) field is populated for only ~15% of SFD "
                "parcels. An earlier version of this pipeline fell back to MAILZIPPREFIX (the owner's mailing "
                "address ZIP) for the rest, but that field turned out to be a poor location proxy: it reflects "
                "wherever the OWNER receives mail (frequently a landlord, bank, or trust elsewhere in California "
                "or out of state), not where the property sits -- using it produced 1,235 distinct 'ZIPs' for a "
                "single county. Instead, every parcel's own SITUSZIP is kept verbatim when present (treated as "
                f"ground truth if at least {ANCHOR_MIN_COUNT} parcels share it, which excludes a small number of "
                "apparent data-entry typos -- e.g. exactly one parcel carried SITUSZIP=95530, a real ZIP code but "
                "in Humboldt County, ~250 miles away). For parcels without a usable SITUSZIP, the ZIP is inferred "
                "by finding which qualifying ZIP's centroid (computed from its own SITUSZIP-labeled parcels' real "
                "lat/lon) the parcel's own lat/lon is geographically nearest to -- a nearest-neighbor "
                "classification on the same GIS coordinates already used to place the dot on the map, never the "
                "owner's mailing address. Each parcel is tagged zip_source=situs or "
                "zip_source=nearest_situs_centroid in the full table."
            ),
            "deviation_assessed_value_formula": (
                "The assigned methodology specified assessed = LAND_VALUE + IMPROVEMENT_VALUE + STRUCTURE_VALUE + "
                "FIXED_EQUIP_VALUE. This was changed to assessed = LAND_VALUE + IMPROVEMENT_VALUE after verifying "
                "against the live ArcGIS service that IMPROVEMENT_VALUE is a rollup field that already equals "
                "STRUCTURE_VALUE + FIXED_EQUIP_VALUE + TREE_VINE_VALUE + PERS_PROP_VALUE (0 mismatches across a "
                "2,000-parcel sample pulling all five value fields; STRUCTURE_VALUE + FIXED_EQUIP_VALUE alone "
                "matched IMPROVEMENT_VALUE on 178,941 of 178,991 parcels -- 99.97% -- county-wide, with the "
                "remaining 0.03% fully explained by nonzero TREE_VINE_VALUE/PERS_PROP_VALUE). Using the originally "
                "assigned formula would have added STRUCTURE_VALUE and FIXED_EQUIP_VALUE a second time on top of "
                "IMPROVEMENT_VALUE, roughly doubling the non-land component of assessed value for virtually every "
                "parcel in the county and badly distorting every downstream tax figure on the map. This was caught "
                "during development (a suspiciously-high sample assessed value prompted the check) and corrected "
                "before this dataset was finalized."
            ),
            "tax_assumptions": {
                "current_general_rate_pct": GENERAL_RATE_CURRENT,
                "bond_rate_pct": BOND_RATE,
                "bond_rate_note": "Held at SF's bond rate, not San Joaquin's actual local bond rate, so subsidy/"
                                  "change figures stay comparable across counties on this site.",
                "proposed_general_rate_pct": GENERAL_RATE_PROPOSED,
                "current_tax_formula": "assessed * (1.00 + 0.18) / 100",
                "market_tax_current_law_formula": "market * (1.00 + 0.18) / 100",
                "subsidy_formula": "market_tax_current_law - current_tax",
                "reform_tax_formula": "market * (0.70 + 0.18) / 100",
                "change_formula": "reform_tax - current_tax",
            },
        },
        "counts": {
            **load_stats,
            "distinct_zips_in_parcel_data": len(by_zip),
            "zips_with_direct_zhvi_rate": len(zip_psf_direct),
            "zips_resolved_via_nearest_zip_fallback": len(fallback_zips) - len(unresolvable_zips),
            "zips_with_no_resolvable_rate": len(unresolvable_zips),
            "unresolvable_zip_list": unresolvable_zips,
            "parcels_using_fallback_rate": fallback_parcel_count,
            "parcels_skipped_no_resolvable_rate": parcels_skipped_no_rate,
            "final_rows_written": rows_written,
        },
        "stats": {
            "subsidy": {
                "median": round(statistics.median(subsidy_all)) if subsidy_all else None,
                "mean": round(statistics.mean(subsidy_all)) if subsidy_all else None,
                "min": round(min(subsidy_all)) if subsidy_all else None,
                "max": round(max(subsidy_all)) if subsidy_all else None,
            },
            "under_reform": {
                "would_pay_more": increases,
                "would_pay_less": decreases,
                "pct_pay_more": round(100 * increases / rows_written, 1) if rows_written else None,
                "pct_pay_less": round(100 * decreases / rows_written, 1) if rows_written else None,
            },
        },
    }
    with open(OUT_METHODOLOGY, "w") as f:
        json.dump(methodology, f, indent=2)
    print(f"wrote methodology -> {OUT_METHODOLOGY}", file=sys.stderr)


if __name__ == "__main__":
    main()
