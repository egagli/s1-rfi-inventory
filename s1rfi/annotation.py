"""Parse Sentinel-1 Level-1 RFI annotations (``annotation/rfi/rfi-*.xml``, IPF >= 3.40,
2021-11-04 onward) and place each noise-based detection on the ground.

Each RFI file holds, per swath:

- ``rfiDetectionFromNoiseReport``: one per noise (rank-echo) sequence, roughly every 2.76 s,
  with ``rfiDetected``, ``maxKLDivergence``, ``maxFisherZ`` and ``maxRfiPsd``. These are the
  Monti-Guarnieri et al. (2017) statistics computed operationally by ESA's processor.
- ``rfiBurstReport`` (IW/EW): one per burst, with ``inBandOutBandPowerRatio`` and optional
  time- and frequency-domain summaries of the lines or bandwidth affected.

Noise reports carry a time but no position. :func:`locate` interpolates the product
annotation's geolocation grid at that time and at the middle of the swath, which is
appropriate for a ~25 km x 70 km receive footprint, not for pinpointing an emitter.
"""

import re
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd


def _root(xml):
    return ET.fromstring(xml) if isinstance(xml, (bytes, str)) else ET.parse(xml).getroot()


def _text(el, path, cast=str, default=None):
    node = el.find(path)
    return default if node is None or node.text is None else cast(node.text)


def _float(s):
    return float(s)


def _bool(s):
    return s.strip().lower() == "true"


def parse_rfi(xml):
    """Parse one RFI annotation file.

    Returns ``(header, noise_reports, burst_reports)``: a dict and two DataFrames.
    """
    root = _root(xml)
    h = root.find("adsHeader")
    header = {
        "mission": _text(h, "missionId"),
        "product_type": _text(h, "productType"),
        "polarization": _text(h, "polarisation"),
        "mode": _text(h, "mode"),
        "swath": _text(h, "swath"),
        "start_time": pd.Timestamp(_text(h, "startTime")),
        "stop_time": pd.Timestamp(_text(h, "stopTime")),
        "absolute_orbit": _text(h, "absoluteOrbitNumber", int),
        "datatake_id": _text(h, "missionDataTakeId", int),
        "rfi_mitigation_applied": _text(root, "rfiMitigationApplied"),
    }

    noise = pd.DataFrame(
        [
            {
                "swath": _text(r, "swath"),
                "noise_sensing_time": pd.Timestamp(_text(r, "noiseSensingTime")),
                "rfi_detected": _text(r, "rfiDetected", _bool),
                "max_kl_divergence": _text(r, "maxKLDivergence", _float),
                "max_fisher_z": _text(r, "maxFisherZ", _float),
                "max_rfi_psd": _text(r, "maxRfiPsd", _float),
            }
            for r in root.iter("rfiDetectionFromNoiseReport")
        ],
        columns=["swath", "noise_sensing_time", "rfi_detected", "max_kl_divergence", "max_fisher_z", "max_rfi_psd"],
    )

    bursts = []
    for r in root.iter("rfiBurstReport"):
        row = {
            "swath": _text(r, "swath"),
            "azimuth_time": pd.Timestamp(_text(r, "azimuthTime")),
            "in_out_band_power_ratio": _text(r, "inBandOutBandPowerRatio", _float),
        }
        td = r.find("timeDomainRfiReport")
        if td is not None:
            row["td_pct_affected_lines"] = _text(td, "percentageAffectedLines", _float)
            row["td_avg_pct_affected_samples"] = _text(td, "avgPercentageAffectedSamples", _float)
            row["td_max_pct_affected_samples"] = _text(td, "maxPercentageAffectedSamples", _float)
        fd = r.find("frequencyDomainRfiBurstReport")
        if fd is not None:
            row["fd_pct_affected_lines"] = _text(fd, "isolatedRfiReport/percentageAffectedLines", _float)
            row["fd_max_pct_affected_bw"] = _text(fd, "isolatedRfiReport/maxPercentageAffectedBW", _float)
            row["fd_pct_blocks_persistent"] = _text(fd, "percentageBlocksPersistentRfi", _float)
            row["fd_max_pct_bw_persistent"] = _text(fd, "maxPercentageBWAffectedPersistentRfi", _float)
        bursts.append(row)
    burst_reports = pd.DataFrame(bursts)
    if burst_reports.empty:
        burst_reports = pd.DataFrame(columns=["swath", "azimuth_time", "in_out_band_power_ratio"])
    return header, noise, burst_reports


def parse_geolocation(xml):
    """Parse a product annotation's geolocation grid and (GRD only) swath range bounds.

    Returns ``(grid, swath_bounds)`` where ``grid`` has columns azimuth_time, line, pixel,
    latitude, longitude and ``swath_bounds`` maps swath -> (first_sample, last_sample), or is
    empty for single-swath (SLC) annotations.
    """
    root = _root(xml)
    grid = pd.DataFrame(
        [
            {
                "azimuth_time": pd.Timestamp(_text(p, "azimuthTime")),
                "line": _text(p, "line", int),
                "pixel": _text(p, "pixel", int),
                "latitude": _text(p, "latitude", _float),
                "longitude": _text(p, "longitude", _float),
            }
            for p in root.iter("geolocationGridPoint")
        ]
    )
    bounds = {}
    for m in root.iter("swathMerge"):
        b = m.find("swathBoundsList/swathBounds")
        bounds[_text(m, "swath")] = (_text(b, "firstRangeSample", int), _text(b, "lastRangeSample", int))
    return grid, bounds


def _interp_extrap(x, xp, fp):
    """1-D linear interpolation that extrapolates linearly beyond the ends."""
    x, xp, fp = np.asarray(x, float), np.asarray(xp, float), np.asarray(fp, float)
    y = np.interp(x, xp, fp)
    lo, hi = x < xp[0], x > xp[-1]
    y[lo] = fp[0] + (x[lo] - xp[0]) * (fp[1] - fp[0]) / (xp[1] - xp[0])
    y[hi] = fp[-1] + (x[hi] - xp[-1]) * (fp[-1] - fp[-2]) / (xp[-1] - xp[-2])
    return y


def locate(times, grid, pixel):
    """Latitude/longitude at the given azimuth ``times`` and range ``pixel`` of a grid.

    The grid is interpolated in range at ``pixel`` for each grid row, then in time between
    rows (extrapolating linearly, since noise sequences can precede the product start).
    Longitudes are unwrapped before interpolation and wrapped back to [-180, 180).
    """
    t0 = grid["azimuth_time"].min()
    rows = []
    for _, g in grid.groupby("line", sort=True):
        g = g.sort_values("pixel")
        lon = np.degrees(np.unwrap(np.radians(g["longitude"].to_numpy())))
        rows.append(
            (
                (g["azimuth_time"].mean() - t0).total_seconds(),
                np.interp(pixel, g["pixel"], g["latitude"]),
                np.interp(pixel, g["pixel"], lon),
            )
        )
    tr, lat_r, lon_r = map(np.array, zip(*rows))
    lon_r = np.degrees(np.unwrap(np.radians(lon_r)))
    x = (pd.to_datetime(pd.Series(times)) - t0).dt.total_seconds().to_numpy()
    lat = _interp_extrap(x, tr, lat_r)
    lon = (_interp_extrap(x, tr, lon_r) + 180.0) % 360.0 - 180.0
    return lat, lon


def locate_noise_reports(noise, grid, swath_bounds=None, time_col="noise_sensing_time"):
    """Add latitude/longitude columns to ``noise`` using a product geolocation grid.

    For GRD, ``swath_bounds`` (from :func:`parse_geolocation`) gives each swath's range
    sample span, and the swath centre is used. For SLC (one annotation per swath) pass
    ``None`` and the grid's range centre is used. Pass ``time_col="azimuth_time"`` to place
    burst reports (at the burst's annotated azimuth time) instead of noise reports.
    """
    out = noise.copy()
    out["latitude"] = np.nan
    out["longitude"] = np.nan
    for swath, idx in out.groupby("swath").groups.items():
        if swath_bounds:
            if swath not in swath_bounds:
                continue
            first, last = swath_bounds[swath]
            pixel = 0.5 * (first + last)
        else:
            pixel = 0.5 * (grid["pixel"].min() + grid["pixel"].max())
        lat, lon = locate(out.loc[idx, time_col], grid, pixel)
        out.loc[idx, "latitude"] = lat
        out.loc[idx, "longitude"] = lon
    return out


# Centre of each subswath as a fraction of GRD ground-range width, near range = 0, from the
# swathMerging bounds of GRD product annotations (IW: e.g. IW1 0-8681, IW2 8682-17462, IW3
# 17463-25787 of 25788 samples; EW: four EW GRDM SDH/SDV products of 2024-01-10, which agree
# to +/-0.005).
SWATH_CENTRE_FRACTION = {
    "IW1": 0.168,
    "IW2": 0.508,
    "IW3": 0.839,
    "EW1": 0.145,
    "EW2": 0.381,
    "EW3": 0.571,
    "EW4": 0.760,
    "EW5": 0.925,
}

# Sentinel-1 orbit inclination (sun-synchronous, retrograde)
INCLINATION_DEG = 98.18


def _footprint_points(wkt):
    """Distinct (lon, lat) vertices of a POLYGON/MULTIPOLYGON WKT, longitudes unwrapped.

    Footprints that cross the antimeridian come as a MULTIPOLYGON split at +/-180; shifting
    negative longitudes by 360 when the vertices span more than 180 degrees rejoins them.
    """
    nums = [float(v) for v in wkt.replace("(", " ").replace(")", " ").replace(",", " ").split() if _is_number(v)]
    pts = np.array(nums, dtype=float).reshape(-1, 2)
    if len(pts) and np.ptp(pts[:, 0]) > 180:
        pts[pts[:, 0] < 0, 0] += 360.0
    return list(dict.fromkeys(map(tuple, np.round(pts, 9))))


def _convex_hull(pts):
    """Andrew's monotone chain; returns hull vertices counter-clockwise, no repeats."""
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _four_corners(pts):
    """Reduce a convex polygon to its four sharpest corners.

    Catalogue footprints are quadrilaterals, but CDSE inserts extra vertices where an edge
    crosses the prime meridian or the antimeridian. Those lie on an edge (turn angle ~0), so
    repeatedly dropping the vertex with the smallest turn recovers the true corners.
    """
    hull = _convex_hull(pts)
    if len(hull) < 4:
        return None
    while len(hull) > 4:
        turns = []
        for i in range(len(hull)):
            a, b, c = np.array(hull[i - 1]), np.array(hull[i]), np.array(hull[(i + 1) % len(hull)])
            u, v = b - a, c - b
            cosang = np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v))
            turns.append(np.arccos(np.clip(cosang, -1, 1)))
        hull.pop(int(np.argmin(turns)))
    return hull


def ground_track_heading(lat, orbit_direction):
    """Approximate ground-track heading (degrees clockwise from north) at a latitude.

    From spherical geometry for a circular orbit, sin(heading) = cos(i) / cos(lat); Earth
    rotation adds a few degrees westward, which does not matter for choosing footprint corners.
    Ascending tracks head north-north-west at the equator and due west at the turning
    latitude (~81.8 deg); descending tracks head south-south-west.
    """
    x = np.clip(np.cos(np.radians(INCLINATION_DEG)) / np.cos(np.radians(lat)), -1, 1)
    h = np.degrees(np.arcsin(x))
    return h if orbit_direction.upper().startswith("A") else 180.0 - h


def footprint_corners(wkt, orbit_direction):
    """Return the (start_near, start_far, end_near, end_far) lon/lat corners of a slice footprint.

    Sentinel-1 is right-looking, so going counter-clockwise round the footprint the corners
    are always start-near, start-far, end-far, end-near. Which corner is start-near is chosen
    as the rotation whose along-track edges best match the orbit heading at the footprint's
    latitude (:func:`ground_track_heading`), which also works near the orbit's turning
    latitude where the track runs east-west. Footprints with extra vertices (prime-meridian or
    antimeridian crossings) are first reduced to four corners. Longitudes are unwrapped, so
    they can exceed 180 for antimeridian footprints. Returns ``None`` if four corners cannot
    be found.
    """
    hull = _four_corners(_footprint_points(wkt))  # counter-clockwise in lon/lat
    if hull is None:
        return None
    pts = np.array(hull)
    lat0 = pts[:, 1].mean()
    xy = np.column_stack([pts[:, 0] * np.cos(np.radians(lat0)), pts[:, 1]])  # local, ~conformal
    h = np.radians(ground_track_heading(lat0, orbit_direction))
    heading = np.array([np.sin(h), np.cos(h)])
    best, best_score = None, -np.inf
    for k in range(4):
        sn, sf, ef, en = (xy[(k + i) % 4] for i in range(4))
        along = (en - sn) + (ef - sf)
        score = along @ heading / np.linalg.norm(along)
        if score > best_score:
            best, best_score = k, score
    sn, sf, ef, en = (tuple(hull[(best + i) % 4]) for i in range(4))
    return sn, sf, en, ef


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def _unit_vector(lon, lat):
    lon, lat = np.radians(lon), np.radians(lat)
    return np.array([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])


def locate_from_footprint(noise, footprint_wkt, start, end, orbit_direction, time_col="noise_sensing_time"):
    """Approximate latitude/longitude for noise reports from the catalogue footprint alone.

    Bilinear interpolation between the footprint corners (on 3-D unit vectors, so it holds
    near the poles): along track by time within [start, end] (extrapolating beyond), across
    track at the swath centre. Agrees with
    :func:`locate_noise_reports` to ~1 km on a test GRD product, well inside the ~25 x 70 km
    receive footprint. Works across the antimeridian; longitudes are returned in [-180, 180).
    """
    out = noise.copy()
    out["latitude"] = np.nan
    out["longitude"] = np.nan
    corners = footprint_corners(footprint_wkt, orbit_direction)
    if corners is None:
        return out
    sn, sf, en, ef = (_unit_vector(*p) for p in corners)
    start, end = pd.Timestamp(start).tz_localize(None), pd.Timestamp(end).tz_localize(None)
    t = pd.to_datetime(out[time_col]).dt.tz_localize(None)
    a = ((t - start).dt.total_seconds() / (end - start).total_seconds()).to_numpy()[:, None]
    c = out["swath"].map(SWATH_CENTRE_FRACTION).to_numpy(dtype=float)[:, None]
    # Bilinear in 3-D unit vectors rather than in degrees, so it holds near the poles.
    v = (1 - a) * ((1 - c) * sn + c * sf) + a * ((1 - c) * en + c * ef)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    out["latitude"] = np.degrees(np.arcsin(np.clip(v[:, 2], -1, 1)))
    out["longitude"] = np.degrees(np.arctan2(v[:, 1], v[:, 0]))
    return out


# GRD product-name polarization code -> (polarization, file index) of each measurement
_GRD_POLS = {
    "SV": [("vv", 1)],
    "SH": [("hh", 1)],
    "DV": [("vv", 1), ("vh", 2)],
    "DH": [("hh", 1), ("hv", 2)],
}
_SAFE = re.compile(
    r"^(?P<mission>S1[A-D])_(?P<mode>IW|EW|S[1-6])_GRD[HM]_1S(?P<pol>[SD][VH])_"
    r"(?P<start>\d{8}T\d{6})_(?P<stop>\d{8}T\d{6})_(?P<orbit>\d{6})_(?P<dt>[0-9A-F]{6})_[0-9A-F]{4}(\.SAFE)?$"
)


def rfi_file_names(safe_name):
    """RFI annotation file names of a GRD product, built from its name without listing folders.

    Checked against CDSE listings for IW/EW GRD in SDV, SDH, SSH and SSV: co-polarization is
    file 001 and cross-polarization 002, and the file times equal the product times. The
    product annotation is the same name without the ``rfi-`` prefix. Returns ``None`` for
    products this does not cover (e.g. SLC, whose per-swath files carry swath times).
    """
    m = _SAFE.match(safe_name)
    if not m:
        return None
    g = m.groupdict()
    stem = f"{g['mission']}-{g['mode']}-grd".lower()
    tail = f"{g['start']}-{g['stop']}-{g['orbit']}-{g['dt']}".lower()
    return [f"rfi-{stem}-{pol}-{tail}-{i:03d}.xml" for pol, i in _GRD_POLS[g["pol"]]]


def datatake_key(product_name):
    """(mission, datatake id) of a product name; slices of one datatake share it."""
    parts = product_name.split("_")
    return parts[0], parts[-2]
