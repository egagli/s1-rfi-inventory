"""Harvest ESA Sentinel-1 RFI annotations from CDSE for an area and period.

Needs a free CDSE account in CDSE_USERNAME / CDSE_PASSWORD. Resumable: rerun the same
command to pick up products that failed or were not reached.

Examples
--------
One area and month, placing each report with the product's own geolocation grid (more exact, but
two more downloads per product):

    python scripts/harvest.py --start 2024-01-01 --end 2024-02-01 \
        --wkt "POLYGON((5 45,11 45,11 48,5 48,5 45))" --out data/alps_2024-01 --geolocate annotation

Whole globe for one day (footprint geolocation, which avoids the ~2 MB product annotations):

    python scripts/harvest.py --start 2024-01-10 --end 2024-01-11 --out data/global_20240110

For the day-by-day global inventory use ``scripts/run_days.py`` instead.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from s1rfi import cdse, inventory  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--wkt", help="area of interest polygon (lon lat, EPSG:4326); omit for global")
    ap.add_argument("--product-type", default="IW_GRDH_1S", help="comma-separated, e.g. IW_GRDH_1S,EW_GRDM_1S (or IW_SLC__1S)")
    ap.add_argument("--geolocate", choices=["footprint", "annotation"], default="footprint")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4, help="parallel downloads (keep small: be polite)")
    ap.add_argument("--min-interval", type=float, default=0.1,
                    help="minimum seconds between requests, shared by all workers (default 0.1 = 10 req/s max)")
    args = ap.parse_args()

    start = max(cdse.RFI_ANNOTATION_START.tz_localize(None), pd.Timestamp(args.start))
    products = pd.concat(
        [cdse.search(start, args.end, wkt=args.wkt, product_type=pt) for pt in args.product_type.split(",")],
        ignore_index=True,
    )
    print(f"catalogue: {len(products)} products")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    if len(products):
        products.to_parquet(Path(args.out) / f"catalogue_{start:%Y%m%dT%H%M}_{pd.Timestamp(args.end):%Y%m%dT%H%M}.parquet")
    client = cdse.Client(min_interval=args.min_interval)
    inventory.harvest(client, products, args.out, geolocate=args.geolocate, workers=args.workers)
    print("client stats:", client.stats)


if __name__ == "__main__":
    main()
