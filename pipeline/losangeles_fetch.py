"""
Fetch a bounded, geographically-contiguous sample of Los Angeles County
single-family-residential parcel data (current values AND multi-year
history in one shot) from the LA County Assessor's bulk "Assessor Parcel
Data (Rolls 2006-Present)" file, plus LA County's FRED house price index.

WHY THIS LOOKS DIFFERENT FROM SF'S 01/02 SPLIT
------------------------------------------------
LA County's assessor open-data item (ArcGIS item id
2231275cebd6426897bb9c2a7aaf9840, "Assessor Parcel Data (Rolls 2006 -
Present)") was scouted as a possible queryable FeatureServer but turned out,
on actually inspecting the item's metadata (url: null, type: "CSV"), to be a
single flat file, ~17.7GB (17,725,918,387 bytes) -- there is no Socrata/
FeatureServer query endpoint for it (a live ArcGIS MapServer *does* exist at
public.gis.lacounty.gov/.../LACounty_Parcel/MapServer/0 with a `where`-queryable
current-roll snapshot, but it lacks any sale/recording-date field, which
level would only get us a current snapshot, not jump-confirmed comps -- so
it was not used). We fetch the bulk file directly instead.

Two things about the real file, found by pulling and inspecting an actual
byte range before deciding anything (rather than trusting the mirrors'
guessed schema):
  1. Despite the ".csv" extension, fields are TAB-separated, and several
     guessed-at fields (SQFTmain1..5, YearBuilt1..5) don't exist -- the real
     fields are singular (SQFTmain, YearBuilt, Bedrooms, ...), i.e. one
     "main building" record per parcel-year row, plus a genuine
     RecordingDate (YYYYMMDD) field. That means the SAME full jump-confirmed
     comp methodology as SF (assessed-value jump >=8%/yr corroborated by a
     recorded sale within a year) is possible here, unlike a county with no
     sale-date field.
  2. Rows are AIN-major, RollYear-minor: each parcel's ~19 years (2006-2024
     in the vintage we hit) are contiguous, then the file moves to the next
     AIN. Since LA's AIN encodes assessor map-book/page (a geographic
     index), a contiguous *byte range* from the start of the file is a
     contiguous *geographic* swath, not a random scatter. A 50MB sample
     pulled from byte 0 came back entirely out of the Chatsworth / Winnetka
     / Canoga Park / West Hills corner of the northwest San Fernando Valley
     (zips 91304/91311/91307/91303).

SCOPING DECISION (per task instructions: don't download/process the full
17.7GB; cap to a bounded sample or a specific region)
------------------------------------------------------
We do a single HTTP Range request for the first FETCH_BYTES bytes of the
file (Range: bytes=0-N; the host confirmed Accept-Ranges: bytes) rather
than downloading all 17.7GB. Calibration on a real 50MB sample showed
~6,662 distinct SFR (UseType=='SFR') parcels per 50MB, each carrying ~18-19
years of history -- so FETCH_BYTES = 1GiB targets roughly 130k SFR parcels,
comfortably inside (in fact a bit past) the suggested 50k-150k range, and
gives every parcel in scope its FULL multi-year history for real
jump-confirmed comp detection (no separate history fetch needed, since this
one file already has both current values and history unified). The
resulting scope is effectively "the northwest San Fernando Valley portion
of LA County covered by the first ~1GB of the AIN-ordered roll" --
documented here and in the methodology JSON rather than silently baked in.
This is a deliberate deviation from SF's citywide scope; a subsequent run
could raise FETCH_BYTES (or start from a different byte offset) to cover a
different swath, at the cost of a bigger/slower fetch.

Also fetches LA County's FRED house price index (ATNHPIUS06037A, "All-
Transactions House Price Index for Los Angeles County, CA"), same pattern
as 13_fetch_hpi.py, for appreciation-adjusting older comps to today's-
equivalent price. NOTE: in this run, fred.stlouisfed.org was unreachable
from this environment (connection timeouts on every retry, including with
a browser User-Agent) -- this looks like a transient/sandbox network issue,
not a missing series, but we could not independently confirm the series'
contents this session. losangeles_process.py degrades gracefully (no
appreciation adjustment, i.e. adjusted_psf == raw_psf) if the HPI file
isn't present or is empty, and the methodology JSON records whichever
happened.

Reads:  nothing (hits the network)
Writes: pipeline/tmp/losangeles_bulk_sample.tsv  (raw byte-range slice, kept for debugging)
        pipeline/tmp/losangeles_sfr_raw.json     (parsed, UseType=='SFR'-filtered parcel-year rows)
        data/losangeles-hpi.json                 (FRED LA County HPI, if reachable)
"""
import csv
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
TMP_DIR = PIPELINE_DIR / "tmp"
DATA_DIR = PIPELINE_DIR.parent / "data"
BULK_SAMPLE_PATH = TMP_DIR / "losangeles_bulk_sample.tsv"
OUT_JSON = TMP_DIR / "losangeles_sfr_raw.json"
HPI_OUT = DATA_DIR / "losangeles-hpi.json"

# LA County Assessor's bulk "Assessor Parcel Data (Rolls 2006-Present)" item.
# ArcGIS item id 2231275cebd6426897bb9c2a7aaf9840; this /data endpoint 302s to
# the actual CloudFront-fronted file and supports byte-range requests.
BULK_URL = "https://lacounty.maps.arcgis.com/sharing/rest/content/items/2231275cebd6426897bb9c2a7aaf9840/data"

# First ~1GiB of the (AIN-ordered) file -> one contiguous geographic swath,
# ~130k SFR parcels with full multi-year history. See module docstring.
FETCH_BYTES = 1_073_741_824

CHUNK_SIZE = 8 * 1024 * 1024  # 8MB read chunks while streaming to disk
ENCODING = "latin-1"  # the real file isn't clean UTF-8 (confirmed by a decode error on a raw sample)

HPI_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=ATNHPIUS06037A"
HPI_SERIES = "ATNHPIUS06037A"


def fetch_bulk_sample(url, num_bytes, out_path):
    """Stream the first `num_bytes` of the bulk CSV to disk via an HTTP Range
    request, retrying transient failures. Returns bytes actually written."""
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{num_bytes - 1}"})
    for attempt in range(3):
        try:
            written = 0
            with urllib.request.urlopen(req, timeout=120) as resp, open(out_path, "wb") as f:
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    print(f"  downloaded {written / 1e6:.0f}MB / {num_bytes / 1e6:.0f}MB", file=sys.stderr)
            return written
        except Exception as e:
            print(f"  retry {attempt} after error: {e}", file=sys.stderr)
            time.sleep(3)
    raise RuntimeError("failed to fetch bulk sample after 3 attempts")


def parse_and_filter(tsv_path, out_json_path):
    """Parse the tab-separated sample and keep only UseType=='SFR' rows with
    usable geometry/sqft. Drops the last line if it looks truncated (the
    byte-range cutoff can land mid-row)."""
    rows = []
    dropped_truncated = 0
    kept_use_types = {}
    with open(tsv_path, encoding=ENCODING, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames
        expected_cols = len(fieldnames)
        for row in reader:
            if row.get(fieldnames[-1]) is None:
                # short row -- csv.DictReader pads missing trailing fields with None;
                # this only happens on a mid-row cutoff at the very end of the byte range.
                dropped_truncated += 1
                continue
            ut = row.get("UseType")
            kept_use_types[ut] = kept_use_types.get(ut, 0) + 1
            if ut != "SFR":
                continue
            if not row.get("AIN") or not row.get("CENTER_LAT") or not row.get("CENTER_LON"):
                continue
            rows.append(row)
    print(f"parsed rows by UseType: {kept_use_types}", file=sys.stderr)
    print(f"dropped {dropped_truncated} truncated trailing rows", file=sys.stderr)
    with open(out_json_path, "w") as f:
        json.dump(rows, f)
    return rows


def fetch_hpi(url, out_path):
    """Same approach as 13_fetch_hpi.py, parameterized for LA County (FIPS
    06037). Best-effort: writes an empty dict (rather than raising) if FRED
    is unreachable, so the rest of the pipeline can degrade gracefully."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        hpi = {}
        for row in reader:
            year = row["observation_date"][:4]
            value = row[HPI_SERIES]
            if value in ("", "."):
                continue
            hpi[year] = float(value)
        print(f"HPI years: {min(hpi)}-{max(hpi)} ({len(hpi)} points)", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: could not fetch LA County HPI from FRED ({e}); "
              f"writing empty HPI -- comps will not be appreciation-adjusted.", file=sys.stderr)
        hpi = {}
    with open(out_path, "w") as f:
        json.dump(hpi, f)
    return hpi


if __name__ == "__main__":
    TMP_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    print(f"fetching first {FETCH_BYTES / 1e9:.2f}GB of LA County bulk assessor roll...", file=sys.stderr)
    written = fetch_bulk_sample(BULK_URL, FETCH_BYTES, BULK_SAMPLE_PATH)
    print(f"DONE downloading {written / 1e6:.0f}MB -> {BULK_SAMPLE_PATH}", file=sys.stderr)

    print("parsing + filtering to UseType=='SFR'...", file=sys.stderr)
    rows = parse_and_filter(BULK_SAMPLE_PATH, OUT_JSON)
    distinct_ain = len({r["AIN"] for r in rows})
    print(f"DONE. {len(rows)} SFR parcel-year rows, {distinct_ain} distinct parcels -> {OUT_JSON}", file=sys.stderr)

    print("fetching LA County FRED HPI...", file=sys.stderr)
    fetch_hpi(HPI_URL, HPI_OUT)
    print(f"DONE -> {HPI_OUT}", file=sys.stderr)
