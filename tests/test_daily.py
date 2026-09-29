"""Offline tests of the day-by-day runner (no network: a fake catalogue and a stub client)."""

import datetime as dt

import pandas as pd

from s1rfi import daily
from test_annotation import GRD_FOOTPRINT, _StubClient

D = dt.date


def test_pick_days_pending_first_then_recent_rechecks():
    m = pd.DataFrame([dict(day=D(2024, 1, d), catalogue_products=1, harvested=1, failed=0, attempts=1, status="done",
                           last_run="") for d in (1, 2, 8)] +
                     [dict(day=D(2024, 1, 3), catalogue_products=2, harvested=1, failed=1, attempts=1, status="partial",
                           last_run="")])
    days = daily.pick_days(m, "2024-01-01", "2024-01-10", recheck_days=3, today="2024-01-10")
    assert days[:6] == [D(2024, 1, d) for d in (3, 4, 5, 6, 7, 9)]  # pending, oldest first
    assert days[6:] == [D(2024, 1, 8)]  # a done day within the recheck window


def test_newest_processing_keeps_latest_of_the_same_slice():
    base = "S1A_EW_GRDM_1SDH_20240110T051741_20240110T051845_052040_064A03"
    cat = pd.DataFrame({"name": [f"{base}_4EE3.SAFE", f"{base}_778B.SAFE", "S1A_IW_GRDH_1SDV_x_y_1_2_AAAA.SAFE"],
                        "start": pd.to_datetime(["2024-01-10T05:17:41"] * 2 + ["2024-01-10T06:00:00"]),
                        "processing_date": pd.to_datetime(["2024-01-10", "2024-03-01", "2024-01-10"])})
    out = daily.newest_processing(cat)
    assert sorted(out.name) == [f"{base}_778B.SAFE", "S1A_IW_GRDH_1SDV_x_y_1_2_AAAA.SAFE"]
    # without a processing date (older products), the publication date decides
    old = cat.assign(processing_date=pd.NaT, publication_date=pd.to_datetime(["2024-05-01", "2024-01-11", "2024-01-11"]))
    assert f"{base}_4EE3.SAFE" in set(daily.newest_processing(old).name)


def _fake_search(names):
    def search(start, end, product_type="IW_GRDH_1S", **kw):
        if product_type != "IW_GRDH_1S":
            return pd.DataFrame()
        return pd.DataFrame({"id": "x", "name": names, "footprint": GRD_FOOTPRINT, "platform": "A",
                             "orbit_direction": "DESCENDING", "relative_orbit": 1,
                             "start": pd.Timestamp("2023-09-16T06:37:30"), "end": pd.Timestamp("2023-09-16T06:37:57"),
                             "processing_date": pd.Timestamp("2023-09-16T12:00"), "processor_version": "003.61",
                             "timeliness": "Fast-24h"})
    return search


def test_run_writes_day_files_manifest_and_summary_and_skips_unchanged_days(tmp_path):
    names = ["S1A_IW_GRDH_1SDV_20230916T063730_20230916T063755_050349_060FCD_AAAA.SAFE"]
    kw = dict(manifest_path=tmp_path / "manifest.csv", out_dir=tmp_path / "out", work_root=tmp_path / "work",
              summary_path=tmp_path / "summary.csv", start=D(2023, 9, 16), end=D(2023, 9, 17), recheck_days=7,
              log=lambda *_: None, search=_fake_search(names))
    written = daily.run(_StubClient(), **kw)
    assert [p.name for p in written] == ["noise_2023-09-16.parquet", "bursts_2023-09-16.parquet", "cells_2023-09-16.parquet"]
    noise = pd.read_parquet(written[0])
    assert len(noise) and "duplicate" in noise and noise["product_processor_version"].eq("003.61").all()
    c = pd.read_parquet(written[2])
    assert c["reports"].sum() == (~noise["duplicate"]).sum() and c["flagged"].sum() == noise.loc[~noise["duplicate"], "rfi_detected"].sum()
    m = daily.read_manifest(kw["manifest_path"])
    assert m.iloc[0][["catalogue_products", "harvested", "failed", "status"]].tolist() == [1, 1, 0, "done"]
    s = pd.read_csv(kw["summary_path"])
    assert s["noise_reports"].sum() == len(noise[~noise["duplicate"]]) and s["catalogue_products"].max() == 1
    assert not (tmp_path / "work" / "2023-09-16").exists()  # resume cache removed once the day is done
    # Same catalogue on a recheck: nothing redone
    assert daily.run(_StubClient(), **{**kw, "search": _fake_search(names)}) == []


def test_refused_login_stops_the_run_before_any_day_is_recorded(tmp_path):
    from s1rfi import cdse

    class Refused(_StubClient):
        def _auth(self, force=False):
            raise cdse.AuthError("refused")

    names = ["S1A_IW_GRDH_1SDV_20230916T063730_20230916T063755_050349_060FCD_AAAA.SAFE"]
    try:
        daily.run(Refused(), tmp_path / "manifest.csv", tmp_path / "out", tmp_path / "work", tmp_path / "summary.csv",
                  start=D(2023, 9, 16), end=D(2023, 9, 17), log=lambda *_: None, search=_fake_search(names))
        raise AssertionError("expected AuthError")
    except cdse.AuthError:
        pass
    assert not (tmp_path / "manifest.csv").exists() and not (tmp_path / "out").exists()


def test_a_day_with_nothing_harvested_is_not_recorded(tmp_path):
    class AllFail(_StubClient):
        def get_file(self, product_id, *path):
            raise ConnectionError("down")

        def list_nodes(self, product_id, *path):
            raise ConnectionError("down")

    names = ["S1A_IW_GRDH_1SDV_20230916T063730_20230916T063755_050349_060FCD_AAAA.SAFE"]
    try:
        daily.run(AllFail(), tmp_path / "manifest.csv", tmp_path / "out", tmp_path / "work", tmp_path / "summary.csv",
                  start=D(2023, 9, 16), end=D(2023, 9, 17), log=lambda *_: None, search=_fake_search(names))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "none of 1 products" in str(e)
    assert not (tmp_path / "manifest.csv").exists()
