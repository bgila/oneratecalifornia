"""
Per-neighborhood subsidy stats, on a per-HOME basis (each unit in a multi-family
building counts as its own home, not the building as a whole).

Two metrics, both averaged across homes within a neighborhood:
  - pct_subsidy:    (est_market_value - assessed_value) / est_market_value * 100
                    the tax rate cancels out of this ratio entirely (both the
                    "assessed" tax and the "at full market value" tax use the same
                    current-law rate), so this is just the assessment gap as a
                    percent of true value -- how big a break a home's LOW
                    assessment gives it, independent of the tax rate applied.
  - dollar_subsidy: subsidy_vs_market_today, converted to a per-home basis for
                    multi-family buildings (building subsidy / units).

Multi-family rows with missing/zero unit counts are excluded (no way to know the
true per-home split), which is a small minority of buildings (~5%, per pipeline
README).

Neighborhood assignment: nearest centroid from data/sf-neighborhoods.json, the
exact same method the live map itself uses for its neighborhood badge -- there is
no neighborhood column in the slim map-data CSVs (only in the raw pipeline
intermediates, which aren't committed/kept around).

Reads:  data/sf-map-data.csv, data/sf-map-data-mf.csv, data/sf-neighborhoods.json
Writes: analysis/neighborhood-subsidy-stats.csv, analysis/neighborhood_pct_subsidy_map.png,
        analysis/neighborhood_dollar_subsidy_map.png
"""
import csv
import json
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon
from matplotlib.collections import PatchCollection

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "analysis"
OUT_DIR.mkdir(exist_ok=True)

nb_json = json.loads((REPO / "data" / "sf-neighborhoods.json").read_text())
centroids = nb_json["centroids"]
boundaries = nb_json["boundaries"]

nb_names = list(centroids.keys())
centroid_arr = np.array([centroids[n] for n in nb_names])  # [n_nb, 2] (lat, lon)


def assign_neighborhoods(lat, lon):
    # squared-distance nearest centroid, matching map.js's nearestNeighborhood()
    d_lat = centroid_arr[:, 0][None, :] - lat[:, None]
    d_lon = centroid_arr[:, 1][None, :] - lon[:, None]
    d2 = d_lat * d_lat + d_lon * d_lon
    idx = np.argmin(d2, axis=1)
    return np.array(nb_names)[idx]


# ---------- load single-family/condo (one row = one home, weight 1) ----------
sfr_lat, sfr_lon, sfr_assessed, sfr_market, sfr_subsidy = [], [], [], [], []
with open(REPO / "data" / "sf-map-data.csv") as f:
    for r in csv.DictReader(f):
        m = float(r["market"])
        if m <= 0:
            continue
        sfr_lat.append(float(r["lat"]))
        sfr_lon.append(float(r["lon"]))
        sfr_assessed.append(float(r["assessed"]))
        sfr_market.append(m)
        sfr_subsidy.append(float(r["subsidy"]))

sfr_lat = np.array(sfr_lat)
sfr_lon = np.array(sfr_lon)
sfr_assessed = np.array(sfr_assessed)
sfr_market = np.array(sfr_market)
sfr_subsidy = np.array(sfr_subsidy)
sfr_pct = (sfr_market - sfr_assessed) / sfr_market * 100
sfr_dollar_per_home = sfr_subsidy
sfr_weight = np.ones(len(sfr_lat))
sfr_nb = assign_neighborhoods(sfr_lat, sfr_lon)

# ---------- load multi-family (one row = one building, weight = units) ----------
mf_lat, mf_lon, mf_assessed, mf_market, mf_subsidy, mf_units = [], [], [], [], [], []
mf_skipped_no_units = 0
with open(REPO / "data" / "sf-map-data-mf.csv") as f:
    for r in csv.DictReader(f):
        m = float(r["market"])
        u = float(r["units"]) if r["units"] not in ("", None) else 0
        if m <= 0:
            continue
        if u <= 0:
            mf_skipped_no_units += 1
            continue
        mf_lat.append(float(r["lat"]))
        mf_lon.append(float(r["lon"]))
        mf_assessed.append(float(r["assessed"]))
        mf_market.append(m)
        mf_subsidy.append(float(r["subsidy"]))
        mf_units.append(u)

mf_lat = np.array(mf_lat)
mf_lon = np.array(mf_lon)
mf_assessed = np.array(mf_assessed)
mf_market = np.array(mf_market)
mf_subsidy = np.array(mf_subsidy)
mf_units = np.array(mf_units)
mf_pct = (mf_market - mf_assessed) / mf_market * 100
mf_dollar_per_home = mf_subsidy / mf_units
mf_weight = mf_units  # each building counts once per unit it contains
mf_nb = assign_neighborhoods(mf_lat, mf_lon)

print(f"SFR homes: {len(sfr_lat):,}")
print(f"MF buildings used: {len(mf_lat):,} ({mf_units.sum():,.0f} homes); "
      f"skipped {mf_skipped_no_units:,} buildings with no unit count")

# ---------- combine and aggregate per neighborhood (weighted mean) ----------
all_nb = np.concatenate([sfr_nb, mf_nb])
all_pct = np.concatenate([sfr_pct, mf_pct])
all_dollar = np.concatenate([sfr_dollar_per_home, mf_dollar_per_home])
all_weight = np.concatenate([sfr_weight, mf_weight])

stats = {}
for name in nb_names:
    mask = all_nb == name
    w = all_weight[mask]
    if w.sum() == 0:
        continue
    stats[name] = {
        "n_homes": int(round(w.sum())),
        "avg_pct_subsidy": float(np.average(all_pct[mask], weights=w)),
        "avg_dollar_subsidy_per_home": float(np.average(all_dollar[mask], weights=w)),
    }

# ---------- write CSV ----------
csv_path = OUT_DIR / "neighborhood-subsidy-stats.csv"
with open(csv_path, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["neighborhood", "n_homes", "avg_pct_subsidy", "avg_dollar_subsidy_per_home"])
    for name in sorted(stats, key=lambda n: -stats[n]["avg_dollar_subsidy_per_home"]):
        s = stats[name]
        writer.writerow([name, s["n_homes"], round(s["avg_pct_subsidy"], 2), round(s["avg_dollar_subsidy_per_home"])])
print(f"wrote {csv_path}")


# ---------- choropleth maps ----------
def draw_choropleth(value_key, title, cbar_label, out_path, cmap_name, fmt):
    fig, ax = plt.subplots(figsize=(9, 10))
    values = [stats[n][value_key] for n in stats]
    vmin, vmax = min(values), max(values)
    cmap = plt.get_cmap(cmap_name)
    patches = []
    colors = []
    for name, s in stats.items():
        rings = boundaries.get(name)
        if not rings:
            continue
        norm = (s[value_key] - vmin) / (vmax - vmin) if vmax > vmin else 0.5
        for ring in rings:
            xy = [(lon, lat) for lat, lon in ring]
            patches.append(Polygon(xy, closed=True))
            colors.append(norm)
    coll = PatchCollection(patches, cmap=cmap, edgecolor="#333333", linewidths=0.4)
    coll.set_array(np.array(colors))
    coll.set_clim(0, 1)
    ax.add_collection(coll)
    ax.autoscale_view()
    ax.set_aspect(1 / np.cos(np.radians(37.7749)))
    ax.axis("off")
    ax.set_title(title, fontsize=14)
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(vmin=vmin, vmax=vmax))
    cbar = fig.colorbar(sm, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label(cbar_label)
    cbar.ax.yaxis.set_major_formatter(plt.FuncFormatter(fmt))
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"wrote {out_path}")


draw_choropleth(
    "avg_pct_subsidy",
    "Average per-home subsidy, as % of market value's fair-share tax",
    "avg % subsidy",
    OUT_DIR / "neighborhood_pct_subsidy_map.png",
    "cividis",
    lambda v, _: f"{v:.0f}%",
)
draw_choropleth(
    "avg_dollar_subsidy_per_home",
    "Average per-home subsidy, in dollars/year",
    "avg $ subsidy / home / yr",
    OUT_DIR / "neighborhood_dollar_subsidy_map.png",
    "cividis",
    lambda v, _: f"${v:,.0f}",
)
