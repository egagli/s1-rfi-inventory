"""Day-by-day, manifest-driven harvest of the global Sentinel-1 RFI annotation inventory.

Each UTC day (by product sensing start) is one unit of work:

1. catalogue search for the day (:func:`search_day`), keeping only the newest processing when the
   same slice was processed more than once;
2. harvest of every product's RFI annotation (:func:`s1rfi.inventory.harvest`);
3. consolidation into two files, ``noise_YYYY-MM-DD.parquet`` and ``bursts_YYYY-MM-DD.parquet``,
   with a ``duplicate`` column marking reports repeated in the overlap of consecutive slices
   (rows are kept, so nothing is lost);
4. a per-day summary of sampling effort and detections (:func:`summarize`).

A manifest (one row per day) records what is done, so a run can stop at any point (e.g. at the end
of a CI job's time budget) and the next run resumes. Recent days are re-checked, because products
reach the catalogue with a lag, and a day is redone if its catalogue count changed.
"""

import shutil
import time
from pathlib import Path

import pandas as pd

from s1rfi import cdse, inventory

MANIFEST_COLUMNS = ["day", "catalogue_products", "harvested", "failed", "attempts", "status", "last_run"]
FIRST_DAY = cdse.RFI_ANNOTATION_START.date()  # first day with RFI annotations
MAX_ATTEMPTS = 3  # a day whose failures persist this many runs is closed as "done" with failures


def read_manifest(path):
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=MANIFEST_COLUMNS)
    m = pd.read_csv(p, dtype={"status": str})
    m["day"] = pd.to_datetime(m["day"]).dt.date
    return m


def write_manifest(manifest, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    manifest.sort_values("day").to_csv(path, index=False)


def update_manifest(manifest, row):
    """Replace (or add) the row for ``row['day']``."""
    m = manifest[manifest["day"] != row["day"]]
    return pd.concat([m, pd.DataFrame([row])], ignore_index=True)[MANIFEST_COLUMNS].sort_values("day").reset_index(drop=True)


def pick_days(manifest, start, end, recheck_days=7, today=None):
    """Days in [start, end) to work on: every day not yet done (oldest first), then the done days
    among the last ``recheck_days`` before ``today`` (to pick up late catalogue entries)."""
    today = pd.Timestamp(today or pd.Timestamp.now("UTC").date()).date()
    days = [d.date() for d in pd.date_range(start, end, freq="D", inclusive="left")]
    done = set(manifest.loc[manifest["status"] == "done", "day"])
    cutoff = (pd.Timestamp(today) - pd.Timedelta(days=recheck_days)).date()
    pending = [d for d in days if d not in done]
    recheck = [d for d in days if d in done and d >= cutoff]
    return pending + recheck


def newest_processing(catalogue):
    """Keep one product per slice: the latest ``processing_date`` among names that differ only in
    their final 4-character unique id (the same slice processed more than once)."""
    if catalogue.empty:
        return catalogue
    key = catalogue["name"].str.removesuffix(".SAFE").str.rsplit("_", n=1).str[0]
    order = catalogue.assign(_key=key)
    if "processing_date" in order:
        order = order.sort_values("processing_date", na_position="first")
    return order.drop_duplicates("_key", keep="last").drop(columns="_key").sort_values("start").reset_index(drop=True)


def search_day(day, product_types=("IW_GRDH_1S", "EW_GRDM_1S"), search=cdse.search):
    start = pd.Timestamp(day)
    frames = [search(start, start + pd.Timedelta(days=1), product_type=pt) for pt in product_types]
    frames = [f for f in frames if len(f)]
    return newest_processing(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()


def consolidate(work_dir):
    """The day's reports from the per-product files, with a ``duplicate`` column."""
    out = {}
    for kind, t in (("noise", "noise_sensing_time"), ("bursts", "azimuth_time")):
        df = inventory.load(work_dir, kind)
        if len(df):
            df = df.sort_values(["product_name", t]).reset_index(drop=True)
            df["duplicate"] = inventory.duplicated(df, kind).to_numpy()
        out[kind] = df
    return out["noise"], out["bursts"]


def _mode(names):
    return names.str.split("_").str[1]


def summarize(day, catalogue, noise):
    """Per day x platform x mode x polarization: catalogue products, harvested products, noise
    reports and flagged reports (duplicates excluded), and products with RFI mitigation applied."""
    cols = ["day", "platform", "mode", "polarization", "catalogue_products", "products", "noise_reports",
            "flagged", "mitigated_products"]
    if catalogue.empty:
        return pd.DataFrame(columns=cols)
    cat = catalogue.assign(platform=catalogue["name"].str[:3], mode=_mode(catalogue["name"]))
    ncat = cat.groupby(["platform", "mode"]).size().rename("catalogue_products")
    if noise.empty:
        s = ncat.reset_index().assign(polarization=None, products=0, noise_reports=0, flagged=0, mitigated_products=0)
        return s.assign(day=day)[cols]
    n = noise[~noise["duplicate"]].assign(platform=noise["product_name"].str[:3], mode=_mode(noise["product_name"]))
    mitigated = n["rfi_mitigation_applied"].notna() & (n["rfi_mitigation_applied"].astype(str) != "None")
    n = n.assign(_mit_product=n["product_name"].where(mitigated))
    g = n.groupby(["platform", "mode", "polarization"]).agg(
        products=("product_name", "nunique"), noise_reports=("product_name", "size"),
        flagged=("rfi_detected", "sum"), mitigated_products=("_mit_product", "nunique")).reset_index()
    g = g.merge(ncat.reset_index(), on=["platform", "mode"], how="outer")
    for c in ("products", "noise_reports", "flagged", "mitigated_products"):
        g[c] = g[c].fillna(0).astype(int)
    return g.assign(day=day)[cols]


def day_paths(out_dir, day):
    d = pd.Timestamp(day)
    base = Path(out_dir) / f"{d:%Y}" / f"{d:%m}"
    return base / f"noise_{d:%Y-%m-%d}.parquet", base / f"bursts_{d:%Y-%m-%d}.parquet"


def run_day(client, day, out_dir, work_root, product_types=("IW_GRDH_1S", "EW_GRDM_1S"), workers=3,
            previous=None, log=print, search=cdse.search):
    """Harvest and consolidate one day. Returns (manifest row, summary rows, written paths), or
    None if the day was already done and its catalogue count has not changed."""
    cat = search_day(day, product_types, search=search)
    if previous is not None and previous.get("status") == "done" and int(previous["catalogue_products"]) == len(cat):
        log(f"{day}: unchanged ({len(cat)} products), skipped")
        return None
    work = Path(work_root) / f"{day}"
    if len(cat):
        inventory.harvest(client, cat, work, workers=workers, log=log, progress_every=200)
    noise, bursts = consolidate(work) if work.exists() else (pd.DataFrame(), pd.DataFrame())
    harvested = noise["product_name"].nunique() if len(noise) else 0
    # products without any noise report still count as harvested if their (empty) file exists
    done_files = {p.stem for p in (work / "noise").glob("*/*.parquet")} if work.exists() else set()
    harvested = max(harvested, len(done_files))
    failed = len(cat) - harvested
    attempts = int(previous["attempts"]) + 1 if previous is not None and previous.get("status") != "done" else 1
    status = "done" if failed == 0 or attempts >= MAX_ATTEMPTS else "partial"
    n_path, b_path = day_paths(out_dir, day)
    n_path.parent.mkdir(parents=True, exist_ok=True)
    noise.to_parquet(n_path, index=False, compression="zstd")
    bursts.to_parquet(b_path, index=False, compression="zstd")
    row = dict(day=day, catalogue_products=len(cat), harvested=harvested, failed=failed, attempts=attempts,
               status=status, last_run=pd.Timestamp.now("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"))
    if status == "done":
        shutil.rmtree(work, ignore_errors=True)  # per-product files are only a resume cache
    return row, summarize(day, cat, noise), [n_path, b_path]


def run(client, manifest_path, out_dir, work_root, summary_path, start=FIRST_DAY, end=None, recheck_days=7,
        budget_minutes=None, max_days=None, product_types=("IW_GRDH_1S", "EW_GRDM_1S"), workers=3, log=print,
        search=cdse.search):
    """Work through the pending days until done, ``max_days`` or the time budget. Writes the
    manifest and summary after every day, and returns the list of files written."""
    t0 = time.monotonic()
    end = end or pd.Timestamp.now("UTC").date()
    manifest = read_manifest(manifest_path)
    summary = pd.read_csv(summary_path, parse_dates=["day"]) if Path(summary_path).exists() else pd.DataFrame()
    if len(summary):
        summary["day"] = summary["day"].dt.date
    written, n = [], 0
    for day in pick_days(manifest, start, end, recheck_days):
        if budget_minutes is not None and (time.monotonic() - t0) / 60 > budget_minutes:
            log(f"time budget of {budget_minutes} min reached")
            break
        if max_days is not None and n >= max_days:
            break
        prev = manifest.loc[manifest["day"] == day]
        prev = prev.iloc[0].to_dict() if len(prev) else None
        res = run_day(client, day, out_dir, work_root, product_types, workers, prev, log, search)
        n += 1
        if res is None:
            continue
        row, rows, paths = res
        manifest = update_manifest(manifest, row)
        write_manifest(manifest, manifest_path)
        summary = pd.concat([summary[summary["day"] != day] if len(summary) else summary, rows], ignore_index=True)
        Path(summary_path).parent.mkdir(parents=True, exist_ok=True)
        summary.sort_values(["day", "platform", "mode", "polarization"]).to_csv(summary_path, index=False)
        written += paths
        log(f"{day}: {row['harvested']}/{row['catalogue_products']} products, {row['failed']} failed, {row['status']}")
    return written
