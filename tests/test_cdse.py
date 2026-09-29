"""Offline tests of the CDSE client's HTTP handling (no network)."""

import pytest

from s1rfi import cdse


class _Resp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cdse.requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        return {"access_token": f"tok{_Resp.n_tokens}", "expires_in": 600}


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, headers=None, **kw):
        self.calls.append((url, dict(headers or {})))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def client(monkeypatch):
    _Resp.n_tokens = 0

    def post(*a, **kw):
        _Resp.n_tokens += 1
        return _Resp(200)

    monkeypatch.setattr(cdse.requests, "post", post)
    monkeypatch.setattr(cdse.time, "sleep", lambda s: None)
    return cdse.Client("u", "p", min_interval=0)


def test_redirect_keeps_bearer_token(client):
    client.session = _Session([_Resp(302, headers={"Location": "https://other.host/file"}), _Resp(200, b"xml")])
    assert client.get_file("id", "a.SAFE", "annotation", "f.xml") == b"xml"
    (u1, h1), (u2, h2) = client.session.calls
    assert u1.endswith("/Nodes(a.SAFE)/Nodes(annotation)/Nodes(f.xml)/$value") and u2 == "https://other.host/file"
    assert h1["Authorization"] == h2["Authorization"] == "Bearer tok1"


def test_401_refreshes_token_once(client):
    client.session = _Session([_Resp(401), _Resp(200, b"ok")])
    assert client.get_file("id", "f") == b"ok"
    assert [h["Authorization"] for _, h in client.session.calls] == ["Bearer tok1", "Bearer tok2"]
    client.session = _Session([_Resp(401), _Resp(401)])
    with pytest.raises(cdse.requests.HTTPError):
        client.get_file("id", "f")


def test_404_raises_not_found(client):
    client.session = _Session([_Resp(404)])
    with pytest.raises(cdse.NotFound):
        client.get_file("id", "f")


def test_429_retried_and_counted(client):
    client.session = _Session([_Resp(429, headers={"Retry-After": "3"}), _Resp(503), _Resp(200, b"abc")])
    assert client.get_file("id", "f") == b"abc"
    assert client.stats["http_429"] == 1 and client.stats["http_5xx"] == 1
    assert client.stats["downloads"] == 1 and client.stats["bytes"] == 3


def test_dropped_connection_retried(client):
    client.session = _Session([cdse.requests.ConnectionError("proxy reset"), _Resp(200, b"ok")])
    assert client.get_file("id", "f") == b"ok"
    client.session = _Session([cdse.requests.exceptions.ChunkedEncodingError("cut off"), _Resp(200, b"ok")])
    assert client.get_file("id", "f") == b"ok"
    client.session = _Session([cdse.requests.ConnectionError("x")] * 4)
    with pytest.raises(cdse.requests.ConnectionError):
        client.get_file("id", "f")
