"""Copernicus Data Space Ecosystem (CDSE) access for Sentinel-1 RFI annotations.

Catalogue search and listing a product's internal files ("Nodes") work anonymously.
Downloading a single file needs a free CDSE account: set ``CDSE_USERNAME`` and
``CDSE_PASSWORD`` (or pass them to :class:`Client`). Only the small XML files are fetched
(~50 KB for a GRD RFI annotation, ~2 MB for a GRD product annotation), never the imagery.
"""

import os
import threading
import time

import pandas as pd
import requests

CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1"
DOWNLOAD = "https://download.dataspace.copernicus.eu/odata/v1"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

# The first IPF version to write annotation/rfi/*.xml
RFI_ANNOTATION_START = pd.Timestamp("2021-11-04", tz="UTC")


def _attr_filter(name, value):
    return (
        f"Attributes/OData.CSC.StringAttribute/any(att:att/Name eq '{name}' "
        f"and att/OData.CSC.StringAttribute/Value eq '{value}')"
    )


def search(start, end, wkt=None, product_type="IW_GRDH_1S", page_size=1000, session=None):
    """Search Sentinel-1 products whose sensing start falls in [start, end).

    Returns a DataFrame with id, name, start, end, footprint (WKT), platform,
    orbit_direction, relative_orbit, and the processing metadata processing_date,
    processor_version (IPF), timeliness, datatake_id and slice_number. The CDSE "_COG"
    duplicates of GRD products are dropped. Long windows are fine: results are paged by time.
    """
    session = session or requests.Session()
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    rows, cursor = [], start
    while cursor < end:
        flt = [
            "Collection/Name eq 'SENTINEL-1'",
            _attr_filter("productType", product_type),
            f"ContentDate/Start ge {cursor:%Y-%m-%dT%H:%M:%S.%f}Z",
            f"ContentDate/Start lt {end:%Y-%m-%dT%H:%M:%S.%f}Z",
        ]
        if wkt:
            flt.append(f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')")
        params = {
            "$filter": " and ".join(flt),
            "$orderby": "ContentDate/Start asc",
            "$top": page_size,
            "$expand": "Attributes",
        }
        r = _get(session, f"{CATALOGUE}/Products", params=params)
        page = r.json()["value"]
        for p in page:
            if p["Name"].endswith("_COG.SAFE"):
                continue
            attrs = {a["Name"]: a.get("Value") for a in p.get("Attributes", [])}
            rows.append(
                {
                    "id": p["Id"],
                    "name": p["Name"],
                    "start": pd.Timestamp(p["ContentDate"]["Start"]),
                    "end": pd.Timestamp(p["ContentDate"]["End"]),
                    "footprint": p.get("Footprint", "").split(";")[-1].rstrip("'"),
                    "platform": attrs.get("platformSerialIdentifier"),
                    "orbit_direction": attrs.get("orbitDirection"),
                    "relative_orbit": attrs.get("relativeOrbitNumber"),
                    "processing_date": pd.Timestamp(attrs["processingDate"]) if attrs.get("processingDate") else pd.NaT,
                    "processor_version": attrs.get("processorVersion"),
                    "timeliness": attrs.get("timeliness"),
                    "datatake_id": attrs.get("datatakeID"),
                    "slice_number": attrs.get("sliceNumber"),
                }
            )
        if len(page) < page_size:
            break
        last = pd.Timestamp(page[-1]["ContentDate"]["Start"]).tz_localize(None)
        # Advance past the last start; products sharing that exact start are rare but possible.
        cursor = last + pd.Timedelta(microseconds=1)
    df = pd.DataFrame(rows)
    return df.drop_duplicates("id").reset_index(drop=True) if not df.empty else df


# Dropped connections, timeouts, and bodies cut off mid-transfer (not a ConnectionError subclass)
_NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)


def _send(session, url, attempts=4, **kw):
    """``session.get`` that retries dropped connections and timeouts with backoff."""
    for attempt in range(attempts):
        try:
            return session.get(url, **kw)
        except _NETWORK_ERRORS:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** (attempt + 2))


def _get(session, url, retries=4, **kw):
    for attempt in range(retries):
        r = _send(session, url, timeout=120, **kw)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(_retry_after(r, 2 ** (attempt + 1)))
            continue
        r.raise_for_status()
        return r
    return r


def _nodes_url(product_id, *path):
    return f"{DOWNLOAD}/Products({product_id})" + "".join(f"/Nodes({p})" for p in path)


def list_nodes(product_id, *path, session=None):
    """List the children of a path inside a product, e.g. ``(id, safe_name, "annotation", "rfi")``."""
    session = session or requests.Session()
    r = _get(session, _nodes_url(product_id, *path) + "/Nodes")
    return [{"name": n["Name"], "size": n["ContentLength"], "children": n["ChildrenNumber"]} for n in r.json()["result"]]


class NotFound(requests.HTTPError):
    """HTTP 404 for a file inside a product (e.g. a guessed file name that does not exist)."""


def _retry_after(r, default):
    try:
        return max(float(r.headers.get("Retry-After", default)), 1.0)
    except ValueError:
        return default


class Client:
    """Authenticated CDSE session that refreshes its access token as needed.

    Safe to share between a few worker threads: token refresh is locked, and every request
    (listing or download) passes a shared throttle of ``min_interval`` seconds, so the
    request rate is bounded however many workers there are. ``stats`` counts requests,
    bytes and rate-limit responses.
    """

    def __init__(self, username=None, password=None, min_interval=0.1):
        self.username = username or os.environ.get("CDSE_USERNAME")
        self.password = password or os.environ.get("CDSE_PASSWORD")
        if not (self.username and self.password):
            raise RuntimeError("Set CDSE_USERNAME and CDSE_PASSWORD (free account at dataspace.copernicus.eu)")
        self.session = requests.Session()
        self.min_interval = min_interval
        self._token, self._expires = None, 0.0
        self._auth_lock, self._throttle_lock = threading.Lock(), threading.Lock()
        self._next_slot = 0.0
        self.stats = {"downloads": 0, "listings": 0, "bytes": 0, "http_429": 0, "http_5xx": 0, "tokens": 0}

    def _throttle(self):
        with self._throttle_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self.min_interval
        if wait > 0:
            time.sleep(wait)

    def _auth(self, force=False):
        with self._auth_lock:
            if force or time.time() > self._expires - 60:
                r = requests.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "password",
                        "username": self.username,
                        "password": self.password,
                        "client_id": "cdse-public",
                    },
                    timeout=60,
                )
                r.raise_for_status()
                tok = r.json()
                self._token, self._expires = tok["access_token"], time.time() + tok["expires_in"]
                self.stats["tokens"] += 1
            return {"Authorization": f"Bearer {self._token}"}

    def list_nodes(self, product_id, *path):
        """Throttled, counted :func:`list_nodes` (anonymous; no token is sent)."""
        self._throttle()
        self.stats["listings"] += 1
        return list_nodes(product_id, *path, session=self.session)

    def get_range(self, product_id, *path, start, stop):
        """Download bytes [start, stop) of one file inside a product (HTTP Range, answered 206)."""
        url = _nodes_url(product_id, *path) + "/$value"
        for attempt in range(5):
            self._throttle()
            headers = {**self._auth(), "Range": f"bytes={start}-{stop - 1}"}
            r = _send(self.session, url, headers=headers, timeout=300)
            if (r.status_code == 429 or r.status_code >= 500) and attempt < 4:
                self.stats["http_429" if r.status_code == 429 else "http_5xx"] += 1
                time.sleep(_retry_after(r, 2 ** (attempt + 2)))
                continue
            r.raise_for_status()
            if r.status_code != 206 or len(r.content) != stop - start:
                raise RuntimeError(f"expected {stop - start} bytes (206), got {len(r.content)} ({r.status_code})")
            self.stats["downloads"] += 1
            self.stats["bytes"] += len(r.content)
            return r.content
        r.raise_for_status()

    def get_file(self, product_id, *path, retries=5):
        """Download one file inside a product and return its bytes.

        Raises :class:`NotFound` on 404. Retries dropped connections, timeouts and 429/5xx
        with backoff (honouring ``Retry-After``) and refreshes the token once on 401.
        """
        url = _nodes_url(product_id, *path) + "/$value"
        refreshed = False
        for attempt in range(retries):
            # Follow redirects by hand so the bearer token is not dropped on a host change.
            for _ in range(5):
                self._throttle()
                r = _send(self.session, url, headers=self._auth(), timeout=120, allow_redirects=False)
                if r.status_code not in (301, 302, 303, 307, 308):
                    break
                url = r.headers["Location"]
            if r.status_code == 401 and not refreshed:
                self._auth(force=True)
                refreshed = True
                continue
            if r.status_code == 404:
                raise NotFound(f"404 {url}", response=r)
            if r.status_code == 429 or r.status_code >= 500:
                self.stats["http_429" if r.status_code == 429 else "http_5xx"] += 1
                if attempt < retries - 1:
                    time.sleep(_retry_after(r, 2 ** (attempt + 2)))
                    continue
            r.raise_for_status()
            self.stats["downloads"] += 1
            self.stats["bytes"] += len(r.content)
            return r.content
        r.raise_for_status()
        return r.content
