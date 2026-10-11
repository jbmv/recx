import json
import zipfile
from pathlib import Path

import jsonschema
import pytest
import yaml

from recx_cli.index import project

ROOT = Path(__file__).resolve().parent.parent
VALID = ROOT / "spec" / "examples" / "valid"
SCHEMA = json.loads((ROOT / "spec" / "index.schema.json").read_text())


def load(name):
    return yaml.safe_load((VALID / name).read_text())


def keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys(v)


def test_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_full_projection_drops_setup_run_and_extensions():
    full = load("full.yaml")
    assert "setup" in set(keys(full)) and "run" in set(keys(full))
    assert any(str(k).startswith("x-") for k in keys(full))
    got = set(keys(project(full)))
    assert "setup" not in got
    assert "run" not in got
    assert not [k for k in got if str(k).startswith("x-")]


def test_project_does_not_mutate_input():
    full = load("full.yaml")
    before = json.dumps(full, default=str)
    project(full)
    assert json.dumps(full, default=str) == before


def test_escapes_format_and_control_characters():
    m = load("minimal.yaml")
    m["summary"] = "a‮b​c\U000e0041d\x07e\xadf"
    m["capabilities"] = ["x‮y"]
    got = project(m)
    assert got["summary"] == "a\\u202eb\\u200bc\\U000e0041d\\x07e\\xadf"
    assert got["capabilities"] == ["x\\u202ey"]


def test_escapes_nested_strings():
    full = load("full.yaml")
    full["modules"][0]["interface"]["entrypoints"][0] = "f()​"
    got = project(full)
    assert got["modules"][0]["interface"]["entrypoints"][0] == "f()\\u200b"


def source(name):
    return {
        "full_name": f"example/{name}",
        "url": f"https://github.com/example/{name}",
        "default_branch": "main",
        "commit": "a" * 40,
        "blob_sha": "b" * 40,
        "manifest_url": f"https://raw.githubusercontent.com/example/{name}/{'a' * 40}/RECX.yaml",
        "detected_license": "MIT",
        "license_check": "match",
        "declared_repository": "matches",
        "is_fork": False,
        "parent": None,
        "is_archived": False,
        "stars": 3,
        "repo_created_at": "2025-01-01T00:00:00Z",
        "pushed_at": "2026-10-01T00:00:00Z",
        "found_by": ["code-search", "topic"],
        "first_indexed": "2026-10-12T04:21:09Z",
        "last_verified": "2026-10-12T04:21:09Z",
    }


def sample():
    entries = [
        {"source": source(Path(n).stem), "manifest": project(load(n))}
        for n in ("full.yaml", "minimal.yaml")
    ]
    return {
        "format": "recx-index/1.0",
        "generated_at": "2026-10-12T04:21:09Z",
        "generator": "recx-crawler (recx-cli 0.1.0)",
        "spec_versions": ["0.1"],
        "counts": {"entries": len(entries), "rejected": 0},
        "entries": entries,
    }


def validate(doc):
    jsonschema.Draft202012Validator(
        SCHEMA, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    ).validate(json.loads(json.dumps(doc, default=str)))


def test_sample_index_validates():
    validate(sample())


def test_top_level_is_open_for_minor_versions():
    doc = sample()
    doc["format"] = "recx-index/1.7"
    doc["added_later"] = {"a": 1}
    validate(doc)


@pytest.mark.parametrize("mutate", [
    lambda d: d["entries"][0]["manifest"]["evidence"][0].update(run="make"),
    lambda d: d["entries"][0]["manifest"]["evidence"][0].update(setup="make"),
    lambda d: d["entries"][0]["manifest"].update({"x-note": "hi"}),
    lambda d: d["entries"][0]["manifest"]["modules"][0].update({"x-note": "hi"}),
    lambda d: d["entries"][0]["source"].update(license_check="maybe"),
    lambda d: d["entries"][0]["source"].update(found_by=["web"]),
    lambda d: d["entries"][0]["source"].update(extra=1),
    lambda d: d.update(format="recx-index/2.0"),
])
def test_schema_rejects(mutate):
    doc = sample()
    mutate(doc)
    with pytest.raises(jsonschema.ValidationError):
        validate(doc)


def test_schema_not_in_wheel():
    wheels = sorted((ROOT / "dist").glob("*.whl")) if (ROOT / "dist").exists() else []
    if not wheels:
        pytest.skip("no wheel built")
    names = zipfile.ZipFile(wheels[-1]).namelist()
    assert not any("index.schema.json" in n for n in names)
