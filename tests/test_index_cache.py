import gzip
import io
import json
import sys
import urllib.error

import pytest

from recx_cli import index_cache
from recx_cli.index_cache import DEFAULT_INDEX_URL, IndexUnavailable, load_index

NOW = 1_800_000_000.0
HOUR = 3600


def index_obj(fmt="recx-index/1.0", generated=NOW):
    return {
        "format": fmt,
        "generated_at": index_cache._iso(generated),
        "generator": {"name": "test"},
        "spec_versions": ["0.1"],
        "counts": {},
        "entries": [],
    }


class Resp:
    def __init__(self, body=b"", status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self._io = io.BytesIO(body)
        self.read_total = 0

    def read(self, n=-1):
        chunk = self._io.read(n)
        self.read_total += len(chunk)
        return chunk


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(obj=None, etag='"abc"'):
    body = json.dumps(obj or index_obj()).encode()
    return Resp(body, 200, {"ETag": etag, "Last-Modified": "Mon, 01 Jan 2029 00:00:00 GMT"})


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("RECX_INDEX_URL", "RECX_CACHE_DIR", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)


def test_downloads_from_default_url(tmp_path):
    opener = Opener(ok())
    result = load_index(cache_dir=tmp_path, opener=opener, clock=lambda: NOW)
    assert opener.calls[0][0] == DEFAULT_INDEX_URL
    assert result.data["format"] == "recx-index/1.0"
    assert result.stale is False and result.stale_reason is None
    assert (tmp_path / "index-v1.json").exists()
    meta = json.loads((tmp_path / "index-v1.meta.json").read_text())
    assert meta["etag"] == '"abc"' and meta["url"] == DEFAULT_INDEX_URL
    assert meta["last_modified"] and meta["fetched_at"] == result.fetched_at


def test_second_call_within_six_hours_makes_no_request(tmp_path):
    load_index(cache_dir=tmp_path, opener=Opener(ok()), clock=lambda: NOW)
    opener = Opener()
    result = load_index(cache_dir=tmp_path, opener=opener, clock=lambda: NOW + 5 * HOUR)
    assert opener.calls == []
    assert result.data["format"] == "recx-index/1.0"


def test_call_after_six_hours_sends_if_none_match(tmp_path):
    load_index(cache_dir=tmp_path, opener=Opener(ok()), clock=lambda: NOW)
    opener = Opener(ok(index_obj(generated=NOW + 7 * HOUR), etag='"def"'))
    load_index(cache_dir=tmp_path, opener=opener, clock=lambda: NOW + 7 * HOUR)
    headers = opener.calls[0][1]
    assert headers["If-None-Match"] == '"abc"'
    assert headers["If-Modified-Since"] == "Mon, 01 Jan 2029 00:00:00 GMT"


def test_304_updates_fetched_at_and_keeps_file(tmp_path):
    load_index(cache_dir=tmp_path, opener=Opener(ok()), clock=lambda: NOW)
    before = (tmp_path / "index-v1.json").read_bytes()
    later = NOW + 7 * HOUR
    result = load_index(cache_dir=tmp_path, opener=Opener(Resp(b"", 304)), clock=lambda: later)
    assert (tmp_path / "index-v1.json").read_bytes() == before
    meta = json.loads((tmp_path / "index-v1.meta.json").read_text())
    assert meta["fetched_at"] == index_cache._iso(later) == result.fetched_at
    assert meta["etag"] == '"abc"'


def test_refresh_forces_revalidation(tmp_path):
    load_index(cache_dir=tmp_path, opener=Opener(ok()), clock=lambda: NOW)
    opener = Opener(Resp(b"", 304))
    load_index(cache_dir=tmp_path, opener=opener, clock=lambda: NOW + 60, refresh=True)
    assert len(opener.calls) == 1


def test_network_down_with_cache_returns_stale_reason(tmp_path):
    load_index(cache_dir=tmp_path, opener=Opener(ok()), clock=lambda: NOW)
    opener = Opener(urllib.error.URLError("no route"))
    result = load_index(cache_dir=tmp_path, opener=opener, clock=lambda: NOW + 7 * HOUR)
    assert result.stale_reason and "no route" in result.stale_reason
    assert result.data["format"] == "recx-index/1.0"


def test_network_down_without_cache_raises(tmp_path):
    with pytest.raises(IndexUnavailable):
        load_index(cache_dir=tmp_path, opener=Opener(urllib.error.URLError("down")), clock=lambda: NOW)


def test_offline(tmp_path):
    opener = Opener()
    with pytest.raises(IndexUnavailable):
        load_index(cache_dir=tmp_path, opener=opener, offline=True, clock=lambda: NOW)
    load_index(cache_dir=tmp_path, opener=Opener(ok()), clock=lambda: NOW)
    result = load_index(cache_dir=tmp_path, opener=opener, offline=True, clock=lambda: NOW + 99 * HOUR)
    assert opener.calls == [] and result.data


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("RECX_INDEX_URL", "https://example.test/i.json")
    monkeypatch.setenv("RECX_CACHE_DIR", str(tmp_path / "c"))
    opener = Opener(ok())
    load_index(opener=opener, clock=lambda: NOW)
    assert opener.calls[0][0] == "https://example.test/i.json"
    assert (tmp_path / "c" / "index-v1.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="posix cache path")
def test_cache_dir_falls_back_to_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert index_cache.default_cache_dir() == tmp_path / "recx"


def test_cache_for_other_url_is_not_reused(tmp_path):
    load_index(cache_dir=tmp_path, url="https://a.test/i.json", opener=Opener(ok()), clock=lambda: NOW)
    opener = Opener(ok())
    load_index(cache_dir=tmp_path, url="https://b.test/i.json", opener=opener, clock=lambda: NOW)
    assert len(opener.calls) == 1 and "If-None-Match" not in opener.calls[0][1]


def test_file_url(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps(index_obj()))
    opener = Opener()
    result = load_index(url=path.as_uri(), cache_dir=tmp_path / "c", opener=opener, clock=lambda: NOW)
    assert result.data["format"] == "recx-index/1.0" and opener.calls == []


def test_over_50mb_refused_without_reading_it_all(tmp_path):
    big = Resp(b"x" * (index_cache.MAX_BYTES + 10 * 1024 * 1024))
    with pytest.raises(IndexUnavailable, match="50 MB"):
        load_index(cache_dir=tmp_path, opener=Opener(big), clock=lambda: NOW)
    assert big.read_total <= index_cache.MAX_BYTES + 64 * 1024
    assert not (tmp_path / "index-v1.json").exists()


def test_gzip_bomb_refused(tmp_path):
    body = gzip.compress(b"x" * (index_cache.MAX_BYTES + 1000))
    resp = Resp(body, 200, {"Content-Encoding": "gzip"})
    with pytest.raises(IndexUnavailable, match="50 MB"):
        load_index(cache_dir=tmp_path, opener=Opener(resp), clock=lambda: NOW)


def test_gzip_accepted(tmp_path):
    resp = Resp(gzip.compress(json.dumps(index_obj()).encode()), 200, {"Content-Encoding": "gzip"})
    assert load_index(cache_dir=tmp_path, opener=Opener(resp), clock=lambda: NOW).data["entries"] == []


def test_unknown_major_version_says_upgrade(tmp_path):
    with pytest.raises(IndexUnavailable, match="upgrade recx-cli"):
        load_index(cache_dir=tmp_path, opener=Opener(ok(index_obj("recx-index/2.0"))), clock=lambda: NOW)


def test_bad_fresh_response_keeps_good_cache(tmp_path):
    load_index(cache_dir=tmp_path, opener=Opener(ok()), clock=lambda: NOW)
    before = (tmp_path / "index-v1.json").read_bytes()
    for bad in (Resp(b"not json"), ok(index_obj("recx-index/2.0")), Resp(b"[1]")):
        with pytest.raises(IndexUnavailable):
            load_index(cache_dir=tmp_path, opener=Opener(bad), clock=lambda: NOW + 7 * HOUR)
        assert (tmp_path / "index-v1.json").read_bytes() == before


def test_corrupt_cache_is_no_cache(tmp_path):
    load_index(cache_dir=tmp_path, opener=Opener(ok()), clock=lambda: NOW)
    (tmp_path / "index-v1.json").write_text("{broken")
    opener = Opener(ok())
    load_index(cache_dir=tmp_path, opener=opener, clock=lambda: NOW + 60)
    assert len(opener.calls) == 1 and "If-None-Match" not in opener.calls[0][1]


def test_stale_when_generated_over_seven_days_ago(tmp_path):
    old = index_obj(generated=NOW - 8 * 86400)
    assert load_index(cache_dir=tmp_path, opener=Opener(ok(old)), clock=lambda: NOW).stale is True
    new = index_obj(generated=NOW - 6 * 86400)
    assert load_index(cache_dir=tmp_path / "n", opener=Opener(ok(new)), clock=lambda: NOW).stale is False
