"""crawler/github.py against constructed GitHub responses: no network, no real sleeping."""

import itertools
import json
import socket
import urllib.parse

import pytest

from crawler import github as gh
from crawler.github import GitHub, GitHubError, RateLimited, Response
from crawler.model import CODE_SEARCH, RECHECK, SEED, TOPIC, Candidate

T0 = 1_760_000_000.0  # a fixed epoch time for the fake clock
COMMIT = "4f2c0d9a7e1b3c5d6f8a9b0c1d2e3f4a5b6c7d8e"
BLOB = "9b2e1a0c3d4f5e6a7b8c9d0e1f2a3b4c5d6e7f80"
NEW_BLOB = "1111111111111111111111111111111111111111"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open a socket fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("test tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Clock:
    def __init__(self):
        self.now = T0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    """Answers each request from `handler(method, url, body)`; records what was sent and when."""

    def __init__(self, clock, handler):
        self.clock = clock
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, headers, body):
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "headers": headers, "payload": payload, "at": self.clock.now})
        result = self.handler(method, url, payload)
        if isinstance(result, Exception):
            raise result
        return result


def ok(data, **headers):
    base = {"content-type": "application/json; charset=utf-8", "x-ratelimit-remaining": "9"}
    base.update({k.replace("_", "-"): str(v) for k, v in headers.items()})
    return Response(200, base, json.dumps(data).encode())


def err(status, message="", **headers):
    hdrs = {k.replace("_", "-"): str(v) for k, v in headers.items()}
    return Response(status, hdrs, json.dumps({"message": message, "documentation_url": "https://docs.github.com"}).encode())


def queue(*responses):
    """A handler that answers with each response in turn."""
    pending = list(responses)

    def handler(method, url, payload):
        return pending.pop(0)

    return handler


def make(handler):
    clock = Clock()
    transport = FakeTransport(clock, handler)
    client = GitHub("test-token", transport=transport, clock=clock.time, sleep=clock.sleep)
    return client, transport, clock


def params(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


# Shaped like GET /search/code items (fields trimmed).
def code_item(full_name, path="RECX.yaml", sha=BLOB, commit=COMMIT, node_id=None):
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": sha,
        "url": f"https://api.github.com/repositories/1/contents/{path}?ref={commit}",
        "git_url": f"https://api.github.com/repositories/1/git/blobs/{sha}",
        "html_url": f"https://github.com/{full_name}/blob/{commit}/{path}",
        "repository": {
            "id": 1,
            "node_id": node_id or f"R_{full_name}",
            "name": full_name.split("/")[1],
            "full_name": full_name,
            "private": False,
            "fork": False,
        },
        "score": 1.0,
    }


def search_page(items, total=None, incomplete=False):
    return {"total_count": len(items) if total is None else total, "incomplete_results": incomplete, "items": items}


# Shaped like a GraphQL Repository with the design's fields.
def repo_node(full_name, oid=BLOB, text="recx: '0.1'\n", license="MIT", **extra):
    node = {
        "nameWithOwner": full_name,
        "isFork": False,
        "isArchived": False,
        "stargazerCount": 3,
        "createdAt": "2026-10-11T13:43:00Z",
        "pushedAt": "2026-10-11T18:00:00Z",
        "parent": None,
        "licenseInfo": {"spdxId": license} if license else None,
        "defaultBranchRef": {"name": "main", "target": {"oid": COMMIT}},
        "manifest": None if oid is None else {"oid": oid, "byteSize": len(text or ""), "isBinary": False, "text": text},
    }
    node.update(extra)
    return node


# --- code search ----------------------------------------------------------------------------


def test_code_search_query_and_exact_path_filter():
    items = [
        code_item("jbmv/recx", node_id="R_kgDOrecx"),
        code_item("someone/seq_rec", path="seq_rec/config/yoochoose-64/sasrecx.yaml"),
        code_item("someone/nested", path="docs/RECX.yaml"),
        code_item("someone/lower", path="recx.yaml"),
    ]
    client, transport, _ = make(queue(ok(search_page(items))))
    found = client.code_search()

    (call,) = transport.calls
    assert call["method"] == "GET"
    assert call["url"].startswith("https://api.github.com/search/code?")
    assert params(call["url"]) == {"q": "filename:RECX.yaml path:/", "per_page": "100", "page": "1"}
    assert found == [
        Candidate("jbmv/recx", frozenset({CODE_SEARCH}), node_id="R_kgDOrecx", blob_sha=BLOB, commit=COMMIT)
    ]


def test_requests_carry_the_api_headers():
    client, transport, _ = make(queue(ok(search_page([]))))
    client.code_search()
    headers = transport.calls[0]["headers"]
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["Authorization"] == "Bearer test-token"


def test_code_search_requests_are_at_least_6_5_seconds_apart():
    pages = [
        ok(search_page([code_item(f"o/r{i}") for i in range(100)], total=250)),
        ok(search_page([code_item(f"o/s{i}") for i in range(100)], total=250)),
        ok(search_page([code_item(f"o/t{i}") for i in range(50)], total=250)),
    ]
    client, transport, _ = make(queue(*pages))
    found = client.code_search()

    assert len(found) == 250
    assert [params(c["url"])["page"] for c in transport.calls] == ["1", "2", "3"]
    times = [c["at"] for c in transport.calls]
    assert all(b - a >= 6.5 for a, b in itertools.pairwise(times))


def test_pacing_also_applies_to_retries():
    responses = [err(429, "secondary rate limit", retry_after=1, x_ratelimit_remaining=10), ok(search_page([]))]
    client, transport, _ = make(queue(*responses))
    client.code_search()
    times = [c["at"] for c in transport.calls]
    assert times[1] - times[0] >= 6.5


def test_code_search_stops_at_the_1000_result_cap_with_a_note():
    def handler(method, url, payload):
        page = int(params(url)["page"])
        return ok(search_page([code_item(f"o/p{page}r{i}") for i in range(100)], total=4321))

    client, transport, _ = make(handler)
    found = client.code_search()
    assert len(transport.calls) == 10  # page 11 would be HTTP 422
    assert len(found) == 1000
    assert any("4321" in n and "1000" in n for n in client.notes)


# --- topic search, seeds, merging -----------------------------------------------------------


def test_topic_search_uses_topic_recx():
    repos = [{"id": 2, "node_id": "R_topic", "full_name": "alice/tool", "fork": False, "topics": ["recx"]}]
    client, transport, _ = make(queue(ok(search_page(repos))))
    found = client.topic_search()
    (call,) = transport.calls
    assert call["url"].startswith("https://api.github.com/search/repositories?")
    assert params(call["url"])["q"] == "topic:recx"
    assert found == [Candidate("alice/tool", frozenset({TOPIC}), node_id="R_topic")]


def test_seeds_file_lists_jbmv_recx():
    assert "jbmv/recx" in gh.read_seeds()


def test_read_seeds_skips_comments_and_blanks(tmp_path):
    path = tmp_path / "seeds.txt"
    path.write_text("# comment\n\njbmv/recx\n  alice/tool  # trailing\n")
    assert gh.read_seeds(path) == ["jbmv/recx", "alice/tool"]


def test_discover_reads_seeds_by_default():
    client, transport, _ = make(queue())
    found = client.discover(code=False, topic=False)
    assert transport.calls == []
    assert Candidate("jbmv/recx", frozenset({SEED})) in found


def test_discover_merges_channels_case_insensitively():
    code = ok(search_page([code_item("JBMV/recx", node_id="R_recx")]))
    topic = ok(search_page([{"node_id": "R_recx", "full_name": "jbmv/recx"}, {"node_id": "R_b", "full_name": "bob/b"}]))
    client, _, _ = make(queue(code, topic))
    found = client.discover(recheck=["jbmv/RECX", "carol/c"], seeds=["jbmv/recx"])

    assert [c.full_name for c in found] == ["bob/b", "carol/c", "JBMV/recx"]
    recx = found[2]
    assert recx.found_by == {CODE_SEARCH, TOPIC, SEED, RECHECK}
    assert (recx.node_id, recx.blob_sha, recx.commit) == ("R_recx", BLOB, COMMIT)
    assert found[1] == Candidate("carol/c", frozenset({RECHECK}))


def test_previous_state_reads_names_and_blobs():
    index = {"entries": [{"source": {"full_name": "jbmv/recx", "blob_sha": BLOB}, "manifest": {}}]}
    assert gh.previous_state(index) == (["jbmv/recx"], {"jbmv/recx": BLOB})
    assert gh.previous_state(None) == ([], {})


# --- rate limits ----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429])
def test_exhausted_limit_waits_until_reset(status):
    reset = int(T0) + 42
    responses = [
        err(status, "API rate limit exceeded", x_ratelimit_remaining=0, x_ratelimit_reset=reset),
        ok(search_page([])),
    ]
    client, transport, _ = make(queue(*responses))
    client.code_search()
    assert len(transport.calls) == 2
    assert transport.calls[1]["at"] >= reset
    assert transport.calls[1]["at"] - reset <= 2  # not much past it either


def test_retry_after_is_honoured():
    # The spike's shape: 429 on the first code search with quota left and a short retry-after.
    responses = [
        err(429, "secondary rate limit", retry_after=23, x_ratelimit_remaining=10, x_ratelimit_reset=int(T0) + 60),
        ok(search_page([])),
    ]
    client, transport, clock = make(queue(*responses))
    client.code_search()
    assert clock.sleeps == [23]
    assert transport.calls[1]["at"] - transport.calls[0]["at"] == 23


def test_rate_limit_without_headers_waits_60_seconds():
    client, _, clock = make(queue(err(429), ok(search_page([]))))
    client.topic_search()
    assert clock.sleeps == [60]


def test_exhausted_quota_on_success_delays_the_next_request_until_reset():
    reset = int(T0) + 30
    responses = [
        ok(search_page([{"node_id": "R_a", "full_name": "a/a"}] * 100, total=150), x_ratelimit_remaining=0, x_ratelimit_reset=reset),
        ok(search_page([{"node_id": "R_b", "full_name": "b/b"}] * 50, total=150)),
    ]
    client, transport, _ = make(queue(*responses))
    client.topic_search()
    assert transport.calls[1]["at"] >= reset


def test_five_consecutive_rate_limit_failures_raise():
    limited = err(429, retry_after=1)
    client, transport, _ = make(queue(*[limited] * 5))
    with pytest.raises(RateLimited) as exc:
        client.code_search()
    assert len(transport.calls) == 5
    assert isinstance(exc.value, GitHubError)  # one except clause covers every fatal error
    assert exc.value.status == 429


def test_four_rate_limit_failures_then_success_continue():
    limited = err(403, "You have exceeded a secondary rate limit", retry_after=2)
    client, transport, _ = make(queue(*[limited] * 4, ok(search_page([code_item("a/a")]))))
    assert len(client.code_search()) == 1
    assert len(transport.calls) == 5


def test_forbidden_without_rate_limit_signs_is_an_error():
    client, transport, _ = make(queue(err(403, "Resource not accessible by integration", x_ratelimit_remaining=9)))
    with pytest.raises(GitHubError) as exc:
        client.code_search()
    assert not isinstance(exc.value, RateLimited)
    assert exc.value.status == 403
    assert len(transport.calls) == 1


def test_other_errors_carry_the_status():
    client, _, _ = make(queue(err(422, "Validation Failed")))
    with pytest.raises(GitHubError) as exc:
        client.topic_search()
    assert exc.value.status == 422


def test_graphql_rate_limited_error_is_retried_after_reset():
    reset = int(T0) + 100
    limited = ok({"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
                 x_ratelimit_remaining=0, x_ratelimit_reset=reset)
    good = ok({"data": {"nodes": [repo_node("a/a")]}})
    client, transport, _ = make(queue(limited, good))
    result = client.fetch([Candidate("a/a", frozenset({TOPIC}), node_id="R_a")], {})
    assert result["a/a"].exists
    assert transport.calls[1]["at"] >= reset


# --- incomplete results ---------------------------------------------------------------------


def test_incomplete_results_retried_once_then_accepted():
    responses = [
        ok(search_page([code_item("a/a")], incomplete=True)),
        ok(search_page([code_item("a/a"), code_item("b/b")])),
    ]
    client, transport, _ = make(queue(*responses))
    found = client.code_search()
    assert len(transport.calls) == 2
    assert {c.full_name for c in found} == {"a/a", "b/b"}
    assert not client.partial


def test_incomplete_results_retried_only_once():
    page = ok(search_page([], incomplete=True))
    client, transport, _ = make(queue(page, page, page))
    client.topic_search()
    assert len(transport.calls) == 2
    assert client.partial
    assert any("partial" in n for n in client.notes)


# --- fetching -------------------------------------------------------------------------------


def graphql_handler(repos, errors_for=()):
    """Answers nodes(ids) and aliased repository() queries from `repos`, keyed by id or name."""

    def handler(method, url, payload):
        assert method == "POST" and url == "https://api.github.com/graphql"
        variables = payload["variables"]
        errors = []
        if "ids" in variables:
            nodes = []
            for i, node_id in enumerate(variables["ids"]):
                nodes.append(repos.get(node_id))
                if node_id not in repos:
                    errors.append({"type": "NOT_FOUND", "path": ["nodes", i], "message": "Could not resolve"})
            body = {"data": {"nodes": nodes}}
        else:
            data = {}
            for i in range(len(variables) // 2):
                name = f"{variables[f'o{i}']}/{variables[f'n{i}']}"
                data[f"r{i}"] = repos.get(name)
                if name in errors_for:
                    errors.append({"type": errors_for[name], "path": [f"r{i}"], "message": "x"})
                elif name not in repos:
                    errors.append({"type": "NOT_FOUND", "path": [f"r{i}"], "message": "Could not resolve"})
            body = {"data": data}
        if errors:
            body["errors"] = errors
        return ok(body)

    return handler


def test_fetch_uses_graphql_batches_of_at_most_50():
    by_id = [Candidate(f"o/r{i}", frozenset({TOPIC}), node_id=f"R_{i}") for i in range(120)]
    by_name = [Candidate(f"o/s{i}", frozenset({SEED})) for i in range(60)]
    repos = {f"R_{i}": repo_node(f"o/r{i}") for i in range(120)}
    repos.update({f"o/s{i}": repo_node(f"o/s{i}") for i in range(60)})
    client, transport, _ = make(graphql_handler(repos))
    result = client.fetch(by_id + by_name, {})

    assert len(result) == 180
    sizes = []
    for call in transport.calls:
        variables = call["payload"]["variables"]
        sizes.append(len(variables["ids"]) if "ids" in variables else len(variables) // 2)
    assert sorted(sizes) == [10, 20, 50, 50, 50]
    query = transport.calls[0]["payload"]["query"]
    for field in ("nodes(ids: $ids)", 'object(expression: "HEAD:RECX.yaml")', "oid byteSize isBinary text",
                  "licenseInfo { spdxId }", "defaultBranchRef { name target { oid } }", "parent { nameWithOwner }",
                  "stargazerCount", "createdAt", "pushedAt", "isFork", "isArchived"):
        assert field in query
    name_query = transport.calls[-1]["payload"]["query"]
    assert "r0: repository(owner: $o0, name: $n0)" in name_query
    assert transport.calls[-1]["payload"]["variables"]["o0"] == "o"


def test_fetch_maps_repository_fields():
    fork = repo_node("bob/recx", license="NOASSERTION", isFork=True, parent={"nameWithOwner": "jbmv/recx"},
                     isArchived=True, stargazerCount=7)
    client, _, _ = make(graphql_handler({"R_bob": fork}))
    (fetched,) = client.fetch([Candidate("bob/recx", frozenset({TOPIC}), node_id="R_bob")], {}).values()
    assert fetched == gh.Fetched(
        full_name="bob/recx", exists=True, default_branch="main", commit=COMMIT, blob_sha=BLOB,
        byte_size=len("recx: '0.1'\n"), text="recx: '0.1'\n", unchanged=False, is_fork=True,
        parent="jbmv/recx", is_archived=True, stars=7, created_at="2026-10-11T13:43:00Z",
        pushed_at="2026-10-11T18:00:00Z", detected_license="NOASSERTION",
    )


def test_fetch_without_manifest_or_licence():
    client, _, _ = make(graphql_handler({"a/a": repo_node("a/a", oid=None, license=None)}))
    fetched = client.fetch([Candidate("a/a", frozenset({SEED}))], {})["a/a"]
    assert fetched.exists and fetched.blob_sha is None and fetched.text is None
    assert fetched.detected_license is None


def test_unchanged_code_search_blob_is_not_reread():
    cand = Candidate("jbmv/recx", frozenset({CODE_SEARCH}), node_id="R_recx", blob_sha=BLOB, commit=COMMIT)
    client, transport, _ = make(graphql_handler({"R_recx": repo_node("jbmv/recx", text=None)}))
    fetched = client.fetch([cand], {"jbmv/recx": BLOB})["jbmv/recx"]

    (call,) = transport.calls
    assert " text" not in call["payload"]["query"]  # the light query does not ask for the file
    assert "oid byteSize isBinary" in call["payload"]["query"]
    assert fetched.unchanged and fetched.text is None
    assert fetched.blob_sha == BLOB and fetched.commit == COMMIT and fetched.exists


def test_stale_code_search_blob_is_reread_with_text():
    cand = Candidate("jbmv/recx", frozenset({CODE_SEARCH}), node_id="R_recx", blob_sha=BLOB)
    client, transport, _ = make(graphql_handler({"R_recx": repo_node("jbmv/recx", oid=NEW_BLOB, text="new\n")}))
    fetched = client.fetch([cand], {"jbmv/recx": BLOB})["jbmv/recx"]
    assert len(transport.calls) == 2
    assert " text" in transport.calls[1]["payload"]["query"]
    assert (fetched.blob_sha, fetched.text, fetched.unchanged) == (NEW_BLOB, "new\n", False)


def test_recheck_with_unchanged_oid_drops_the_text():
    client, _, _ = make(graphql_handler({"jbmv/recx": repo_node("jbmv/recx")}))
    fetched = client.fetch([Candidate("jbmv/recx", frozenset({RECHECK}))], {"jbmv/recx": BLOB})["jbmv/recx"]
    assert fetched.unchanged and fetched.text is None and fetched.blob_sha == BLOB


def test_changed_blob_keeps_the_text():
    client, _, _ = make(graphql_handler({"jbmv/recx": repo_node("jbmv/recx", oid=NEW_BLOB, text="x\n")}))
    fetched = client.fetch([Candidate("jbmv/recx", frozenset({RECHECK}))], {"jbmv/recx": BLOB})["jbmv/recx"]
    assert not fetched.unchanged and fetched.text == "x\n"


def test_unresolvable_repositories_do_not_exist():
    cands = [
        Candidate("gone/by-name", frozenset({RECHECK})),
        Candidate("gone/by-id", frozenset({TOPIC}), node_id="R_gone"),
    ]
    client, _, _ = make(graphql_handler({}))
    result = client.fetch(cands, {})
    assert result["gone/by-name"] == gh.Fetched(full_name="gone/by-name", exists=False)
    assert result["gone/by-id"] == gh.Fetched(full_name="gone/by-id", exists=False)


def test_other_graphql_errors_leave_the_candidate_out():
    cands = [Candidate("org/saml", frozenset({RECHECK})), Candidate("a/a", frozenset({RECHECK}))]
    client, _, _ = make(graphql_handler({"a/a": repo_node("a/a")}, errors_for={"org/saml": "FORBIDDEN"}))
    result = client.fetch(cands, {})
    assert set(result) == {"a/a"}
    assert any("org/saml" in n and "FORBIDDEN" in n for n in client.notes)


def test_renamed_repository_is_treated_as_gone():
    client, _, _ = make(graphql_handler({"old/name": repo_node("new/name")}))
    result = client.fetch([Candidate("old/name", frozenset({RECHECK}))], {})
    assert result["old/name"].exists is False


def test_failed_batch_is_left_out_with_a_note():
    cands = [Candidate(f"o/r{i}", frozenset({TOPIC}), node_id=f"R_{i}") for i in range(60)]
    repos = {f"R_{i}": repo_node(f"o/r{i}") for i in range(60)}
    inner = graphql_handler(repos)
    calls = []

    def handler(method, url, payload):
        calls.append(1)
        if len(calls) == 1:
            return OSError("connection reset by peer")
        return inner(method, url, payload)

    client, _, _ = make(handler)
    result = client.fetch(cands, {})
    assert set(result) == {f"o/r{i}" for i in range(50, 60)}
    assert any("batch of 50 skipped" in n for n in client.notes)


def test_rate_limit_in_a_batch_ends_the_run():
    client, _, _ = make(queue(*[err(429, retry_after=1)] * 5))
    with pytest.raises(RateLimited):
        client.fetch([Candidate("a/a", frozenset({SEED}))], {})


def test_fetch_result_is_keyed_by_candidate_name():
    client, _, _ = make(graphql_handler({"jbmv/RECX": repo_node("jbmv/recx")}))
    result = client.fetch([Candidate("jbmv/RECX", frozenset({SEED}))], {})
    assert result["jbmv/RECX"].full_name == "jbmv/recx"  # GitHub's spelling inside
