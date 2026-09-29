"""Figures for the README: where Sentinel-1 listened, where ESA's RFI detector fired, and how that
changed month by month.

Inputs:
- ``data/daily_summary.csv`` (in git) for the monthly series;
- the per-day cell files (``cells_YYYY-MM-DD.parquet``) for the maps. They are downloaded from the
  repository's releases into ``--cache``. For days published before cell files existed, the day's
  noise file is downloaded instead and aggregated.

Outputs, in ``--out``:
- ``coverage_map.png``: noise reports per 1-degree cell (the sampling effort);
- ``flag_rate_map.png``: flagged reports / noise reports per cell;
- ``strong_rate_map.png``: strong reports (flagged, ``max_rfi_psd`` >= 250) per 10,000 noise reports;
- ``monthly_series.png``: monthly flag rate and monthly noise reports per platform;
- ``cells_total.csv``: the per-cell table behind the maps.

    python scripts/figures.py --out figures --cache .cache/cells
"""

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from s1rfi import daily  # noqa: E402

REPO = "egagli/s1-rfi-inventory"
MIN_REPORTS = 300  # a cell needs this many noise reports (whole period) before its rates are drawn
MITIGATION_ON = pd.Timestamp("2022-03-23")  # ESA's RFI mitigation switched on (IPF 3.51)

# Reference palette (dataviz skill): light surface, text tokens, sequential blue, categorical slots
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e4e3df"
BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
PLATFORM = {"S1A": "#2a78d6", "S1B": "#eb6834", "S1C": "#1baf7a", "S1D": "#eda100"}  # fixed slots 1-4
plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
                     "text.color": INK, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                     "axes.edgecolor": MUTED, "font.size": 9})


def fetch_cells(days, cache, repo=REPO, session=None):
    """Cell tables for ``days``, cached in ``cache`` (downloaded or rebuilt from the noise file)."""
    session = session or requests.Session()
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    frames = []
    for d in days:
        d = pd.Timestamp(d)
        f = cache / f"cells_{d:%Y-%m-%d}.parquet"
        if not f.exists():
            base = f"https://github.com/{repo}/releases/download/data-{d:%Y-%m}"
            r = session.get(f"{base}/cells_{d:%Y-%m-%d}.parquet", timeout=120)
            if r.status_code == 200:
                f.write_bytes(r.content)
            else:  # published before cell files existed: aggregate the noise file
                r = session.get(f"{base}/noise_{d:%Y-%m-%d}.parquet", timeout=300)
                if r.status_code != 200:
                    print(f"missing {d:%Y-%m-%d}: {r.status_code}")
                    continue
                tmp = cache / "_noise.parquet"
                tmp.write_bytes(r.content)
                daily.cells(d.date(), pd.read_parquet(tmp)).to_parquet(f, index=False)
                tmp.unlink()
        frames.append(pd.read_parquet(f))
    frames = [x for x in frames if len(x)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _world(ax):
    """Coastlines if cartopy's Natural Earth data are reachable; a plain lon/lat frame otherwise."""
    try:
        import cartopy.feature as cfeature

        ax.add_feature(cfeature.COASTLINE.with_scale("110m"), linewidth=0.4, edgecolor=MUTED)
    except Exception as e:  # noqa: BLE001 - figures must still be produced
        print(f"no coastlines ({e})")


def _map(grid, value, title, label, norm, path, note, zero_gray=False):
    try:
        import cartopy.crs as ccrs

        fig = plt.figure(figsize=(11, 5.6))
        ax = fig.add_subplot(1, 1, 1, projection=ccrs.Robinson())
        ax.set_global()
        kw = dict(transform=ccrs.PlateCarree())
        _world(ax)
        ax.spines["geo"].set_edgecolor(GRID)
    except ImportError:
        fig, ax = plt.subplots(figsize=(11, 5.6))
        ax.set_xlim(-180, 180), ax.set_ylim(-90, 90)
        kw = {}
    lats, lons = np.arange(-90, 91), np.arange(-180, 181)
    z = np.full((180, 360), np.nan)
    z[grid["cell_lat"].to_numpy() + 90, grid["cell_lon"].to_numpy() + 180] = grid[value].to_numpy()
    cmap = mcolors.LinearSegmentedColormap.from_list("blues", BLUES)
    if zero_gray:  # sampled cells with none: neutral gray, outside the ramp
        zero = np.where(z == 0, 1.0, np.nan)
        ax.pcolormesh(lons, lats, np.ma.masked_invalid(zero), cmap=mcolors.ListedColormap([GRID]), shading="flat", **kw)
        z = np.where(z == 0, np.nan, z)
    m = ax.pcolormesh(lons, lats, np.ma.masked_invalid(z), cmap=cmap, norm=norm, shading="flat", **kw)
    cb = fig.colorbar(m, ax=ax, orientation="horizontal", fraction=0.04, pad=0.04, aspect=45)
    cb.set_label(label, color=INK2)
    cb.outline.set_edgecolor(GRID)
    ax.set_title(title, loc="left", fontsize=11, color=INK)
    fig.text(0.01, 0.01, note, fontsize=7.5, color=INK2, ha="left", va="bottom")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def maps(cells, out, min_reports=MIN_REPORTS):
    t = cells.groupby(["cell_lat", "cell_lon"])[["reports", "flagged", "strong"]].sum().reset_index()
    t["flag_rate_pct"] = 100 * t.flagged / t.reports
    t["strong_per_10k"] = 1e4 * t.strong / t.reports
    t.to_csv(Path(out) / "cells_total.csv", index=False, float_format="%.4g")
    span = f"{cells.day.min():%d %b %Y} – {cells.day.max():%d %b %Y}"
    n_days = cells.day.nunique()
    base = f"Sentinel-1 IW/EW GRD, {span} ({n_days} days, all platforms and polarizations). 1° cells."
    _map(t, "reports", "Where Sentinel-1 listened: ESA noise reports per 1° cell",
         "noise reports (log scale)", mcolors.LogNorm(1, max(t.reports.max(), 10)), Path(out) / "coverage_map.png",
         base + " Blank: no reports.")
    ok = t[t.reports >= min_reports]
    _map(ok, "flag_rate_pct", "Where ESA's RFI detector fired: share of noise reports flagged",
         "flagged reports, % (log scale)", mcolors.LogNorm(0.01, 100, clip=True), Path(out) / "flag_rate_map.png",
         base + f" Cells with ≥ {min_reports} reports; gray: sampled, never flagged. A flag means ESA's test fired, "
         "not that a source was identified.", zero_gray=True)
    _map(ok, "strong_per_10k", f"Strong detections: flagged with max_rfi_psd ≥ {daily.STRONG_PSD:g}, per 10,000 noise reports",
         "strong reports per 10,000 noise reports (log scale)", mcolors.LogNorm(0.1, 1e4, clip=True),
         Path(out) / "strong_rate_map.png", base + f" Cells with ≥ {min_reports} reports; gray: sampled, no strong reports.",
         zero_gray=True)


def _end_labels(ax, ends, min_gap_pt=11):
    """Direct labels at the line ends, nudged apart vertically so they never overlap."""
    if not ends:
        return
    fig = ax.figure
    fig.canvas.draw()
    to_disp, to_data = ax.transData.transform, ax.transData.inverted().transform
    pts = sorted(((to_disp((mdates.date2num(x), y))[1], y, x, lab) for y, x, lab in ends))
    gap = min_gap_pt * fig.dpi / 72
    ys = []
    for yd, _, _, _ in pts:
        ys.append(max(yd, ys[-1] + gap) if ys else yd)
    for (yd, y, x, lab), yn in zip(pts, ys):
        xd = to_disp((mdates.date2num(x), y))[0]
        ax.annotate(lab, (x, y), xytext=to_data((xd + 8, yn)), textcoords="data", va="center", fontsize=8.5,
                    color=INK, annotation_clip=False)  # text ink, beside the colored mark


def monthly(summary, out):
    s = summary.assign(month=pd.to_datetime(summary["day"]).dt.to_period("M").dt.to_timestamp())
    m = s.groupby(["month", "platform"])[["noise_reports", "flagged"]].sum().reset_index()
    m["rate"] = 100 * m.flagged / m.noise_reports.where(m.noise_reports > 0)
    m.to_csv(Path(out) / "monthly_series.csv", index=False, float_format="%.4g")
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True, gridspec_kw=dict(height_ratios=[1.3, 1]))
    ends = {}
    for plat, g in m.groupby("platform"):
        g = g.sort_values("month")
        c = PLATFORM.get(plat, INK2)
        for ax, col in ((a1, "rate"), (a2, "noise_reports")):
            ax.plot(g.month, g[col], color=c, linewidth=2, marker="o" if len(g) < 3 else None, markersize=8,
                    markeredgecolor=SURFACE, markeredgewidth=2)
            last = g.dropna(subset=[col]).iloc[-1]
            ends.setdefault(ax, []).append((last[col], last.month, plat))
    for ax in (a1, a2):
        ax.set_ylim(bottom=0)
        _end_labels(ax, ends.get(ax, []))
    pad = pd.Timedelta(days=20)
    a2.set_xlim(m.month.min() - pad, m.month.max() + pd.Timedelta(days=45))
    a1.set_ylabel("flagged noise reports, %")
    a2.set_ylabel("noise reports per month")
    a1.set_title("ESA RFI flag rate by platform, per month", loc="left", fontsize=11)
    a2.set_title("Sampling effort: noise reports per month", loc="left", fontsize=10)
    for ax in (a1, a2):
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.set_ylim(bottom=0)
        if m.month.min() <= MITIGATION_ON <= m.month.max() + pd.offsets.MonthEnd(1):
            ax.axvline(MITIGATION_ON, color=MUTED, linewidth=1, linestyle=(0, (3, 3)))
    if m.month.min() <= MITIGATION_ON <= m.month.max() + pd.offsets.MonthEnd(1):
        a1.annotate("ESA RFI mitigation on", (MITIGATION_ON, a1.get_ylim()[1]), xytext=(4, -10),
                    textcoords="offset points", fontsize=8, color=INK2)
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    handles = [plt.Line2D([], [], color=PLATFORM[p], linewidth=2, label=p) for p in PLATFORM if p in set(m.platform)]
    a1.legend(handles=handles, frameon=False, ncol=4, loc="upper left", fontsize=8.5)
    fig.text(0.01, 0.005, "IW + EW GRD, all polarizations. S1C/S1D report every other burst, so their rates are not "
             "directly comparable with S1A/S1B.", fontsize=7.5, color=INK2)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(Path(out) / "monthly_series.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", default="data/daily_summary.csv")
    ap.add_argument("--cache", default=".cache/cells")
    ap.add_argument("--out", default="figures")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--min-reports", type=int, default=MIN_REPORTS)
    args = ap.parse_args()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    summary = pd.read_csv(args.summary)
    monthly(summary, args.out)
    days = sorted(summary.loc[summary.catalogue_products > 0, "day"].unique())
    cells = fetch_cells(days, args.cache, args.repo)
    if len(cells):
        maps(cells, args.out, args.min_reports)
    print(f"figures for {len(days)} days written to {args.out}")


if __name__ == "__main__":
    main()
