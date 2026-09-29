# Test fixtures

Real Sentinel-1 annotation files (Copernicus Sentinel data, free and open under the Copernicus
licence). The `trimmed-*` files keep only `adsHeader`, `geolocationGrid` and (GRD) `swathMerging`.

| File | Product | Obtained from |
|---|---|---|
| `rfi-s1a-iw1-slc-vv-20230916t063731-…-004.xml` | S1A_IW_SLC__1SDV_20230916T063730_…_6814 | [odhondt/eo_tools](https://github.com/odhondt/eo_tools) `data/S1/` (MIT) |
| `trimmed-s1a-iw1-slc-vv-20230916t063731-…-004.xml` | same | same |
| `trimmed-s1b-iw-grd-vv-20210401t052623-…-001.xml` | S1B_IW_GRDH_1SDV_20210401T052623_…_ECC8 (pre-IPF 3.40, no RFI file) | [bopen/xarray-sentinel](https://github.com/bopen/xarray-sentinel) `tests/data/` (Apache-2.0) |
