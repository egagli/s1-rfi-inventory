"""Build an inventory of ESA per-product RFI annotations for a region and period.

For each product found by :func:`s1rfi.cdse.search`, the RFI annotation file(s) are fetched,
parsed and geolocated, and written as one Parquet file per product under ``out_dir`` so that
interrupted runs resume where they stopped:

- ``noise/<YYYY-MM>/<product>.parquet``: one row per noise-sequence report per polarization
  (the detection record *and* the sampling effort, since non-detections are kept).
- ``bursts/<YYYY-MM>/<product>.parquet``: one row per burst report per polarization, placed
  the same way as the noise reports.

Consecutive slices of a datatake repeat the reports in their overlap; use :func:`dedupe`
before counting.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from s1rfi import annotation, cdse

PRODUCT_COLUMNS = ["id", "name", "platform", "orbit_direction", "relative_orbit", "start", "end",
                   "processor_version", "timeliness"]  # the last two only if the catalogue has them


def _paths(out_dir, product):
    month = f"{pd.Timestamp(product['start']):%Y-%m}"
    stem = product["name"].removesuffix(".SAFE")
    return (Path(out_dir) / "noise" / month / f"{stem}.parquet", Path(out_dir) / "bursts" / month / f"{stem}.parquet")


def _rfi_names_by_listing(client, product):
    """List ``annotation/`` and ``annotation/rfi/``. Returns None if the product has no RFI folder."""
    safe = product["name"]
    ann = client.list_nodes(product["id"], safe, "annotation")
    if not any(n["name"] == "rfi" for n in ann):
        return None  # processed before IPF 3.40: no RFI annotation
    return sorted(n["name"] for n in client.list_nodes(product["id"], safe, "annotation", "rfi"))


def process_product(client, product, geolocate="footprint"):
    """Fetch, parse and locate the RFI annotations of one product (a row of ``cdse.search``).

    GRD file names are built from the product name (:func:`annotation.rfi_file_names`), so a
    product costs one download per polarization (two more with ``geolocate="annotation"``)
    and no listings. If a built name is not found, or the product is not a GRD, the folders
    are listed instead. Returns ``(None, None)`` if the product has no annotation folder and
    two empty frames if it predates the RFI annotation.
    """
    safe = product["name"]

    def fetch(*path):
        return client.get_file(product["id"], safe, "annotation", *path)

    names = annotation.rfi_file_names(safe)
    rfi = {}
    if names:
        try:
            rfi = {n: fetch("rfi", n) for n in names}
        except cdse.NotFound:
            rfi = {}
    if not rfi:
        top = {n["name"] for n in client.list_nodes(product["id"], safe)}
        if "annotation" not in top:
            return None, None
        names = _rfi_names_by_listing(client, product)
        if names is None:
            return pd.DataFrame(), pd.DataFrame()
        rfi = {n: fetch("rfi", n) for n in names}

    noise_frames, burst_frames = [], []
    for name, xml in rfi.items():
        header, noise, bursts = annotation.parse_rfi(xml)
        if geolocate == "annotation":
            # rfi-<product annotation name>: pair each RFI file with its own product annotation
            grid, bounds = annotation.parse_geolocation(fetch(name.removeprefix("rfi-")))
            noise = annotation.locate_noise_reports(noise, grid, bounds or None)
            if len(bursts):
                bursts = annotation.locate_noise_reports(bursts, grid, bounds or None, time_col="azimuth_time")
        else:
            args = (product["footprint"], product["start"], product["end"], product["orbit_direction"])
            noise = annotation.locate_from_footprint(noise, *args)
            if len(bursts):
                bursts = annotation.locate_from_footprint(bursts, *args, time_col="azimuth_time")
        for df in (noise, bursts):
            df["polarization"] = header["polarization"]
            df["rfi_mitigation_applied"] = header["rfi_mitigation_applied"]
            df["geolocation"] = geolocate
            for c in PRODUCT_COLUMNS:
                df[f"product_{c}"] = product.get(c)
        noise_frames.append(noise)
        burst_frames.append(bursts)
    return pd.concat(noise_frames, ignore_index=True), pd.concat(burst_frames, ignore_index=True)


def harvest(client, products, out_dir, geolocate="footprint", workers=4, log=print, progress_every=50):
    """Process every product not already on disk. Returns the number newly written.

    Logs throughput (products/s, requests/s, MB) every ``progress_every`` products and at the end.
    """
    todo = [p for _, p in products.iterrows() if not _paths(out_dir, p)[0].exists()]
    log(f"{len(products)} products, {len(products) - len(todo)} already done, {len(todo)} to fetch")
    done = failed = 0
    t0 = time.monotonic()

    def rate():
        dt = max(time.monotonic() - t0, 1e-9)
        st = getattr(client, "stats", {})
        req = st.get("downloads", 0) + st.get("listings", 0)
        return (
            f"[{done + failed}/{len(todo)} in {dt:.0f} s: {(done + failed) / dt:.2f} products/s, "
            f"{req / dt:.1f} requests/s, {st.get('bytes', 0) / 1e6:.1f} MB, "
            f"{st.get('http_429', 0)} x 429, {st.get('http_5xx', 0)} x 5xx, {failed} failed]"
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_product, client, p, geolocate): p for p in todo}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                noise, bursts = fut.result()
            except Exception as e:  # keep going; the product is retried on the next run
                failed += 1
                log(f"FAILED {p['name']}: {e}")
                continue
            if noise is None:
                failed += 1
                log(f"skip {p['name']}: no annotation folder")
                continue
            n_path, b_path = _paths(out_dir, p)
            n_path.parent.mkdir(parents=True, exist_ok=True)
            b_path.parent.mkdir(parents=True, exist_ok=True)
            bursts.to_parquet(b_path, index=False)
            noise.to_parquet(n_path, index=False)  # written last: its presence marks the product done
            done += 1
            n_det = int(noise["rfi_detected"].sum()) if len(noise) else 0
            log(f"{p['name']}: {len(noise)} noise reports, {n_det} flagged")
            if progress_every and (done + failed) % progress_every == 0:
                log(rate())
    log("done " + rate())
    return done


def duplicated(df, kind="noise"):
    """True for reports repeated in the overlap of consecutive slices of one datatake.

    Adjacent GRD slices share a few noise sequences and bursts at their boundary, so counts of
    reports, flags or effort must skip these. The same report can differ by ~1 us in time
    between the two slices, so times are matched to the millisecond. The first occurrence (in
    row order) is kept as the original.
    """
    if df.empty:
        return pd.Series(False, index=df.index)
    t = "noise_sensing_time" if kind == "noise" else "azimuth_time"
    key = df.assign(
        _dt=df["product_name"].map(lambda n: "_".join(annotation.datatake_key(n))),
        _t=pd.to_datetime(df[t]).dt.round("ms"),
    )
    return key.duplicated(["_dt", "polarization", "swath", "_t"])


def dedupe(df, kind="noise"):
    """Drop reports repeated in the overlap of consecutive slices (see :func:`duplicated`)."""
    if df.empty:
        return df
    return df.loc[~duplicated(df, kind)].reset_index(drop=True)


def load(out_dir, kind="noise"):
    """Concatenate the per-product Parquet files of one kind ("noise" or "bursts")."""
    files = sorted((Path(out_dir) / kind).glob("*/*.parquet"))
    frames = [pd.read_parquet(f) for f in files]
    frames = [f for f in frames if len(f)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
