"""crawler/__main__.py: guards and wiring, with no network."""

import json
import urllib.error
from pathlib import Path

import jsonschema
import pytest

from crawler import __main__ as cli
from crawler.model import Candidate, Fetched
from tests.test_crawler_core import fixture

INDEX_SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "spec" / "index.schema.json").read_text())
ENV = {"GITHUB_TOKEN": "x"}


class FakeClient:
    def __init__(self, token, partial=False, error=None):
        self.notes = ["client note"]
        self.partial = partial
        self.error = error

    def discover(self, recheck=(), seeds=None, *, code=True, topic=True):
        if self.error:
            raise self.error
        return [Candidate("o/r", frozenset({"seed"}))] + [
            Candidate(n, frozenset({"recheck"})) for n in recheck]

    def fetch(self, candidates, known_blobs):
        text = fixture("minimal.yaml")
        gone = {c.full_name: Fetched(c.full_name, exists=False) for c in candidates if c.full_name != "o/r"}
        return {**gone, "o/r": Fetched(
            "o/r", exists=True, default_branch="main", commit="a" * 40, blob_sha="b" * 40,
            byte_size=len(text), text=text, stars=1, created_at="2025-01-01T00:00:00Z",
            pushed_at="2026-10-01T00:00:00Z", detected_license="MIT")}


def run(tmp_path, *, prev=None, factory=FakeClient, env=ENV, fetch=None):
    return cli.main(
        ["--out", str(tmp_path), "--now", "2026-10-12T04:17:00Z"],
        fetch_previous=fetch or (lambda url: prev), client_factory=factory, env=env,
    )


@pytest.mark.parametrize("old,new,tripped", [(20, 13, True), (20, 14, False), (10, 4, True),
                                             (10, 5, False), (0, 0, False), (20, 25, False), (3, 0, False)])
def test_shrink_guard(old, new, tripped):
    assert cli.shrink_tripped(old, new) is tripped


def test_exit_status():
    assert cli.exit_status(partial=True, previous_count=1, new_count=1) == 3
    assert cli.exit_status(partial=False, previous_count=20, new_count=13) == 4
    assert cli.exit_status(partial=False, previous_count=20, new_count=14) == 0


def test_full_run_writes_valid_files(tmp_path, capsys):
    assert run(tmp_path) == 0
    index = json.loads((tmp_path / "v1" / "index.json").read_text())
    jsonschema.validate(index, INDEX_SCHEMA)
    assert [e["source"]["full_name"] for e in index["entries"]] == ["o/r"]
    assert index["generator"].startswith("recx-crawler (recx-cli ")
    assert (tmp_path / "v1" / "index.json").read_text().endswith("}\n")
    assert json.loads((tmp_path / "v1" / "rejected.json").read_text())["rejected"] == []
    out = capsys.readouterr().out
    assert "client note" in out and "indexed: 1" in out and "first run" in out


def test_partial_exits_3_but_writes(tmp_path):
    assert run(tmp_path, factory=lambda t: FakeClient(t, partial=True)) == 3
    assert (tmp_path / "v1" / "index.json").exists()


def test_shrink_exits_4(tmp_path):
    assert run(tmp_path / "first") == 0
    one = json.loads((tmp_path / "first" / "v1" / "index.json").read_text())["entries"][0]
    entries = []
    for i in range(20):
        e = json.loads(json.dumps(one))
        e["source"]["full_name"] = f"p/r{i}"
        e["source"]["blob_sha"] = f"{i:040x}"
        entries.append(e)
    assert run(tmp_path / "second", prev={"entries": entries}) == 4


def test_github_error_exits_1(tmp_path):
    assert run(tmp_path, factory=lambda t: FakeClient(t, error=cli.GitHubError(500, "u"))) == 1
    assert not (tmp_path / "v1").exists()


def test_missing_token_exits_2(tmp_path):
    assert run(tmp_path, env={}) == 2
    assert not (tmp_path / "v1").exists()


def test_previous_error_exits_2_and_writes_nothing(tmp_path):
    def boom(url):
        raise cli.PreviousError("HTTP 500")
    assert run(tmp_path, fetch=boom) == 2
    assert not (tmp_path / "v1").exists()


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code):
    return urllib.error.HTTPError("u", code, "m", {}, None)


def test_fetch_previous_404_is_first_run(monkeypatch):
    def opener(url, timeout):
        raise _http_error(404)
    monkeypatch.setattr(cli.urllib.request, "urlopen", opener)
    assert cli.fetch_previous("u") is None


def test_fetch_previous_other_errors_raise(monkeypatch):
    def opener(url, timeout):
        raise _http_error(503)
    monkeypatch.setattr(cli.urllib.request, "urlopen", opener)
    with pytest.raises(cli.PreviousError):
        cli.fetch_previous("u")
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda url, timeout: _Resp(b"not json"))
    with pytest.raises(cli.PreviousError):
        cli.fetch_previous("u")
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda url, timeout: _Resp(b'{"entries": []}'))
    assert cli.fetch_previous("u") == {"entries": []}
