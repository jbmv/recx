"""The crawler's GitHub client: discovery and fetching (docs/design/search.md, section 2).

Standard library only. The transport, clock and sleep are injectable, so tests run against
constructed responses with a fake clock and make no network calls.

Errors: `RateLimited` (five consecutive rate-limit failures on one request) and `GitHubError`
(any other non-2xx response) end the run; the caller turns either into a non-zero exit. A
GraphQL fetch batch that fails for any other reason is skipped with a note, so the core keeps
those repositories' previous entries.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from crawler.model import CODE_SEARCH, RECHECK, SEED, TOPIC, Candidate, Fetched

API = "https://api.github.com"
SEEDS = Path(__file__).resolve().parent / "seeds.txt"
MANIFEST_PATH = "RECX.yaml"
CODE_QUERY = "filename:RECX.yaml path:/"
TOPIC_QUERY = "topic:recx"

PER_PAGE = 100
RESULT_CAP = 1000  # GitHub serves at most this many results per search query
CODE_SEARCH_INTERVAL = 6.5  # seconds between code search requests: under 10 a minute
BATCH_SIZE = 50  # candidates per GraphQL query
MAX_RATE_LIMIT_FAILURES = 5
DEFAULT_WAIT = 60.0  # wait on a rate limit that gives neither retry-after nor a usable reset
RESET_MARGIN = 1.0  # x-ratelimit-reset has one-second resolution; wait just past it

# Lower priority wins when merging channels: the name's spelling and node id come from the most
# authoritative channel that supplied them.
_PRIORITY = {CODE_SEARCH: 0, TOPIC: 1, RECHECK: 2, SEED: 3}
_FULL_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


# --- transport -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]  # names lower-cased
    body: bytes


Transport = Callable[[str, str, dict, "bytes | None"], Response]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> Response:
    """The real transport. Network failures raise OSError (urllib.error.URLError is one)."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:            return Response(resp.status, _lower(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as err:
        return Response(err.code, _lower(err.headers.items()), err.read())


def _lower(items) -> dict[str, str]:
    return {k.lower(): v for k, v in items}


class GitHubError(Exception):
    """A non-2xx response that is not a rate limit, or one the client gave up waiting on."""

    def __init__(self, status: int, url: str, message: str = ""):
        super().__init__(f"HTTP {status} from {url}" + (f": {message}" if message else ""))
        self.status = status
        self.url = url


class RateLimited(GitHubError):
    """Five consecutive rate-limit failures on one request. The run must end, non-zero."""


# --- seeds and previous index --------------------------------------------------------------


def read_seeds(path: Path = SEEDS) -> list[str]:
    """`owner/name` lines from seeds.txt; blank lines and `#` comments are skipped."""
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def previous_state(index: dict | None) -> tuple[list[str], dict[str, str]]:
    """From a previous index: the names to recheck, and `known_blobs` for `fetch`."""
    names, blobs = [], {}
    for entry in (index or {}).get("entries", []):
        source = entry.get("source", {})
        name = source.get("full_name")
        if name:
            names.append(name)
            if source.get("blob_sha"):
                blobs[name] = source["blob_sha"]
    return names, blobs


# --- GraphQL query text --------------------------------------------------------------------

_REPO_FIELDS = """
  nameWithOwner isFork isArchived stargazerCount createdAt pushedAt
  parent { nameWithOwner }
  licenseInfo { spdxId }
  defaultBranchRef { name target { oid } }
"""
# The full fragment reads the file's text; the light one is for candidates whose code search
# blob sha already equals the previous index's, so their text is not transferred again.
_BLOB_FIELDS = {True: "oid byteSize isBinary text", False: "oid byteSize isBinary"}
_FRAGMENTS = {
    with_text: f'fragment R on Repository {{{_REPO_FIELDS} manifest: object(expression: "HEAD:{MANIFEST_PATH}") {{'
    f" ... on Blob {{ {fields} }} }} }}"
    for with_text, fields in _BLOB_FIELDS.items()
}


def _nodes_query(with_text: bool) -> str:
    return "query($ids: [ID!]!) { nodes(ids: $ids) { ...R } }\n" + _FRAGMENTS[with_text]


def _names_query(count: int, with_text: bool) -> str:
    params = ", ".join(f"$o{i}: String!, $n{i}: String!" for i in range(count))
    fields = " ".join(f"r{i}: repository(owner: $o{i}, name: $n{i}) {{ ...R }}" for i in range(count))
    return f"query({params}) {{ {fields} }}\n" + _FRAGMENTS[with_text]


# --- the client ----------------------------------------------------------------------------


class GitHub:
    """A serial, rate-limit-aware GitHub client. `notes` collects lines for the job log."""

    def __init__(
        self,
        token: str,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        api: str = API,
    ):
        self._token = token
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._api = api.rstrip("/")
        self._last_code_search: float | None = None
        self._blocked_until: dict[str, float] = {}  # rate-limit resource -> epoch seconds
        self.notes: list[str] = []
        self.partial = False  # True when a search stayed incomplete after its retry

    # -- requests --

    def _headers(self, json_body: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "recx-crawler",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _wait(self, seconds: float, why: str) -> None:
        if seconds > 0:
            self.notes.append(f"waiting {seconds:.1f}s: {why}")
            self._sleep(seconds)

    def request(self, method: str, path: str, *, params=None, payload=None, resource: str = "core"):
        """Send one request and return the parsed JSON body, retrying on rate limits.

        `resource` names the rate-limit bucket (`code_search`, `search`, `graphql`, `core`).
        Code search requests are paced at least CODE_SEARCH_INTERVAL seconds apart, retries
        included. A bucket whose last response showed `x-ratelimit-remaining: 0` is left alone
        until its reset.
        """
        url = self._api + path + ("?" + urllib.parse.urlencode(params) if params else "")
        body = json.dumps(payload).encode() if payload is not None else None
        failures = 0
        while True:
            blocked = self._blocked_until.pop(resource, None)
            if blocked is not None:
                self._wait(blocked - self._clock(), f"{resource} rate limit exhausted, until reset")
            if resource == "code_search" and self._last_code_search is not None:
                self._wait(self._last_code_search + CODE_SEARCH_INTERVAL - self._clock(), "code search pacing")
            if resource == "code_search":
                self._last_code_search = self._clock()

            resp = self._transport(method, url, self._headers(body is not None), body)
            data = _parse(resp.body)
            limited = self._rate_limit_wait(resp, data)
            if limited is None:
                self._note_exhausted(resp, resource)
                if not 200 <= resp.status < 300:
                    message = data.get("message", "") if isinstance(data, dict) else ""
                    raise GitHubError(resp.status, url, str(message)[:200])
                if data is _UNPARSEABLE:
                    raise GitHubError(resp.status, url, "response body is not JSON")
                return data

            failures += 1
            if failures >= MAX_RATE_LIMIT_FAILURES:
                raise RateLimited(resp.status, url, f"{failures} consecutive rate-limit failures")
            self._wait(limited, f"rate limited (HTTP {resp.status}) on {path}, attempt {failures}")

    def _rate_limit_wait(self, resp: Response, data) -> float | None:
        """Seconds to wait if `resp` is a rate-limit failure, else None."""
        h = resp.headers
        if resp.status == 429:
            pass
        elif resp.status == 403:
            # A 403 is also GitHub's answer to "not allowed". Count it as a rate limit only when
            # it says so: retry-after, an exhausted quota, or a message naming the rate limit.
            message = str(data.get("message", "")) if isinstance(data, dict) else ""
            if not ("retry-after" in h or h.get("x-ratelimit-remaining") == "0" or "rate limit" in message.lower()):
                return None
        elif 200 <= resp.status < 300 and _graphql_rate_limited(data):
            pass  # GraphQL reports its primary limit as HTTP 200 with a RATE_LIMITED error
        else:
            return None

        retry_after = _number(h.get("retry-after"))
        if retry_after is not None:
            return max(retry_after, 0.0)
        reset = _number(h.get("x-ratelimit-reset"))
        if h.get("x-ratelimit-remaining") == "0" and reset is not None:
            return max(reset - self._clock(), 0.0) + RESET_MARGIN
        return DEFAULT_WAIT

    def _note_exhausted(self, resp: Response, resource: str) -> None:
        reset = _number(resp.headers.get("x-ratelimit-reset"))
        if resp.headers.get("x-ratelimit-remaining") == "0" and reset is not None:
            self._blocked_until[resource] = reset + RESET_MARGIN

    # -- discovery --

    def _search(self, path: str, query: str, resource: str, label: str) -> list[dict]:
        """Every item of one search query, paged up to the 1,000-result cap."""
        items: list[dict] = []
        for page in range(1, RESULT_CAP // PER_PAGE + 1):
            params = {"q": query, "per_page": PER_PAGE, "page": page}
            data = self.request("GET", path, params=params, resource=resource)
            if data.get("incomplete_results"):
                self.notes.append(f"{label}: page {page} incomplete, retrying once")
                items.extend(data.get("items", []))
                data = self.request("GET", path, params=params, resource=resource)
                if data.get("incomplete_results"):
                    self.partial = True
                    self.notes.append(f"{label}: page {page} still incomplete; run is partial")
            page_items = data.get("items", [])
            items.extend(page_items)
            total = data.get("total_count", 0)
            if page == 1 and total > RESULT_CAP:
                self.notes.append(
                    f"{label}: total_count {total} exceeds the {RESULT_CAP}-result cap; "
                    "only the first 1000 were read (splitting is phase 2)"
                )
            if len(page_items) < PER_PAGE or page * PER_PAGE >= min(total, RESULT_CAP):
                break
        return items

    def code_search(self) -> list[Candidate]:
        items = self._search("/search/code", CODE_QUERY, "code_search", "code search")
        found = []
        for item in items:
            if item.get("path") != MANIFEST_PATH:
                continue
            repo = item.get("repository") or {}
            if not repo.get("full_name"):
                continue
            ref = urllib.parse.parse_qs(urllib.parse.urlsplit(item.get("url", "")).query).get("ref", [None])[0]
            found.append(
                Candidate(
                    full_name=repo["full_name"],
                    found_by=frozenset({CODE_SEARCH}),
                    node_id=repo.get("node_id"),
                    blob_sha=item.get("sha"),
                    commit=ref,
                )
            )
        self.notes.append(f"code search: {len(items)} items, {len(found)} with path {MANIFEST_PATH}")
        return found

    def topic_search(self) -> list[Candidate]:
        items = self._search("/search/repositories", TOPIC_QUERY, "search", "topic search")
        found = [
            Candidate(full_name=i["full_name"], found_by=frozenset({TOPIC}), node_id=i.get("node_id"))
            for i in items
            if i.get("full_name")
        ]
        self.notes.append(f"topic search: {len(found)} repositories")
        return found

    def discover(
        self,
        recheck: Iterable[str] = (),
        seeds: Iterable[str] | None = None,
        *,
        code: bool = True,
        topic: bool = True,
    ) -> list[Candidate]:
        """Candidates from code search, topic search, seeds and the previous index, merged.

        `seeds` defaults to crawler/seeds.txt; `recheck` is every full_name in the previous
        index. Search errors propagate: a discovery failure ends the run.
        """
        found: list[Candidate] = []
        if code:
            found += self.code_search()
        if topic:
            found += self.topic_search()
        seeds = read_seeds() if seeds is None else list(seeds)
        found += [Candidate(full_name=n, found_by=frozenset({SEED})) for n in seeds]
        found += [Candidate(full_name=n, found_by=frozenset({RECHECK})) for n in recheck]
        merged = merge(found)
        self.notes.append(f"discovery: {len(merged)} candidates")
        return merged

    # -- fetching --

    def fetch(self, candidates: Iterable[Candidate], known_blobs: dict[str, str]) -> dict[str, Fetched]:
        """Repository metadata and RECX.yaml for each candidate, keyed by candidate full_name.

        Batches of at most BATCH_SIZE go through GraphQL: by node id where known, by aliased
        `repository(owner:, name:)` otherwise. A candidate whose code search blob sha equals
        `known_blobs` is fetched without the file's text; if the fetched oid then differs (code
        search was stale) it is fetched again with the text. Any candidate whose fetched oid
        equals `known_blobs` comes back with `unchanged=True` and `text=None`. Candidates in a
        batch that fails are left out of the result, so the core keeps their previous entries.
        """
        known = {k.lower(): v for k, v in known_blobs.items()}
        light, full = [], []
        for c in candidates:
            if not c.node_id and not _FULL_NAME.match(c.full_name):
                self.notes.append(f"fetch: skipped malformed name {c.full_name!r}")
                continue
            same = c.blob_sha is not None and c.blob_sha == known.get(c.full_name.lower())
            (light if same else full).append(c)

        results: dict[str, Fetched] = {}
        stale = self._fetch_group(light, known, results, with_text=False)
        self._fetch_group(full + stale, known, results, with_text=True)
        if light:
            self.notes.append(
                f"fetch: {len(light)} matched the previous blob by code search; text not requested"
                + (f", {len(stale)} had changed since and were re-read" if stale else "")
            )
        unchanged = sum(f.unchanged for f in results.values())
        gone = sum(not f.exists for f in results.values())
        self.notes.append(f"fetch: {len(results)} fetched, {unchanged} unchanged, {gone} not found")
        return results

    def _fetch_group(self, cands, known, results, *, with_text: bool) -> list[Candidate]:
        """Fetch `cands` into `results`. Without text, returns those whose blob changed."""
        by_node = [c for c in cands if c.node_id]
        by_name = [c for c in cands if not c.node_id]
        stale: list[Candidate] = []
        for batches, by_id in ((_chunks(by_node), True), (_chunks(by_name), False)):
            for batch in batches:
                try:
                    pairs = self._query_batch(batch, by_id, with_text)
                except RateLimited:
                    raise
                except (GitHubError, OSError, ValueError) as err:
                    names = ", ".join(c.full_name for c in batch[:3]) + (", ..." if len(batch) > 3 else "")
                    self.notes.append(f"fetch: batch of {len(batch)} skipped ({names}): {err}")
                    continue
                for cand, node, error in pairs:
                    fetched = self._to_fetched(cand, node, error, known, with_text)
                    if fetched is None:
                        continue
                    if not with_text and fetched.exists and fetched.blob_sha and not fetched.unchanged:
                        stale.append(cand)
                        continue
                    results[cand.full_name] = fetched
        return stale

    def _query_batch(self, batch: list[Candidate], by_id: bool, with_text: bool):
        """One GraphQL query; yields (candidate, node or None, error type or None)."""
        if by_id:
            payload = {"query": _nodes_query(with_text), "variables": {"ids": [c.node_id for c in batch]}}
        else:
            variables = {}
            for i, c in enumerate(batch):
                variables[f"o{i}"], variables[f"n{i}"] = c.full_name.split("/")
            payload = {"query": _names_query(len(batch), with_text), "variables": variables}
        data = self.request("POST", "/graphql", payload=payload, resource="graphql")
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            errors = data.get("errors") if isinstance(data, dict) else None
            raise ValueError(f"GraphQL returned no data: {_error_summary(errors)}")

        errors_at: dict[tuple, str] = {}
        for err in data.get("errors") or []:
            path = tuple(err.get("path") or ())
            errors_at[path] = err.get("type") or "UNKNOWN"
        if by_id:
            nodes = data["data"].get("nodes") or []
            if len(nodes) != len(batch):
                raise ValueError(f"GraphQL returned {len(nodes)} nodes for {len(batch)} ids")
            return [(c, nodes[i], errors_at.get(("nodes", i))) for i, c in enumerate(batch)]
        return [(c, data["data"].get(f"r{i}"), errors_at.get((f"r{i}",))) for i, c in enumerate(batch)]

    def _to_fetched(self, cand: Candidate, node, error, known, with_text) -> Fetched | None:
        if node is None:
            if error in (None, "NOT_FOUND"):
                return Fetched(full_name=cand.full_name, exists=False)
            self.notes.append(f"fetch: {cand.full_name} skipped, GraphQL error {error}")
            return None
        name = node.get("nameWithOwner")
        if not name:
            self.notes.append(f"fetch: {cand.full_name} skipped, node is not a repository")
            return None
        if name.lower() != cand.full_name.lower():
            # Renamed or transferred. The old name is gone; discovery finds the new one.
            self.notes.append(f"fetch: {cand.full_name} now resolves to {name}; treated as gone")
            return Fetched(full_name=cand.full_name, exists=False)

        branch = node.get("defaultBranchRef") or {}
        blob = node.get("manifest") or {}
        oid = blob.get("oid")  # None when there is no file, or RECX.yaml is not a blob
        unchanged = oid is not None and oid == known.get(cand.full_name.lower())
        text = None
        if with_text and oid and not unchanged and not blob.get("isBinary"):
            text = blob.get("text")
        return Fetched(
            full_name=name,
            exists=True,
            default_branch=branch.get("name"),
            commit=(branch.get("target") or {}).get("oid"),
            blob_sha=oid,
            byte_size=blob.get("byteSize") if oid else None,
            text=text,
            unchanged=unchanged,
            is_fork=bool(node.get("isFork")),
            parent=(node.get("parent") or {}).get("nameWithOwner"),
            is_archived=bool(node.get("isArchived")),
            stars=node.get("stargazerCount") or 0,
            created_at=node.get("createdAt"),
            pushed_at=node.get("pushedAt"),
            detected_license=(node.get("licenseInfo") or {}).get("spdxId"),
        )


# --- helpers -------------------------------------------------------------------------------


def merge(found: Iterable[Candidate]) -> list[Candidate]:
    """One Candidate per full_name, case-insensitively, with `found_by` the union.

    The spelling and node id come from the highest-priority channel that has them (code
    search, then topic, recheck, seed); blob sha and commit come from code search only.
    """
    groups: dict[str, list[Candidate]] = {}
    for c in found:
        groups.setdefault(c.full_name.strip().lower(), []).append(c)
    merged = []
    for group in groups.values():
        group.sort(key=lambda c: min(_PRIORITY.get(ch, 9) for ch in c.found_by))
        merged.append(
            Candidate(
                full_name=group[0].full_name.strip(),
                found_by=frozenset().union(*(c.found_by for c in group)),
                node_id=next((c.node_id for c in group if c.node_id), None),
                blob_sha=next((c.blob_sha for c in group if c.blob_sha), None),
                commit=next((c.commit for c in group if c.commit), None),
            )
        )
    return sorted(merged, key=lambda c: c.full_name.lower())


def _chunks(items: list, size: int = BATCH_SIZE) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


_UNPARSEABLE = object()


def _parse(body: bytes):
    if not body:
        return {}
    try:
        return json.loads(body)
    except ValueError:
        return _UNPARSEABLE


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _graphql_rate_limited(data) -> bool:
    return isinstance(data, dict) and any(
        isinstance(e, dict) and e.get("type") == "RATE_LIMITED" for e in data.get("errors") or []
    )


def _error_summary(errors) -> str:
    if not errors:
        return "no errors given"
    return "; ".join(f"{e.get('type', '?')}: {str(e.get('message', ''))[:120]}" for e in errors[:3])
