import copy
import hashlib
import json
import math
from pathlib import Path

import jsonschema
import pytest
import yaml

from recx_cli.check import check_file
from recx_cli.index import project
from recx_cli.match import B, K1, MAX_LIMIT, STOPWORDS, WEIGHTS, Match, rank, terms

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "search"
CASES = yaml.safe_load((ROOT / "tests" / "search_cases.yaml").read_text())
INDEX_SCHEMA = json.loads((ROOT / "spec" / "index.schema.json").read_text())


def source(full_name: str, text: str) -> dict:
    digest = hashlib.sha1(text.encode()).hexdigest()
    commit = hashlib.sha1(("commit:" + full_name).encode()).hexdigest()
    return {
        "full_name": full_name,
        "url": f"https://github.com/{full_name}",
        "default_branch": "main",
        "commit": commit,
        "blob_sha": digest,
        "manifest_url": f"https://raw.githubusercontent.com/{full_name}/{commit}/RECX.yaml",
        "detected_license": None,
        "license_check": "undetected",
        "declared_repository": "absent",
        "is_fork": False,
        "parent": None,
        "is_archived": False,
        "stars": len(full_name) * 7,
        "repo_created_at": "2025-03-01T12:00:00Z",
        "pushed_at": "2026-10-01T12:00:00Z",
        "found_by": ["code-search"],
        "first_indexed": "2026-10-12T04:21:09Z",
        "last_verified": "2026-10-12T04:21:09Z",
    }


def entry(full_name: str, manifest: dict) -> dict:
    src = source(full_name, json.dumps(manifest, sort_keys=True))
    src["detected_license"] = manifest["license"]
    src["license_check"] = "match"
    return {"source": src, "manifest": project(manifest)}


def build_index() -> list[dict]:
    out = []
    for stem, full_name in CASES["index"].items():
        manifest = yaml.safe_load((FIXTURES / f"{stem}.yaml").read_text())
        out.append(entry(full_name, manifest))
    return sorted(out, key=lambda e: e["source"]["full_name"])


INDEX = build_index()


def names(matches: list[Match]) -> list[str]:
    return [m.entry["source"]["full_name"] for m in matches]


def scored(matches: list[Match]) -> list[tuple[str, float]]:
    return [(m.entry["source"]["full_name"], m.score) for m in matches]


def minimal(name: str, capabilities: list[str], **extra) -> dict:
    manifest = {"recx": "0.1", "name": name, "summary": f"The {name} project.", "license": "MIT",
                "status": "active", "capabilities": capabilities, **extra}
    return entry(f"example/{name}", manifest)


# --- The fixture index -------------------------------------------------------------------


def test_fixture_index_has_at_least_15_manifests_and_20_cases():
    assert len(INDEX) >= 15
    assert len(CASES["cases"]) >= 20
    assert sorted(p.stem for p in FIXTURES.glob("*.yaml")) == sorted(CASES["index"])


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.yaml")), ids=lambda p: p.stem)
def test_fixture_is_a_valid_manifest(path):
    assert check_file(path, repo=False) == []


def test_fixture_index_validates_against_index_schema():
    index = {"format": "recx-index/1.0", "generated_at": "2026-10-12T04:21:09Z",
             "generator": "tests", "spec_versions": ["0.1"],
             "counts": {"entries": len(INDEX), "rejected": 0}, "entries": INDEX}
    jsonschema.Draft202012Validator(INDEX_SCHEMA).validate(index)


# --- Search cases ------------------------------------------------------------------------


def _case_params():
    return [
        pytest.param(case, id=case["id"], marks=(
            [pytest.mark.xfail(reason=case["xfail"], strict=True)] if case.get("xfail") else []))
        for case in CASES["cases"]
    ]


@pytest.mark.parametrize("case", _case_params())
def test_search_case(case):
    known = set(CASES["index"].values())
    assert set(case["top5"]) | set(case.get("not_top5", [])) <= known, "case names an unknown entry"
    got = names(rank(INDEX, case["query"], limit=5, languages=case.get("languages", ()),
                     interfaces=case.get("interfaces", ()), licenses=case.get("licenses", ())))
    for want in case["top5"]:
        assert want in got, f"{want} not in top five: {got}"
    for unwanted in case.get("not_top5", []):
        assert unwanted not in got, f"{unwanted} in top five: {got}"


# --- Tokens and stemming -----------------------------------------------------------------


@pytest.mark.parametrize("word", ["validate", "validates", "validated", "validating"])
def test_validate_family_stems_to_validat(word):
    assert terms(word) == ["validat"]


@pytest.mark.parametrize("word,stem", [
    ("libraries", "library"),  # ies -> y, then nothing else applies
    ("queries", "query"),  # four characters remain after removing ies
    ("series", "seri"),  # ies would leave three; then s, then e are removed
    ("parses", "pars"),  # s, then e
    ("parsing", "pars"),
    ("parsed", "pars"),
    ("parser", "parser"),
    ("access", "access"),  # ss is not a plural
    ("class", "class"),
    ("uses", "uses"),  # removing s would leave three characters
    ("thing", "thing"),  # removing ing would leave two
    ("feeds", "feed"),
    ("cached", "cach"),
    ("caching", "cach"),
    ("cache", "cach"),
    ("rules", "rule"),  # removing e would leave three
    ("yaml", "yaml"),
    ("time", "time"),
    ("dates", "date"),
    ("files", "file"),
])
def test_stemming_rules(word, stem):
    assert terms(word) == [stem]


def test_tokens_split_on_anything_but_letters_digits_plus_and_hash():
    assert terms("RECX.yaml") == ["recx", "yaml"]
    assert terms("C++ and C# and F#") == ["c++", "c#", "f#"]
    assert terms("snake_case/kebab-case") == ["snak", "case", "kebab"]


def test_single_characters_dropped_except_c_and_r():
    assert terms("a b c d r x 7 R C") == ["c", "r"]


def test_nfkc_and_case_folding():
    assert terms("ＹＡＭＬ") == ["yaml"]  # fullwidth letters
    assert terms("ﬁles") == ["file"]  # the fi ligature
    assert terms("STRASSE Straße") == ["strass"]


def test_stopwords_dropped_and_list_is_about_fifty():
    assert 40 <= len(STOPWORDS) <= 80
    assert terms("the parser of the feeds and of the atom") == ["parser", "feed", "atom"]


def test_terms_are_distinct_in_first_seen_order():
    assert terms("yaml json yaml schema json") == ["yaml", "json", "schema"]


# --- Scoring -----------------------------------------------------------------------------


def test_weights_follow_section_3():
    assert WEIGHTS == {"capabilities": 3.0, "summary": 2.0, "labels": 1.5,
                       "description": 1.0, "entrypoints": 0.5}
    assert (K1, B) == (1.2, 0.75)


def test_bm25f_score_by_hand():
    # Two entries. Query term "yaml" appears once in a's capabilities and once in a's summary.
    a = minimal("alpha", ["lint yaml files"])
    a["manifest"]["summary"] = "Checks yaml."
    b = minimal("beta", ["format byte sizes"])
    got = rank([a, b], "yaml")
    assert names(got) == ["example/alpha"]
    # Lengths in terms: capabilities a=3 ("lint", "yaml", "fil"), b=3 ("format", "byte", "siz").
    # summary a=2 ("check", "yaml"), b=2 ("beta", "project"). Both at the average.
    tf_prime = 3.0 * 1 / (1 - B + B * 3 / 3) + 2.0 * 1 / (1 - B + B * 2 / 2)
    idf = math.log(1 + (2 - 1 + 0.5) / (1 + 0.5))
    assert got[0].score == pytest.approx(idf * tf_prime / (K1 + tf_prime))


def test_length_normalisation_uses_field_average():
    short = minimal("short", ["lint yaml"])
    long = minimal("long", ["lint yaml files with many rules for every kind of document"])
    got = rank([short, long], "yaml")
    assert names(got) == ["example/short", "example/long"]
    assert got[0].score > got[1].score


def test_repeated_query_terms_count_once():
    assert rank(INDEX, "yaml")[0].score == rank(INDEX, "yaml yaml YAML yaml")[0].score


def test_query_capped_at_2000_characters():
    padding = "x" * 1996 + " "  # one term no fixture has; "sitemap" is cut to "sit"
    assert names(rank(INDEX, padding + "sitemap")) == []
    assert names(rank(INDEX, padding[:-20] + " sitemap")) == ["parcel-run/sitemapgen"]


def test_every_field_of_the_table_is_scored():
    base = {"recx": "0.1", "name": "plain", "summary": "Nothing much.", "license": "MIT",
            "status": "active", "capabilities": ["do nothing much"]}
    variants = {
        "capabilities": {"capabilities": ["do zebra things"]},
        "summary": {"summary": "Zebra."},
        "tags": {"tags": ["zebra"]},
        "languages": {"languages": ["zebra"]},
        "name": {"name": "zebra"},
        "description": {"description": "Zebra."},
        "module-capabilities": {"modules": [_module(capabilities=["zebra"])]},
        "module-summary": {"modules": [_module(summary="Zebra.")]},
        "module-language": {"modules": [_module(language="zebra")]},
        "module-entrypoints": {"modules": [_module(entrypoints=["zebra()"])]},
    }
    for label, change in variants.items():
        got = rank([entry("x/plain", base), entry("x/zebra", {**base, **change})], "zebra")
        assert names(got) == ["x/zebra"], label


def _module(**fields):
    module = {"id": "m", "path": "m/", "summary": fields.get("summary", "A module."),
              "interface": {"type": "library", "entrypoints": fields.get("entrypoints", ["m()"])},
              "extract": {"method": "copy"}}
    if "capabilities" in fields:
        module["capabilities"] = fields["capabilities"]
    if "language" in fields:
        module["language"] = fields["language"]
    return module


def test_unscored_fields_do_not_match():
    manifest = {"recx": "0.1", "name": "plain", "summary": "Nothing.", "license": "MIT",
                "status": "active", "capabilities": ["do nothing"],
                "modules": [{**_module(), "extract": {"method": "copy", "notes": "zebra"}}],
                "evidence": [{"id": "zebra", "summary": "Zebra.", "covers": []}],
                "reuses": [{"repository": "https://github.com/zebra/zebra"}]}
    assert rank([entry("x/plain", manifest)], "zebra") == []


def test_higher_weight_field_scores_higher():
    in_caps = minimal("one", ["zebra"])
    in_desc = minimal("two", ["other"], description="zebra")
    assert names(rank([in_caps, in_desc], "zebra")) == ["example/one", "example/two"]


def test_reworded_capability_scores_at_most_one_and_a_half_times():
    base = copy.deepcopy(next(e for e in INDEX if e["source"]["full_name"] == "kitebase/patient"))
    repeated = copy.deepcopy(base)
    repeated["manifest"]["capabilities"].append(
        "back off exponentially, with jitter, before retrying a failed async call")
    others = [e for e in INDEX if e["source"]["full_name"] != "kitebase/patient"]
    for query in ["retry with exponential backoff", "jitter", "retry failed async calls"]:
        before = _score_of(others + [base], query, "kitebase/patient")
        after = _score_of(others + [repeated], query, "kitebase/patient")
        assert after <= 1.5 * before, query


def _score_of(entries, query, full_name):
    return next(m.score for m in rank(entries, query, limit=100) if
                m.entry["source"]["full_name"] == full_name)


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["id"])
def test_repeating_best_capability_stays_under_one_and_a_half(case):
    # For every entry matching the case's query: repeat its best-matching capability phrase
    # in different word order, as a manifest author padding for rank would, and compare.
    qterms = set(terms(case["query"]))
    for i, target in enumerate(INDEX):
        matched = rank([target], case["query"])
        if not matched or not matched[0].capabilities:
            continue
        best = matched[0].capabilities[0]
        padded = copy.deepcopy(target)
        padded["manifest"]["capabilities"].append(" ".join(reversed(best.split())))
        name = target["source"]["full_name"]
        before = _score_of(INDEX, case["query"], name)
        after = _score_of(INDEX[:i] + [padded] + INDEX[i + 1:], case["query"], name)
        assert after <= 1.5 * before, (name, best, qterms)


# --- Ordering, recall, limits ------------------------------------------------------------


def test_equal_scores_ordered_by_full_name():
    twins = [minimal("same", ["parse yaml files"]) for _ in range(3)]
    for t, owner in zip(twins, ["zeta", "alpha", "mid"]):
        t["source"]["full_name"] = f"{owner}/same"
    got = rank(twins, "yaml")
    assert len({m.score for m in got}) == 1
    assert names(got) == ["alpha/same", "mid/same", "zeta/same"]


def test_ties_in_the_fixture_index_are_ordered_by_full_name():
    for case in CASES["cases"]:
        got = rank(INDEX, case["query"], limit=100)
        keys = [(-m.score, m.entry["source"]["full_name"]) for m in got]
        assert keys == sorted(keys)


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["id"])
def test_every_entry_sharing_a_term_is_returned_with_a_large_limit(case):
    qterms = set(terms(case["query"]))
    sharing = set()
    for e in INDEX:
        text = " ".join(_scored_text(e["manifest"]))
        if qterms & set(terms(text)):
            sharing.add(e["source"]["full_name"])
    got = names(rank(INDEX, case["query"], limit=100))
    assert set(got) == sharing


def _scored_text(m):
    parts = [m.get("name"), m.get("summary"), m.get("description"), m.get("capabilities"),
             m.get("tags"), m.get("languages")]
    for mod in m.get("modules", []):
        parts += [mod.get("summary"), mod.get("capabilities"), mod.get("language"),
                  mod["interface"]["entrypoints"]]
    return [s for p in parts if p for s in ([p] if isinstance(p, str) else p)]


def test_one_shared_term_is_enough():
    got = names(rank(INDEX, "zzzqx sitemap", limit=100))
    assert got == ["parcel-run/sitemapgen"]


def test_limit_cut_and_cap():
    many = [minimal(f"p{i:03d}", ["parse yaml"]) for i in range(150)]
    assert len(rank(many, "yaml")) == 20
    assert len(rank(many, "yaml", limit=3)) == 3
    assert len(rank(many, "yaml", limit=1000)) == MAX_LIMIT
    with pytest.raises(ValueError):
        rank(many, "yaml", limit=0)


def test_empty_query_and_empty_index():
    assert rank(INDEX, "") == []
    assert rank(INDEX, "the of and") == []
    assert rank([], "yaml") == []


def test_rank_is_pure_and_deterministic():
    before = json.dumps(INDEX, sort_keys=True)
    assert scored(rank(INDEX, "parse yaml json")) == scored(rank(INDEX, "parse yaml json"))
    assert json.dumps(INDEX, sort_keys=True) == before


# --- Filters -----------------------------------------------------------------------------


def test_language_filter_uses_project_and_module_languages():
    only_module = minimal("modlang", ["parse things"], modules=[_module(language="zig")])
    only_project = minimal("projlang", ["parse things"], languages=["zig"])
    neither = minimal("nolang", ["parse things"])
    got = names(rank([only_module, only_project, neither], "parse", languages=["zig"]))
    assert sorted(got) == ["example/modlang", "example/projlang"]


def test_language_filter_is_exact_tag_not_substring():
    got = names(rank(INDEX, "parse", languages=["c"], limit=100))
    assert got == []
    assert names(rank(INDEX, "parse", languages=["C++"], limit=100)) == ["quarry-cpp/quickjson"]


def test_interface_filter():
    got = names(rank(INDEX, "github", interfaces=["github-action"], limit=100))
    assert sorted(got) == ["octolabel/pr-labeler", "relaybot/slack-notify"]
    got = names(rank(INDEX, "sqlite database", interfaces=["mcp-server", "http-api"], limit=100))
    assert got == ["tidepool-dev/mcp-sqlite"]


def test_license_filter():
    got = names(rank(INDEX, "parse", licenses=["BSD-3-Clause"], limit=100))
    assert got == ["quillworks/feedparse"]
    got = names(rank(INDEX, "parse", licenses=["bsl-1.0", "BSD-3-Clause"], limit=100))
    assert sorted(got) == ["quarry-cpp/quickjson", "quillworks/feedparse"]


def test_filters_do_not_change_scores():
    full = dict(scored(rank(INDEX, "parse", limit=100)))
    for m in rank(INDEX, "parse", languages=["python"], limit=100):
        assert m.score == full[m.entry["source"]["full_name"]]


# --- Explanation -------------------------------------------------------------------------


def test_explanation_lists_terms_capabilities_and_modules():
    feedparse = next(m for m in rank(INDEX, "parse feed dates leniently")
                     if m.entry["source"]["full_name"] == "quillworks/feedparse")
    assert feedparse.matched_terms == ["pars", "feed", "date", "leniently"]
    assert feedparse.capabilities == [
        "parse RFC 822 and ISO 8601 dates leniently",  # 3 terms, module capability
        "parse RSS and Atom feeds into a common entry type",  # 2 terms, project, first
        "normalise feed dates to timezone-aware datetimes",  # 2 terms, project, second
        "sanitise HTML in feed titles and summaries",  # 1 term
    ]
    assert feedparse.modules == ["parser", "dates"]


def test_modules_only_lists_modules_with_a_match():
    m = next(m for m in rank(INDEX, "completion scripts")
             if m.entry["source"]["full_name"] == "ferrous-tools/argwise")
    assert m.modules == ["complete"]
    assert m.capabilities == ["generate shell completion scripts",
                              "generate shell completions for bash, zsh and fish"]


def test_match_entry_is_the_given_entry():
    m = rank(INDEX, "sitemap")[0]
    assert m.entry is next(e for e in INDEX if e["source"]["full_name"] == "parcel-run/sitemapgen")
