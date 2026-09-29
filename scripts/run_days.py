"""Build or extend the global inventory of Sentinel-1 RFI annotations, one UTC day at a time.

Needs a free Copernicus Data Space Ecosystem account in ``CDSE_USERNAME`` / ``CDSE_PASSWORD``.
Resumable: the manifest records finished days, so rerunning continues where the last run stopped.

    # the next 3 pending days, from the first day with RFI annotations (2021-11-04)
    python scripts/run_days.py --manifest state/manifest.csv --out out --max-days 3

    # a CI run: whatever fits in 5 hours, then list the files written for upload
    python scripts/run_days.py --manifest state/manifest.csv --out out --budget-minutes 300 \\
        --written out/written.txt
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from s1rfi import cdse, daily  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, help="CSV, one row per day (created if missing)")
    ap.add_argument("--out", required=True, help="output folder for the per-day Parquet files")
    ap.add_argument("--work", default=None, help="resume cache for per-product files (default: <out>/_work)")
    ap.add_argument("--summary", default="data/daily_summary.csv", help="per-day effort/detection table")
    ap.add_argument("--start", default=str(daily.FIRST_DAY))
    ap.add_argument("--end", default=None, help="exclusive; default today (UTC)")
    ap.add_argument("--recheck-days", type=int, default=7)
    ap.add_argument("--budget-minutes", type=float, default=None)
    ap.add_argument("--max-days", type=int, default=None)
    ap.add_argument("--product-type", default="IW_GRDH_1S,EW_GRDM_1S")
    ap.add_argument("--workers", type=int, default=3, help="parallel downloads (CDSE allows ~4 per account)")
    ap.add_argument("--min-interval", type=float, default=0.1, help="minimum seconds between requests")
    ap.add_argument("--written", default=None, help="append the paths of the files written to this file")
    args = ap.parse_args()
    client = cdse.Client(min_interval=args.min_interval)
    written = daily.run(client, args.manifest, args.out, args.work or Path(args.out) / "_work", args.summary,
                        start=pd.Timestamp(args.start).date(), end=args.end and pd.Timestamp(args.end).date(),
                        recheck_days=args.recheck_days, budget_minutes=args.budget_minutes, max_days=args.max_days,
                        product_types=tuple(args.product_type.split(",")), workers=args.workers)
    if args.written:
        with open(args.written, "a") as fh:
            fh.writelines(f"{p}\n" for p in written)
    print(f"{len(written)} files written; client stats: {client.stats}")


if __name__ == "__main__":
    main()
