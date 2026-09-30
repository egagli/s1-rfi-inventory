"""Rebuild ``data/daily_summary.csv`` rows for done days that are missing from it.

For each day marked done in the manifest (the ``manifest`` release) but absent from the summary,
the day's catalogue is searched again (anonymous CDSE catalogue) and its noise file is downloaded
from the day's release; the rows are recomputed with ``s1rfi.daily.summarize`` and merged in.

    python scripts/rebuild_summary.py --summary data/daily_summary.csv
"""

import argparse
import io
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from s1rfi import daily  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", default="data/daily_summary.csv")
    ap.add_argument("--repo", default="egagli/s1-rfi-inventory")
    ap.add_argument("--product-type", default="IW_GRDH_1S,EW_GRDM_1S")
    args = ap.parse_args()
    s = requests.Session()
    base = f"https://github.com/{args.repo}/releases/download"
    manifest = pd.read_csv(io.StringIO(s.get(f"{base}/manifest/manifest.csv", timeout=120).text))
    summary = pd.read_csv(args.summary) if Path(args.summary).exists() else pd.DataFrame(columns=["day"])
    have = set(pd.to_datetime(summary["day"]).dt.date) if len(summary) else set()
    todo = [d for d in pd.to_datetime(manifest.loc[manifest.status == "done", "day"]).dt.date if d not in have]
    print(f"{len(todo)} done days missing from the summary")
    rows = []
    for d in todo:
        cat = daily.search_day(d, tuple(args.product_type.split(",")))
        r = s.get(f"{base}/data-{d:%Y-%m}/noise_{d:%Y-%m-%d}.parquet", timeout=300)
        r.raise_for_status()
        rows.append(daily.summarize(d, cat, pd.read_parquet(io.BytesIO(r.content))))
        print(d, len(cat), "products", flush=True)
    merged = daily.merge_summaries(summary, pd.concat(rows) if rows else None)
    merged.to_csv(args.summary, index=False)
    print(f"summary now has {merged['day'].nunique()} days")


if __name__ == "__main__":
    main()
