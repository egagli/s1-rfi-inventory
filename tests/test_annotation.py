from pathlib import Path

import numpy as np
import pandas as pd

from s1rfi import annotation, cdse, inventory

FIX = Path(__file__).parent / "fixtures"
RFI_SLC = next(FIX.glob("rfi-s1a-iw1-slc-vv-*.xml"))
ANN_SLC = next(FIX.glob("trimmed-s1a-iw1-slc-vv-*.xml"))
ANN_GRD = next(FIX.glob("trimmed-s1b-iw-grd-vv-*.xml"))
# CDSE catalogue footprint of S1B_IW_GRDH_1SDV_20210401T052623_20210401T052648_026269_032297_ECC8
GRD_FOOTPRINT = (
    "MULTIPOLYGON (((12.040968 45.614502, 12.446052 47.11525, 9.086069 47.512238, "
    "8.772268 46.011879, 12.040968 45.614502)))"
)


def test_parse_rfi_slc():
    header, noise, bursts = annotation.parse_rfi(RFI_SLC.read_bytes())
    assert header["mission"] == "S1A" and header["swath"] == "IW1" and header["polarization"] == "VV"
    assert header["rfi_mitigation_applied"] == "None"
    assert len(noise) == 12 and len(bursts) == 11
    assert not noise["rfi_detected"].any()
    assert noise["noise_sensing_time"].is_monotonic_increasing
    assert np.isclose(noise["max_fisher_z"].iloc[0], 3.561126)
    assert np.isclose(bursts["in_out_band_power_ratio"].iloc[0], 7.454331)


def test_geolocation_slc_is_on_track():
    grid, bounds = annotation.parse_geolocation(ANN_SLC.read_bytes())
    assert bounds == {}
    _, noise, _ = annotation.parse_rfi(RFI_SLC.read_bytes())
    loc = annotation.locate_noise_reports(noise, grid)
    # Descending pass over Morocco: latitude decreases with time and stays near the grid.
    assert loc["latitude"].is_monotonic_decreasing
    assert loc["latitude"].between(29.5, 32.5).all() and loc["longitude"].between(-9.5, -7.0).all()


def test_grd_swath_bounds_and_order():
    grid, bounds = annotation.parse_geolocation(ANN_GRD.read_bytes())
    assert bounds == {"IW1": (0, 8681), "IW2": (8682, 17462), "IW3": (17463, 25787)}
    t = [grid["azimuth_time"].min()] * 3
    lons = [annotation.locate(t[:1], grid, 0.5 * sum(bounds[s]))[1][0] for s in ("IW1", "IW2", "IW3")]
    # Descending and right-looking: near range (IW1) is east of far range (IW3).
    assert lons[0] > lons[1] > lons[2]


def test_footprint_matches_annotation_grid():
    grid, bounds = annotation.parse_geolocation(ANN_GRD.read_bytes())
    noise = pd.DataFrame(
        {
            "swath": ["IW1", "IW2", "IW3", "IW1", "IW3"],
            "noise_sensing_time": pd.date_range("2021-04-01T05:26:21", "2021-04-01T05:26:50", periods=5),
        }
    )
    a = annotation.locate_noise_reports(noise, grid, bounds)
    b = annotation.locate_from_footprint(
        noise, GRD_FOOTPRINT, "2021-04-01T05:26:23.794457", "2021-04-01T05:26:48.793", "DESCENDING"
    )
    km = np.hypot(
        (a.latitude - b.latitude) * 111.0,
        (a.longitude - b.longitude) * 111.0 * np.cos(np.radians(a.latitude)),
    )
    assert (km < 3).all()


def test_footprint_corners_ascending():
    # Ascending: starts at the southern edge, near range to the west.
    wkt = "POLYGON((0 0, 2 0.3, 2.4 2, 0.4 1.7, 0 0))"
    sn, sf, en, ef = annotation.footprint_corners(wkt, "ASCENDING")
    assert sn == (0.0, 0.0) and sf == (2.0, 0.3) and en == (0.4, 1.7) and ef == (2.4, 2.0)


class _StubClient:
    """Serves fixture files for any path, records requests, and lists a fake SAFE tree."""

    def __init__(self, tree=None, missing=()):
        self.session, self.tree, self.missing = None, tree or {}, set(missing)
        self.downloads, self.listings = [], []

    def list_nodes(self, product_id, *path):
        self.listings.append(path)
        return [{"name": n, "size": 0, "children": 0} for n in self.tree[path]]

    def get_file(self, product_id, *path):
        self.downloads.append(path)
        if path[-1] in self.missing:
            raise cdse.NotFound("404")
        if path[-2] == "rfi":
            return RFI_SLC.read_bytes()
        return (ANN_SLC if "slc" in path[-1] else ANN_GRD).read_bytes()


def _product(safe, footprint=""):
    return pd.Series(
        {
            "id": "x",
            "name": safe,
            "platform": "A",
            "orbit_direction": "DESCENDING",
            "relative_orbit": 1,
            "start": pd.Timestamp("2023-09-16T06:37:30"),
            "end": pd.Timestamp("2023-09-16T06:37:57"),
            "footprint": footprint,
        }
    )


def test_process_product_slc_lists_folders():
    safe = "S1A_IW_SLC__1SDV_20230916T063730_20230916T063757_050349_060FCD_6814.SAFE"
    tree = {
        (safe,): ["annotation", "measurement"],
        (safe, "annotation"): ["rfi", ANN_SLC.name.removeprefix("trimmed-")],
        (safe, "annotation", "rfi"): [RFI_SLC.name],
    }
    client = _StubClient(tree)
    noise, bursts = inventory.process_product(client, _product(safe), geolocate="annotation")
    assert len(noise) == 12 and noise["latitude"].notna().all()
    assert bursts["latitude"].notna().all() and (bursts["geolocation"] == "annotation").all()
    assert (noise["product_name"] == safe).all() and (bursts["polarization"] == "VV").all()
    # The RFI file is paired with its own product annotation (same name minus "rfi-").
    assert (safe, "annotation", ANN_SLC.name.removeprefix("trimmed-")) in client.downloads


GRD_SAFE = "S1A_IW_GRDH_1SDV_20240110T004145_20240110T004204_052037_0649ED_D8D2.SAFE"
# Built names, fetched successfully for this product from CDSE in a 2024-01-10 global harvest
GRD_RFI = [
    "rfi-s1a-iw-grd-vh-20240110t004145-20240110t004204-052037-0649ed-002.xml",
    "rfi-s1a-iw-grd-vv-20240110t004145-20240110t004204-052037-0649ed-001.xml",
]


def test_rfi_file_names_match_cdse_listings():
    assert sorted(annotation.rfi_file_names(GRD_SAFE)) == GRD_RFI
    assert annotation.rfi_file_names(GRD_SAFE.removesuffix(".SAFE")) == annotation.rfi_file_names(GRD_SAFE)
    # Names listed by CDSE on 2026-09-24 for other polarization codes and modes
    assert annotation.rfi_file_names("S1A_IW_GRDH_1SSH_20240110T073827_20240110T073856_052041_064A10_2CFF.SAFE") == [
        "rfi-s1a-iw-grd-hh-20240110t073827-20240110t073856-052041-064a10-001.xml"
    ]
    assert annotation.rfi_file_names("S1A_EW_GRDM_1SDH_20240110T042827_20240110T042932_052039_064A01_DBDD.SAFE") == [
        "rfi-s1a-ew-grd-hh-20240110t042827-20240110t042932-052039-064a01-001.xml",
        "rfi-s1a-ew-grd-hv-20240110t042827-20240110t042932-052039-064a01-002.xml",
    ]
    assert annotation.rfi_file_names("S1A_IW_SLC__1SDV_20230916T063730_20230916T063757_050349_060FCD_6814.SAFE") is None


def test_process_product_grd_builds_names_and_pairs_polarizations():
    client = _StubClient()
    noise, _ = inventory.process_product(client, _product(GRD_SAFE), geolocate="annotation")
    assert client.listings == []  # no folder listing at all
    rfi = [p[-1] for p in client.downloads if p[-2] == "rfi"]
    ann = [p[-1] for p in client.downloads if p[-2] == "annotation"]
    assert sorted(rfi) == GRD_RFI
    # Each RFI file is paired with the product annotation of the same polarization.
    assert sorted(ann) == sorted(n.removeprefix("rfi-") for n in GRD_RFI)
    assert len(client.downloads) == 4 and noise["latitude"].notna().all()


def test_process_product_falls_back_to_listing_on_404():
    tree = {
        (GRD_SAFE,): ["annotation"],
        (GRD_SAFE, "annotation"): ["rfi", "calibration"],
        (GRD_SAFE, "annotation", "rfi"): ["rfi-actual-name.xml"],
    }
    client = _StubClient(tree, missing={GRD_RFI[1]})
    noise, _ = inventory.process_product(client, _product(GRD_SAFE, GRD_FOOTPRINT), geolocate="footprint")
    assert [p[-1] for p in client.downloads][-1] == "rfi-actual-name.xml"
    assert len(client.listings) == 3 and len(noise) == 12


def test_process_product_without_rfi_folder():
    tree = {(GRD_SAFE,): ["annotation"], (GRD_SAFE, "annotation"): ["calibration", "x.xml"]}
    client = _StubClient(tree, missing=set(GRD_RFI))
    noise, bursts = inventory.process_product(client, _product(GRD_SAFE))
    assert noise.empty and bursts.empty


# Real CDSE footprints (2024-01-10) that do not have exactly four vertices
PRIME_MERIDIAN = (  # S1A_IW_GRDH_1SDV_20240110T180506_..._D983, ascending: extra vertices at lon 0
    "POLYGON ((0.20226 16.113003, 2.51925 16.546045, 2.226629 18.053829, 0 17.64449376898351, "
    "-0.109754 17.624317, 0 17.09269740358445, 0.20226 16.113003))"
)
ANTIMERIDIAN = (  # S1A_IW_GRDH_1SDV_20240110T183208_..._F57D, descending, split at +/-180
    "MULTIPOLYGON (((-178.546616 63.928047, -177.546875 65.638084, -180 65.84582014201925, "
    "-180 64.05466750876944, -178.546616 63.928047)), ((180 64.05466750876944, 180 65.84582014201925, "
    "177.0271 66.097572, 176.358078 64.371956, 180 64.05466750876944)))"
)


def test_footprint_corners_prime_meridian():
    sn, sf, en, ef = annotation.footprint_corners(PRIME_MERIDIAN, "ASCENDING")
    assert sn == (0.20226, 16.113003) and sf == (2.51925, 16.546045)
    assert en == (-0.109754, 17.624317) and ef == (2.226629, 18.053829)


def test_footprint_corners_antimeridian():
    sn, sf, en, ef = annotation.footprint_corners(ANTIMERIDIAN, "DESCENDING")
    # Descending: starts at the northern edge, near range east of track; longitudes unwrapped.
    assert sn == (182.453125, 65.638084) and sf == (177.0271, 66.097572)
    assert en == (181.453384, 63.928047) and ef == (176.358078, 64.371956)
    noise = pd.DataFrame(
        {"swath": ["IW1", "IW2", "IW3"], "noise_sensing_time": pd.to_datetime(["2024-01-10T18:32:10"] * 3)}
    )
    loc = annotation.locate_from_footprint(
        noise, ANTIMERIDIAN, "2024-01-10T18:32:08", "2024-01-10T18:32:37", "DESCENDING"
    )
    assert loc["longitude"].between(-180, 180).all()
    # IW1 (near range, east of the line) is at negative longitude, IW3 west of it at positive.
    assert loc["longitude"].iloc[0] < -177 and loc["longitude"].iloc[2] > 177
    assert loc["latitude"].between(63.9, 66.1).all()


def test_dedupe_overlapping_slices():
    t = pd.to_datetime(["2024-01-10T02:26:57", "2024-01-10T02:27:00", "2024-01-10T02:27:03"])
    a = pd.DataFrame({"swath": "IW3", "polarization": "VH", "noise_sensing_time": t[:2],
                      "product_name": "S1A_IW_GRDH_1SDV_20240110T022632_20240110T022701_052038_0649F5_CC94.SAFE"})
    # The shared report is 1 us apart in the two slices, as seen in real data
    b = pd.DataFrame({"swath": "IW3", "polarization": "VH", "noise_sensing_time": t[1:] + pd.Timedelta("1us"),
                      "product_name": "S1A_IW_GRDH_1SDV_20240110T022701_20240110T022726_052038_0649F5_08C1.SAFE"})
    d = inventory.dedupe(pd.concat([a, b], ignore_index=True))
    assert len(d) == 3 and d["noise_sensing_time"].is_unique


def test_footprint_near_orbit_turning_latitude():
    # S1A_IW_GRDH_1SDH_20240110T083456_..._4138 (descending, 80-82.7 N, track running ~west):
    # the old "start = northern edge" rule put noise reports ~130-250 km off here.
    wkt = (
        "POLYGON ((-9.326939 80.108582, -3.224369 81.576279, -17.964613 82.700645, "
        "-22.163429 81.060524, -9.326939 80.108582))"
    )
    noise = pd.DataFrame(
        {
            "swath": ["IW1", "IW2", "IW3"],
            "noise_sensing_time": pd.to_datetime(
                ["2024-01-10T08:34:57.283424", "2024-01-10T08:35:15.456230", "2024-01-10T08:35:30.325660"]
            ),
        }
    )
    b = annotation.locate_from_footprint(
        noise, wkt, "2024-01-10T08:34:56.950259", "2024-01-10T08:35:25.983339", "DESCENDING"
    )
    # Positions from the product's annotation geolocation grid (annotation.locate_noise_reports)
    lat = np.array([81.791303, 81.236414, 80.700771])
    lon = np.array([-5.579469, -13.971894, -20.612304])
    km = np.hypot((b.latitude - lat) * 111.0, (b.longitude - lon) * 111.0 * np.cos(np.radians(lat)))
    assert (km < 6).all()


def test_ground_track_heading():
    assert -10 < annotation.ground_track_heading(0, "ASCENDING") < -7
    assert annotation.ground_track_heading(81.9, "ASCENDING") == -90
    assert 187 < annotation.ground_track_heading(0, "DESCENDING") < 190
