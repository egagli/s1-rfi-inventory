# Sentinel-1 RFI annotation inventory

A global, daily inventory of the radio-frequency-interference (RFI) annotations that ESA's
Sentinel-1 processor has written into every Level-1 product since 4 November 2021 (IPF 3.40).
It covers every IW GRDH and EW GRDM product in the Copernicus Data Space Ecosystem (CDSE). The
data are published as one Parquet file per day and kind; their size is estimated below.

For each noise-sequence report, the inventory records:
- its time and approximate ground position;
- ESA's detection flag and test statistics.

The sampling effort is kept too: reports without a detection are included, so rates can be
computed, not just counts.

## What is in the data

ESA's IPF runs an RFI detector on each product. Its output is in `annotation/rfi/*.xml`: per swath
and polarization, a report for each noise sequence at the start of a burst, with a detection flag
and test statistics, plus per-burst reports when mitigation was applied. This repository fetches
only those small XML files (~50 KB per product, never the imagery) and flattens them into tables.

**`noise_YYYY-MM-DD.parquet`**: one row per noise report per polarization.

| Column | Meaning |
|---|---|
| `swath`, `polarization` | e.g. `IW2`, `VV` |
| `noise_sensing_time` | UTC time of the noise sequence |
| `rfi_detected` | ESA's flag |
| `max_kl_divergence`, `max_fisher_z`, `max_rfi_psd` | ESA's test statistics |
| `latitude`, `longitude` | approximate position: the swath centre at that time, interpolated from the product footprint (±~40 km) |
| `rfi_mitigation_applied` | the product's mitigation setting (e.g. `TimeFrequency`) |
| `product_*` | product name, id, platform, orbit direction, relative orbit, start/end, processor (IPF) version, timeliness |
| `duplicate` | `True` for a report repeated in the overlap of two consecutive slices of one datatake. **Exclude these before counting.** |

**`bursts_YYYY-MM-DD.parquet`**: the per-burst reports (when present), with the same product
columns and `duplicate`.

**`data/daily_summary.csv`** (in git): for each day × platform × mode × polarization, the
catalogue products, harvested products, noise reports, flagged reports and products with
mitigation applied. This is the effort layer for rate analyses.

## Access

The day files are assets of monthly releases (`data-YYYY-MM`), e.g. with DuckDB:

```sql
SELECT product_platform, count(*) AS reports, sum(rfi_detected::int) AS flagged
FROM 'https://github.com/egagli/s1-rfi-inventory/releases/download/data-2024-01/noise_2024-01-10.parquet'
WHERE NOT duplicate
GROUP BY 1;
```

Or download a month with `gh release download data-2024-01 --repo egagli/s1-rfi-inventory`.

## How it is built

- `scripts/run_days.py` works through the days listed in a manifest (released as `manifest`):
  - catalogue search for the day;
  - when a slice was processed more than once, only the newest processing is kept;
  - it downloads each product's RFI annotation, then consolidates and writes the day files.
- Recent days are re-checked, because products reach the catalogue with a delay.
- `.github/workflows/inventory.yml` runs it every 6 hours for up to ~5 hours, then publishes.
- Requests are throttled (≤ 3 parallel downloads, ≥ 0.1 s apart, retries with back-off).
- It needs a free CDSE account, supplied as the repository secrets `CDSE_USERNAME` and
  `CDSE_PASSWORD`. Nothing else is used.

Local use:

```bash
pip install -r requirements.txt
export CDSE_USERNAME=... CDSE_PASSWORD=...
python scripts/run_days.py --manifest state/manifest.csv --out out --max-days 1
python scripts/harvest.py --help   # one area and period instead
python -m pytest -q                # offline tests
```

**Cost:** one global day (~600 products in 2024) takes ~2.5 min and ~1,200 requests, and gives
~3 MB of Parquet. The full archive since Nov 2021 is ~1,800 days: very roughly 75–110 h of
harvesting and a few GB of files.

## Things to know before using the flags

- **They are ESA's detections, not a complete record of interference.** The detector sees only the
  noise sequences at the start of each burst. Interference that arrives later in a burst, or whose
  statistics its tests are not sensitive to, can go unflagged. A flag says that ESA's test fired,
  not what the source was or whether the image was affected.
- **The sampling is uneven.** Coverage follows the Sentinel-1 observation scenario: dense over
  Europe and land, sparse over open ocean, at fixed local times (~06 and ~18 h). Normalise counts
  by the noise reports (effort), not by area or time alone.
- **The platforms differ.**
  - S1B stopped in Dec 2021; S1C data start in 2025 and S1D in 2026.
  - S1A reports one noise sequence at the start of every burst (every 2.76 s per swath).
  - S1C and S1D report every other burst (5.52 s), at different times within the burst cycle.
  - Keep platforms as separate strata.
- **Processor versions change.** Detection thresholds may differ between IPF versions, so the
  processor version is kept in `product_processor_version`.
- **Positions are approximate** (swath centre, ±~40 km). They are good enough for regional rates,
  not for locating a source.

## Attribution and licence

- **Code:** MIT (see `LICENSE`).
- **Data** (the release files and `data/daily_summary.csv`): [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  Please cite this repository.
- **Source data:** the tables are derived from ESA's Sentinel-1 annotation files. They contain
  modified Copernicus Sentinel data [year of the data], processed by ESA, obtained from the Copernicus
  Data Space Ecosystem (free, full and open under the Copernicus data policy).

The test fixtures are real Sentinel-1 annotation files from
[odhondt/eo_tools](https://github.com/odhondt/eo_tools) (MIT) and
[bopen/xarray-sentinel](https://github.com/bopen/xarray-sentinel) (Apache-2.0); see
`tests/fixtures/README.md`.
