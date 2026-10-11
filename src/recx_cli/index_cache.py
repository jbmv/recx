"""Client for the published search index: download, cache, revalidate.

Standard library only. See docs/design/search.md, "How a client finds and caches it".
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

DEFAULT_INDEX_URL = "https://jbmv.github.io/recx/v1/index.json"
MAX_BYTES = 50 * 1024 * 1024
FRESH_SECONDS = 6 * 3600
STALE_SECONDS = 7 * 86400
TIMEOUT = 20
DATA_NAME = "index-v1.json"
META_NAME = "index-v1.meta.json"
_FORMAT = re.compile(r"recx-index/(\d+)\.(\d+)")
_CHUNK = 64 * 1024


class IndexUnavailable(Exception):
    """The index cannot be loaded; the message says why."""


@dataclass
class LoadedIndex:
    data: dict
    url: str
    fetched_at: str
    stale: bool
    stale_reason: str | None


# An opener is called as opener(url, headers) and returns an object with
# `status` (or `code`), `headers` (a mapping) and `read(n)`. It raises OSError
# (URLError and friends) when the network fails. A 304 is returned, not raised.
Opener = Callable[[str, "dict[str, str]"], Any]


def _urllib_opener(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return error
        raise


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def default_cache_dir() -> Path:
    env = os.environ.get("RECX_CACHE_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "recx" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "recx"
    return Path.home() / ".cache" / "recx"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _check(body: bytes, source: str) -> dict:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError) as error:
        raise IndexUnavailable(f"{source} is not valid JSON ({error})") from error
    if not isinstance(data, dict):
        raise IndexUnavailable(f"{source} is not a JSON object")
    match = _FORMAT.fullmatch(str(data.get("format", "")))
    if not match:
        raise IndexUnavailable(f"{source} has no recognised format (got {data.get('format')!r})")
    if match.group(1) != "1":
        raise IndexUnavailable(
            f"{source} uses format {data['format']}, which this recx-cli does not understand; "
            "upgrade recx-cli (pip install --upgrade recx-cli)"
        )
    return data


def _too_big() -> IndexUnavailable:
    return IndexUnavailable("the index is larger than 50 MB; refusing to read it")


def _read_limited(response: Any) -> bytes:
    headers = response.headers
    encoding = (headers.get("Content-Encoding") or "").strip().lower() if headers else ""
    inflater = zlib.decompressobj(31) if encoding == "gzip" else None
    out = bytearray()
    raw = 0
    while True:
        chunk = response.read(_CHUNK)
        if not chunk:
            break
        raw += len(chunk)
        if raw > MAX_BYTES:
            raise _too_big()
        if inflater is None:
            out += chunk
        else:
            try:
                pending = chunk
                while pending and len(out) <= MAX_BYTES:
                    out += inflater.decompress(pending, MAX_BYTES + 1 - len(out))
                    pending = inflater.unconsumed_tail
            except zlib.error as error:
                raise IndexUnavailable(f"the index response is not valid gzip ({error})") from error
        if len(out) > MAX_BYTES:
            raise _too_big()
    return bytes(out)


def _read_cache(directory: Path, url: str) -> tuple[dict, dict] | None:
    try:
        meta = json.loads((directory / META_NAME).read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or meta.get("url") != url:
            return None
        if _parse_iso(meta.get("fetched_at")) is None:
            return None
        data = _check((directory / DATA_NAME).read_bytes(), "the cached index")
    except (OSError, ValueError, IndexUnavailable):
        return None
    return data, meta


def _write_meta(directory: Path, url: str, headers: Any, fetched_at: str, old: dict | None) -> None:
    def get(name: str) -> str | None:
        return headers.get(name) if headers is not None else None

    meta = {
        "url": url,
        "etag": get("ETag") or get("Etag") or get("etag") or (old or {}).get("etag"),
        "last_modified": get("Last-Modified") or (old or {}).get("last_modified"),
        "fetched_at": fetched_at,
    }
    _atomic_write(directory / META_NAME, json.dumps(meta).encode("utf-8"))


def _result(data: dict, url: str, fetched_at: str, now: float, reason: str | None) -> LoadedIndex:
    generated = _parse_iso(data.get("generated_at"))
    stale = generated is not None and now - generated > STALE_SECONDS
    return LoadedIndex(data=data, url=url, fetched_at=fetched_at, stale=stale, stale_reason=reason)


def _load_file(url: str, now: float) -> LoadedIndex:
    parsed = urlparse(url)
    path = urllib.request.url2pathname(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        path = f"//{parsed.netloc}{path}"
    try:
        with open(path, "rb") as handle:
            body = handle.read(MAX_BYTES + 1)
    except OSError as error:
        raise IndexUnavailable(f"cannot read {url}: {error}") from error
    if len(body) > MAX_BYTES:
        raise _too_big()
    return _result(_check(body, url), url, _iso(now), now, None)


def load_index(
    *,
    url: str | None = None,
    cache_dir: str | os.PathLike | None = None,
    refresh: bool = False,
    offline: bool = False,
    opener: Opener | None = None,
    clock: Callable[[], float] | None = None,
) -> LoadedIndex:
    url = url or os.environ.get("RECX_INDEX_URL") or DEFAULT_INDEX_URL
    now = (clock or time.time)()
    if url.startswith("file:"):
        return _load_file(url, now)

    directory = Path(cache_dir) if cache_dir else default_cache_dir()
    cached = _read_cache(directory, url)

    if offline:
        if cached is None:
            raise IndexUnavailable("offline and no cached index is available")
        return _result(cached[0], url, cached[1]["fetched_at"], now, None)

    if cached is not None and not refresh:
        age = now - (_parse_iso(cached[1]["fetched_at"]) or 0)
        if 0 <= age < FRESH_SECONDS:
            return _result(cached[0], url, cached[1]["fetched_at"], now, None)

    headers = {"Accept": "application/json", "Accept-Encoding": "gzip", "User-Agent": "recx-cli"}
    if cached is not None:
        if cached[1].get("etag"):
            headers["If-None-Match"] = cached[1]["etag"]
        if cached[1].get("last_modified"):
            headers["If-Modified-Since"] = cached[1]["last_modified"]

    try:
        response = (opener or _urllib_opener)(url, headers)
        status = getattr(response, "status", None) or getattr(response, "code", 200)
        body = b"" if status == 304 else _read_limited(response)
    except IndexUnavailable:
        raise
    except (OSError, ValueError) as error:  # URLError, HTTPError, timeouts, resets
        if cached is not None:
            fetched = cached[1]["fetched_at"]
            reason = f"could not reach {url} ({error}); using the copy cached at {fetched}"
            return _result(cached[0], url, fetched, now, reason)
        raise IndexUnavailable(f"could not reach {url} ({error}) and no cached index exists") from error

    fetched_at = _iso(now)
    if status == 304:
        if cached is None:
            raise IndexUnavailable(f"{url} answered 304 but there is no cached index")
        _write_meta(directory, url, response.headers, fetched_at, cached[1])
        return _result(cached[0], url, fetched_at, now, None)

    data = _check(body, url)
    _atomic_write(directory / DATA_NAME, body)
    _write_meta(directory, url, response.headers, fetched_at, None)
    return _result(data, url, fetched_at, now, None)
