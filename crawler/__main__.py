"""Run the crawl: `python -m crawler --out DIR`. Writes DIR/v1/index.json and DIR/v1/rejected.json.

Exit status: 0 completed and guards passed; 1 GitHub error or rate limit; 2 usage or
previous-index fetch error; 3 partial crawl (files written, do not publish); 4 shrink guard.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from crawler import core
from crawler.github import GitHub, GitHubError, RateLimited, previous_state

DEFAULT_PREVIOUS_URL = "https://jbmv.github.io/recx/v1/index.json"
BLOCKLIST = Path(__file__).resolve().parent / "blocklist.txt"
SHRINK_FRACTION = 0.30
SHRINK_MIN_DROP = 5

OK, ERROR, USAGE, PARTIAL, SHRUNK = 0, 1, 2, 3, 4


class PreviousError(Exception):
    """The previous index could not be fetched or parsed (anything but a 404)."""


def fetch_previous(url: str) -> dict | None:
    """The previous index parsed from `url`; None on HTTP 404 (a first run)."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise PreviousError(f"HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise PreviousError(f"{url}: {e}") from e
    try:
        data = json.loads(body)
    except ValueError as e:
        raise PreviousError(f"{url}: not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise PreviousError(f"{url}: not a JSON object")
    return data


def shrink_tripped(previous_count: int, new_count: int) -> bool:
    """True when the entry count fell by more than 30 percent and by more than 5 entries."""
    drop = previous_count - new_count
    return drop > SHRINK_FRACTION * previous_count and drop > SHRINK_MIN_DROP


def exit_status(*, partial: bool, previous_count: int, new_count: int) -> int:
    if partial:
        return PARTIAL
    if shrink_tripped(previous_count, new_count):
        return SHRUNK
    return OK


def _generator() -> str:
    try:
        version = metadata.version("recx-cli")
    except metadata.PackageNotFoundError:
        version = "unknown"
    return f"recx-crawler (recx-cli {version})"


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(
    argv: list[str] | None = None,
    *,
    fetch_previous: Callable[[str], dict | None] = fetch_previous,
    client_factory: Callable[[str], GitHub] = GitHub,
    env: dict | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m crawler", description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("--previous-url", default=DEFAULT_PREVIOUS_URL)
    parser.add_argument("--now", help="ISO timestamp to stamp the index with (default: current UTC time)")
    parser.add_argument("--no-code-search", action="store_true")
    parser.add_argument("--no-topic", action="store_true")
    args = parser.parse_args(argv)  # exits 2 on usage errors

    token = (os.environ if env is None else env).get("GITHUB_TOKEN")
    if not token:
        print("error: GITHUB_TOKEN is not set", file=sys.stderr)
        return USAGE

    try:
        previous = fetch_previous(args.previous_url)
    except PreviousError as e:
        print(f"error: could not fetch the previous index: {e}; nothing written", file=sys.stderr)
        return USAGE
    print("previous index: none (first run)" if previous is None else
          f"previous index: {len(previous.get('entries', []))} entries")

    now = args.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    recheck, known_blobs = previous_state(previous)
    client = client_factory(token)
    try:
        candidates = client.discover(
            recheck=recheck, code=not args.no_code_search, topic=not args.no_topic
        )
        fetched = client.fetch(candidates, known_blobs)
    except (GitHubError, RateLimited) as e:
        for note in client.notes:
            print(note)
        print(f"error: {e}", file=sys.stderr)
        return ERROR

    result = core.build(
        previous, candidates, fetched,
        now=now, blocklist=core.load_list(BLOCKLIST), generator=_generator(),
    )
    out = args.out / "v1"
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "index.json", result.index)
    _write(out / "rejected.json", result.rejected)

    for note in [*client.notes, *result.notes]:
        print(note)

    previous_count = len((previous or {}).get("entries", []))
    new_count = len(result.index["entries"])
    status = exit_status(partial=client.partial, previous_count=previous_count, new_count=new_count)
    if status == PARTIAL:
        print("PARTIAL crawl: files written for inspection; do not publish", file=sys.stderr)
    elif status == SHRUNK:
        print(f"SHRINK guard: entries fell from {previous_count} to {new_count}; do not publish",
              file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
