"""crawler/core.py: offline decisions, with constructed Candidate and Fetched values only."""

import ast
import hashlib
import json
import socket
from pathlib import Path

import jsonschema
import pytest
import yaml

from crawler.core import build, load_list, reason_codes
from crawler.model import CODE_SEARCH, RECHECK, SEED, TOPIC, Candidate, Fetched
from recx_cli.check import check_text

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec"
VALID = SPEC / "examples" / "valid"
INDEX_SCHEMA = json.loads((SPEC / "index.schema.json").read_text())

T0 = "2026-10-12T04:21:09Z"
T1 = "2026-10-13T04:17:00Z"
GEN = "recx-crawler (test)"
CODES = {"schema", "rule-1", "rule-2", "rule-5", "rule-6", "yaml", "utf-8"}


def sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()


def fixture(name: str) -> str:
    return (VALID / name).read_text()


def variant(name: str, **changes) -> str:
    data = yaml.safe_load(fixture(name))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return yaml.safe_dump(data, sort_keys=False)


def cand(full_name: str, *found_by: str, **kw) -> Candidate:
    return Candidate(full_name, frozenset(found_by or {CODE_SEARCH}), **kw)


_MINIMAL = object()


def fetch(full_name: str, text=_MINIMAL, **kw) -> Fetched:
    """A fetched record for an existing repository whose manifest is `text` (minimal.yaml by default)."""
    if text is _MINIMAL:
        text = None if kw.get("unchanged") else fixture("minimal.yaml")
    defaults = dict(
        exists=True,
        default_branch="main",
        commit=sha("commit:" + full_name),
        blob_sha=sha("blob:" + (text or full_name)),
        byte_size=len(text.encode()) if text else None,
        text=text,
        stars=2,
        created_at="2025-01-01T00:00:00Z",
        pushed_at="2026-10-01T00:00:00Z",
        detected_license="MIT",
    )
    defaults.update(kw)
    return Fetched(full_name, **defaults)


def run(cands, fetched, previous=None, *, now=T0, blocklist=()):
    return build(
        previous,
        list(cands),
        {f.full_name: f for f in fetched},
        now=now,
        blocklist=set(blocklist),
        generator=GEN,
    )


def entry(result, full_name):
    found = [e for e in result.index["entries"] if e["source"]["full_name"] == full_name]
    return found[0] if found else None


def names(result):
    return [e["source"]["full_name"] for e in result.index["entries"]]


def rejections(result):
    return {r["full_name"]: r["reasons"] for r in result.rejected["rejected"]}


def validate_index(doc):
    jsonschema.Draft202012Validator(
        INDEX_SCHEMA, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    ).validate(json.loads(json.dumps(doc)))


# -- purity -------------------------------------------------------------------


def test_build_returns_index_and_rejections_without_network(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    result = run(
        [cand("a/good"), cand("b/bad")],
        [fetch("a/good"), fetch("b/bad", "recx: [")],
    )
    assert names(result) == ["a/good"]
    assert rejections(result) == {"b/bad": ["yaml"]}
    assert result.index["format"] == "recx-index/1.0"
    assert result.rejected["format"] == "recx-rejected/1.0"
    assert result.rejected["generated_at"] == T0


def test_core_imports_no_network_or_process_modules():
    tree = ast.parse((ROOT / "crawler" / "core.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    tops = {m.split(".")[0] for m in imported}
    assert not tops & {"socket", "urllib", "http", "requests", "httpx", "subprocess", "ssl"}
    assert "crawler.github" not in imported


# -- invalid manifests --------------------------------------------------------


@pytest.mark.parametrize(
    "path, code",
    [
        ("invalid-rules/rule1-duplicate-module-id.yaml", "rule-1"),
        ("invalid-rules/rule1-duplicate-evidence-id.yaml", "rule-1"),
        ("invalid-rules/rule2-covers-unknown-module.yaml", "rule-2"),
        ("invalid-rules/rule2-requires-unknown-module.yaml", "rule-2"),
        ("invalid-rules/rule5-over-64-kib.yaml", "rule-5"),
        ("invalid-rules/rule6-anchor-alias.yaml", "rule-6"),
        ("invalid-rules/rule6-duplicate-key.yaml", "rule-6"),
        ("invalid-rules/rule6-merge-key.yaml", "rule-6"),
        ("invalid/unknown-key.yaml", "schema"),
        ("invalid/missing-capabilities.yaml", "schema"),
        ("invalid/license-not-permissive.yaml", "schema"),
    ],
)
def test_invalid_manifest_is_rejected_with_a_code_and_no_message(path, code):
    text = (SPEC / "examples" / path).read_text()
    problems = check_text(text)
    assert problems
    result = run([cand("x/bad")], [fetch("x/bad", text)])
    assert entry(result, "x/bad") is None
    assert result.rejected["rejected"] == [{"full_name": "x/bad", "reasons": [code]}]
    dumped = json.dumps(result.rejected) + json.dumps(result.index) + "\n".join(result.notes)
    for p in problems:
        assert p.message not in dumped


def test_yaml_syntax_error_is_coded_yaml():
    result = run([cand("x/bad")], [fetch("x/bad", "recx: \"0.1\nname: [")])
    assert rejections(result) == {"x/bad": ["yaml"]}


def test_codes_come_from_the_marker_at_the_end_not_from_author_text():
    def module():  # a fresh dict each time, or safe_dump writes an anchor and alias (rule 6)
        return {"id": "x (rule 5)", "path": "a.py", "summary": "s",
                "interface": {"type": "library", "entrypoints": ["f()"]}, "extract": {"method": "copy"}}

    text = variant("full.yaml", modules=[module(), module()], evidence=None)
    result = run([cand("x/bad")], [fetch("x/bad", text)])
    assert rejections(result) == {"x/bad": ["rule-1", "schema"]}


def test_reason_codes_are_from_the_fixed_set():
    from recx_cli.check import Problem

    got = reason_codes([
        Problem("/", "file is not valid UTF-8"),
        Problem("/", "line 3: invalid YAML: mapping values are not allowed here"),
        Problem("/", "line 2: aliases (*name) are not allowed (SPEC.md rule 6)"),
        Problem("/x", "'(rule 2)' is not of type 'integer'"),
    ])
    assert got == ["rule-6", "schema", "utf-8", "yaml"]
    assert set(got) <= CODES


def test_deeply_nested_yaml_is_rejected_not_raised():
    result = run([cand("x/deep")], [fetch("x/deep", "[" * 20000)])
    assert rejections(result) == {"x/deep": ["yaml"]}


def test_binary_or_oversized_blob_without_text_is_rejected():
    result = run(
        [cand("x/bin"), cand("x/big")],
        [
            fetch("x/bin", None, blob_sha=sha("bin"), byte_size=10),
            fetch("x/big", None, blob_sha=sha("big"), byte_size=70000),
        ],
    )
    assert rejections(result) == {"x/big": ["rule-5"], "x/bin": ["utf-8"]}


# -- licences -----------------------------------------------------------------


def test_detected_licence_not_on_allowlist_is_rejected():
    result = run([cand("x/gpl")], [fetch("x/gpl", detected_license="GPL-3.0")])
    assert entry(result, "x/gpl") is None
    assert rejections(result) == {"x/gpl": ["license-not-permissive"]}


def test_different_allowlisted_licence_is_indexed_as_mismatch():
    result = run([cand("x/apache")], [fetch("x/apache", detected_license="Apache-2.0")])
    src = entry(result, "x/apache")["source"]
    assert src["license_check"] == "mismatch"
    assert src["detected_license"] == "Apache-2.0"
    assert rejections(result) == {}


@pytest.mark.parametrize("detected", [None, "NOASSERTION", "OTHER", "other", "NoAssertion"])
def test_undetected_licence_is_indexed_as_undetected(detected):
    result = run([cand("x/none")], [fetch("x/none", detected_license=detected)])
    assert entry(result, "x/none")["source"]["license_check"] == "undetected"
    assert entry(result, "x/none")["source"]["detected_license"] == detected


def test_equal_licence_is_match():
    result = run([cand("x/mit")], [fetch("x/mit", detected_license="MIT")])
    assert entry(result, "x/mit")["source"]["license_check"] == "match"


def test_allowlist_is_read_from_the_spec_schema(monkeypatch):
    from crawler import core

    monkeypatch.setattr(core, "license_allowlist", lambda: frozenset({"MIT"}))
    result = run([cand("x/apache")], [fetch("x/apache", detected_license="Apache-2.0")])
    assert rejections(result) == {"x/apache": ["license-not-permissive"]}
    real = json.loads((SPEC / "recx.schema.json").read_text())["$defs"]["license"]["enum"]
    monkeypatch.undo()
    assert core.license_allowlist() == frozenset(real)


# -- previous entries: kept, refreshed, removed --------------------------------


def first_run():
    return run(
        [cand("a/one", SEED), cand("b/two", CODE_SEARCH)],
        [fetch("a/one"), fetch("b/two", fixture("full.yaml"), detected_license="Apache-2.0")],
    )


def test_previous_entry_absent_from_search_but_valid_on_recheck_is_kept():
    prev = first_run().index
    result = run(
        [cand("a/one", RECHECK), cand("b/two", RECHECK)],
        [fetch("a/one"), fetch("b/two", fixture("full.yaml"), detected_license="Apache-2.0")],
        prev,
        now=T1,
    )
    assert names(result) == ["a/one", "b/two"]
    src = entry(result, "a/one")["source"]
    assert src["first_indexed"] == T0
    assert src["last_verified"] == T1
    assert src["found_by"] == ["recheck"]


def test_previous_entry_not_passed_as_candidate_is_still_rechecked():
    prev = first_run().index
    result = run([], [fetch("a/one", stars=9)], prev, now=T1)
    src = entry(result, "a/one")["source"]
    assert src["stars"] == 9 and src["found_by"] == ["recheck"] and src["last_verified"] == T1
    # b/two was neither a candidate nor fetched: kept unchanged.
    assert entry(result, "b/two") == entry(first_run(), "b/two")


def test_unchanged_blob_reuses_projection_and_refreshes_metadata():
    prev = first_run().index
    old = entry(first_run(), "b/two")
    f = fetch(
        "b/two", None, unchanged=True, blob_sha=old["source"]["blob_sha"],
        commit=sha("newer commit"), stars=40, is_archived=True, detected_license="Apache-2.0",
    )
    result = run([cand("b/two", CODE_SEARCH, TOPIC)], [f], prev, now=T1)
    new = entry(result, "b/two")
    assert new["manifest"] == old["manifest"]
    assert new["source"]["commit"] == sha("newer commit")
    assert new["source"]["manifest_url"].endswith(f"/{sha('newer commit')}/RECX.yaml")
    assert new["source"]["stars"] == 40 and new["source"]["is_archived"] is True
    assert new["source"]["found_by"] == ["code-search", "topic"]
    assert new["source"]["first_indexed"] == T0 and new["source"]["last_verified"] == T1


def test_changed_manifest_moves_forward():
    prev = first_run().index
    text = variant("minimal.yaml", summary="A new summary for the same project.")
    result = run([cand("a/one")], [fetch("a/one", text)], prev, now=T1)
    new = entry(result, "a/one")
    assert new["manifest"]["summary"] == "A new summary for the same project."
    assert new["source"]["blob_sha"] == sha("blob:" + text)
    assert new["source"]["first_indexed"] == T0


@pytest.mark.parametrize("gone", [dict(exists=False), dict(blob_sha=None, text=None)])
def test_previous_entry_whose_file_is_gone_is_removed(gone):
    prev = first_run().index
    result = run([cand("a/one", RECHECK)], [fetch("a/one", **gone)], prev, now=T1)
    assert entry(result, "a/one") is None
    assert "a/one" not in rejections(result)  # removal is not a rejection
    assert any(n.startswith("removed a/one") for n in result.notes)


def test_new_candidate_without_a_file_is_skipped_silently():
    result = run([cand("x/gone"), cand("x/nofile")], [fetch("x/gone", exists=False), fetch("x/nofile", blob_sha=None)])
    assert names(result) == [] and rejections(result) == {}


def test_previous_entry_now_invalid_is_removed_and_rejected():
    prev = first_run().index
    result = run([cand("a/one")], [fetch("a/one", "recx: [")], prev, now=T1)
    assert entry(result, "a/one") is None
    assert rejections(result)["a/one"] == ["yaml"]


def test_previous_entry_whose_fetch_failed_is_kept_unchanged():
    first = first_run()
    result = run(
        [cand("a/one", RECHECK), cand("b/two", CODE_SEARCH)],
        [fetch("b/two", fixture("full.yaml"), detected_license="Apache-2.0")],  # a/one missing
        first.index,
        now=T1,
    )
    assert entry(result, "a/one") == entry(first, "a/one")
    assert entry(result, "a/one")["source"]["last_verified"] == T0
    assert any("a/one" in n and "unchanged" in n for n in result.notes)


def test_new_candidate_whose_fetch_failed_is_skipped_with_a_note():
    result = run([cand("x/new")], [])
    assert names(result) == [] and rejections(result) == {}
    assert any(n.startswith("skipped x/new") for n in result.notes)


# -- forks ----------------------------------------------------------------------


@pytest.mark.parametrize("order", [1, -1])
def test_fork_with_parents_blob_is_dropped(order):
    text = fixture("minimal.yaml")
    cands = [cand("up/proj"), cand("me/proj"), cand("zz/proj")][::order]
    fetched = [
        fetch("up/proj", text),
        fetch("me/proj", text, is_fork=True, parent="up/proj"),
        fetch("zz/proj", text, is_fork=True, parent="UP/Proj"),
    ][::order]
    result = run(cands, fetched)
    assert names(result) == ["up/proj"]
    assert rejections(result) == {"me/proj": ["fork-duplicate"], "zz/proj": ["fork-duplicate"]}


def test_fork_with_its_own_manifest_is_kept():
    other = variant("minimal.yaml", summary="The fork's own changed summary line.")
    result = run(
        [cand("up/proj"), cand("me/proj"), cand("lone/fork")],
        [
            fetch("up/proj"),
            fetch("me/proj", other, is_fork=True, parent="up/proj"),
            fetch("lone/fork", is_fork=True, parent="not/indexed"),
        ],
    )
    assert names(result) == ["lone/fork", "me/proj", "up/proj"]
    assert entry(result, "me/proj")["source"]["parent"] == "up/proj"
    assert entry(result, "up/proj")["source"]["parent"] is None


# -- blocklist ----------------------------------------------------------------


def test_load_list_skips_comments_and_blanks_and_lowercases(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text("# heading\n\nSpam-Owner\n  Some/Repo  # trailing note\n#Other/Thing\n")
    assert load_list(p) == {"spam-owner", "some/repo"}
    assert load_list(ROOT / "crawler" / "seeds.txt") == {"jbmv/recx"}
    assert isinstance(load_list(ROOT / "crawler" / "blocklist.txt"), set)


def test_blocklisted_repository_or_owner_is_never_indexed(tmp_path):
    p = tmp_path / "blocklist.txt"
    p.write_text((ROOT / "crawler" / "blocklist.txt").read_text() + "Spam-Owner\nsome/Repo\n")
    blocklist = load_list(p)
    prev = run([cand("some/repo")], [fetch("some/repo")]).index  # indexed before it was blocked
    result = run(
        [cand("spam-owner/a"), cand("SOME/REPO"), cand("some/other")],
        [fetch("spam-owner/a"), fetch("some/other")],
        prev,
        blocklist=blocklist,
    )
    assert names(result) == ["some/other"]
    # The previous entry's spelling (GitHub's) names the rejection.
    assert rejections(result) == {"some/repo": ["blocked"], "spam-owner/a": ["blocked"]}
    assert any(n.startswith("removed some/repo") for n in result.notes)


# -- shape of the output ------------------------------------------------------


def test_candidates_are_merged_case_insensitively():
    result = run(
        [cand("Ab/Cd", CODE_SEARCH), cand("ab/cd", TOPIC), cand("AB/CD", SEED)],
        [fetch("Ab/Cd")],
    )
    assert names(result) == ["Ab/Cd"]
    assert entry(result, "Ab/Cd")["source"]["found_by"] == ["code-search", "seed", "topic"]


def test_entries_and_rejections_are_sorted_by_full_name():
    order = ["m/m", "a/z", "z/a", "b/b", "c/c"]
    bad = ["y/bad", "d/bad"]
    result = run(
        [cand(n) for n in order + bad],
        [fetch(n) for n in order] + [fetch(n, "recx: [") for n in bad],
    )
    assert names(result) == sorted(order)
    assert [r["full_name"] for r in result.rejected["rejected"]] == sorted(bad)


@pytest.mark.parametrize(
    "repository, expected",
    [
        (None, "absent"),
        ("https://github.com/Ex/Proj", "matches"),
        ("https://github.com/ex/proj/", "matches"),
        ("https://github.com/ex/proj.git", "matches"),
        ("https://github.com/ex/other", "differs"),
    ],
)
def test_declared_repository(repository, expected):
    text = variant("minimal.yaml", repository=repository)
    result = run([cand("ex/proj")], [fetch("ex/proj", text)])
    assert entry(result, "ex/proj")["source"]["declared_repository"] == expected


def test_source_fields_and_header():
    result = first_run()
    src = entry(result, "b/two")["source"]
    commit = sha("commit:b/two")
    assert src["url"] == "https://github.com/b/two"
    assert src["manifest_url"] == f"https://raw.githubusercontent.com/b/two/{commit}/RECX.yaml"
    assert src["first_indexed"] == src["last_verified"] == T0
    assert src["declared_repository"] == "differs"  # full.yaml names example/feedwatch
    assert result.index["generated_at"] == T0
    assert result.index["generator"] == GEN
    assert result.index["spec_versions"] == ["0.1"]
    assert result.index["counts"] == {"entries": 2, "rejected": 0}
    assert all(isinstance(n, str) for n in result.notes)


def test_projection_drops_commands_and_extensions():
    m = entry(first_run(), "b/two")["manifest"]
    assert all("run" not in e and "setup" not in e for e in m["evidence"])
    assert not [k for k in m if k.startswith("x-")]


def test_produced_index_validates_against_the_index_schema():
    first = first_run()
    validate_index(first.index)
    result = run(
        [
            cand("a/one", RECHECK), cand("b/two", CODE_SEARCH), cand("c/apache"), cand("d/none"),
            cand("e/fork"), cand("f/samefork"), cand("g/bad"), cand("h/gpl"), cand("i/gone"),
        ],
        [
            fetch("b/two", None, unchanged=True,
                  blob_sha=entry(first, "b/two")["source"]["blob_sha"], detected_license="Apache-2.0"),
            fetch("c/apache", detected_license="Apache-2.0"),
            fetch("d/none", variant("full.yaml", repository="https://github.com/d/none.git"),
                  detected_license=None, is_archived=True),
            fetch("e/fork", variant("minimal.yaml", tags=["fork"]), is_fork=True, parent="c/apache"),
            fetch("f/samefork", is_fork=True, parent="c/apache"),
            fetch("g/bad", "recx: ["),
            fetch("h/gpl", detected_license="GPL-2.0"),
            fetch("i/gone", exists=False),
        ],
        first.index,
        now=T1,
    )
    assert names(result) == ["a/one", "b/two", "c/apache", "d/none", "e/fork"]
    assert result.index["counts"] == {"entries": 5, "rejected": 3}
    validate_index(result.index)
