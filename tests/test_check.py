import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from recx_cli.cli import main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "spec" / "examples"


def files(sub):
    return sorted((EXAMPLES / sub).glob("*.yaml"))


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


BASE = """recx: "0.1"
name: example
summary: An example project.
license: MIT
status: experimental
capabilities:
  - do one thing
"""


def write(tmp_path, text, name="RECX.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# c3
def test_repo_manifest_is_valid(capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    code, out, _ = run(capsys, "check")
    assert (code, out) == (0, "RECX.yaml: valid\n")


# c4
@pytest.mark.parametrize("f", files("valid"), ids=lambda f: f.name)
def test_valid_examples(capsys, f):
    code, out, _ = run(capsys, "check", "--no-repo", str(f))
    assert code == 0 and out.strip().endswith(": valid")


@pytest.mark.parametrize("sub", ["invalid", "invalid-rules"])
def test_invalid_examples(capsys, sub):
    fs = files(sub)
    assert fs
    for f in fs:
        code, out, _ = run(capsys, "check", "--no-repo", str(f))
        assert code == 1, f.name
        assert out.count("\n") >= 1 and ": valid" not in out


# c5
RULE_FILES = {
    "rule1-duplicate-module-id.yaml": (1, "/modules/1/id"),
    "rule1-duplicate-evidence-id.yaml": (1, "/evidence/1/id"),
    "rule2-requires-unknown-module.yaml": (2, "/modules/0/requires/modules/0"),
    "rule2-covers-unknown-module.yaml": (2, "/evidence/0/covers/1"),
    "rule5-over-64-kib.yaml": (5, "/"),
    "rule6-anchor-alias.yaml": (6, "/"),
    "rule6-merge-key.yaml": (6, "/"),
    "rule6-duplicate-key.yaml": (6, "/"),
}


def test_invalid_rules_cover_each_rule():
    names = {f.name for f in files("invalid-rules")}
    assert names == set(RULE_FILES)
    for f in files("invalid-rules"):
        first = f.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith(f"# rule {RULE_FILES[f.name][0]}:"), f.name


def test_invalid_rules_are_accepted_by_the_schema_alone():
    import yaml
    from jsonschema import Draft202012Validator, FormatChecker

    from recx_cli.check import load_schema

    v = Draft202012Validator(load_schema(), format_checker=FormatChecker())
    for f in files("invalid-rules"):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))  # plain loader: last key wins
        assert not list(v.iter_errors(data)), f.name


@pytest.mark.parametrize("name,expected", RULE_FILES.items())
def test_rule_violation_reported_at_pointer(capsys, name, expected):
    rule, pointer = expected
    path = EXAMPLES / "invalid-rules" / name
    code, out, _ = run(capsys, "check", "--no-repo", str(path))
    assert code == 1
    assert out.startswith(f"{path}: {pointer}: ")
    assert f"rule {rule}" in out


def test_rule6_errors_carry_line_numbers(capsys):
    for name, line in [("rule6-anchor-alias.yaml", 13), ("rule6-merge-key.yaml", 14), ("rule6-duplicate-key.yaml", 9)]:
        _, out, _ = run(capsys, "check", "--no-repo", str(EXAMPLES / "invalid-rules" / name))
        assert f"line {line}:" in out, (name, out)


# c6
def test_missing_path_rejected(capsys, tmp_path):
    p = write(tmp_path, BASE + MODULE.format(path="src/a.py"))
    code, out, _ = run(capsys, "check", str(p))
    assert code == 1 and f"{p}: /modules/0/path: " in out and "does not exist" in out
    code, out, _ = run(capsys, "check", "--no-repo", str(p))
    assert code == 0


MODULE = """modules:
  - id: core
    path: {path}
    summary: A module.
    interface: {{type: library, entrypoints: ["f()"]}}
    extract: {{method: copy}}
"""


def test_trailing_slash_must_match_kind(capsys, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    cases = {"src/": 0, "src/a.py": 0, "src": 1, "src/a.py/": 1}
    for path, expected in cases.items():
        p = write(tmp_path, BASE + MODULE.format(path=path))
        code, out, _ = run(capsys, "check", str(p))
        assert code == expected, (path, out)


def test_paths_resolve_against_manifest_dir_not_cwd(capsys, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    write(repo, BASE + MODULE.format(path="src/"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, out, _ = run(capsys, "check", str(repo))  # directory argument
    assert code == 0, out


def test_escaping_path_is_never_probed(capsys, tmp_path):
    p = write(tmp_path, BASE + MODULE.format(path="../x"))
    code, out, _ = run(capsys, "check", str(p))
    assert code == 1 and "does not exist" not in out and "/modules/0/path" in out


# c7
def test_unquoted_version_and_date_get_quote_hint(capsys, tmp_path):
    p = write(tmp_path, BASE.replace('"0.1"', "0.1"))
    code, out, _ = run(capsys, "check", "--no-repo", str(p))
    assert code == 1
    assert out.startswith(f"{p}: /recx: ") and "quote" in out and '"0.1"' in out
    assert out.count("\n") == 1  # the redundant enum error is folded in

    text = BASE + """evidence:
  - id: t
    run: pytest
    covers: []
    last_result: {status: pass, commit: abc1234, date: 2026-10-01T14:03:00Z, runner: ci}
"""
    p = write(tmp_path, text)
    code, out, _ = run(capsys, "check", "--no-repo", str(p))
    assert code == 1
    assert "/evidence/0/last_result/date: " in out and "quote" in out and "2026-10-01" in out


def test_unquoted_bool_in_enum_gets_hint(capsys, tmp_path):
    p = write(tmp_path, BASE.replace("experimental", "yes"))
    _, out, _ = run(capsys, "check", "--no-repo", str(p))
    assert "/status: " in out and "quote" in out


# c8
def test_all_errors_reported_with_file_and_pointer(capsys, tmp_path):
    p = write(tmp_path, BASE.replace("MIT", "GPL-3.0-only").replace("experimental", "bogus"))
    code, out, _ = run(capsys, "check", "--no-repo", str(p))
    lines = out.splitlines()
    assert code == 1 and len(lines) == 2
    assert all(line.startswith(f"{p}: /") for line in lines)
    assert {line.split(": ")[1] for line in lines} == {"/license", "/status"}


def test_exit_2_for_usage_and_unreadable(capsys, tmp_path):
    with pytest.raises(SystemExit) as e:
        main(["check", "--bogus"])
    assert e.value.code == 2
    capsys.readouterr()
    code, out, err = run(capsys, "check", str(tmp_path / "nope.yaml"))
    assert code == 2 and out == "" and "nope.yaml" in err
    code, _, err = run(capsys, "check", str(tmp_path))  # directory without RECX.yaml
    assert code == 2


def test_hostile_text_is_escaped(capsys, tmp_path):
    p = write(tmp_path, BASE.replace("experimental", '"\\e[31mred\\x00"'))
    _, out, _ = run(capsys, "check", "--no-repo", str(p))
    assert "\x1b" not in out and "\x00" not in out


def test_invalid_yaml_and_utf8(capsys, tmp_path):
    p = write(tmp_path, "a: [unclosed\n")
    code, out, _ = run(capsys, "check", "--no-repo", str(p))
    assert code == 1 and "line " in out
    p.write_bytes(b"\xff\xfe\x00bad")
    code, out, _ = run(capsys, "check", "--no-repo", str(p))
    assert code == 1 and "UTF-8" in out


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and "recx " in capsys.readouterr().out


# c1, c2
def test_schema_has_one_copy_under_version_control():
    tracked = subprocess.run(
        ["git", "ls-files", "*recx.schema.json"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert tracked == ["spec/recx.schema.json"]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_build_produces_sdist_and_wheel(tmp_path):
    out = tmp_path / "dist"
    env = {k: v for k, v in __import__("os").environ.items() if k != "VIRTUAL_ENV"}
    subprocess.run(["uv", "build", "--out-dir", str(out)], cwd=ROOT, check=True, capture_output=True, env=env)
    version = __import__("tomllib").loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    wheels = list(out.glob(f"recx_cli-{version}-*.whl"))
    sdists = list(out.glob(f"recx_cli-{version}.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1
    with zipfile.ZipFile(wheels[0]) as z:
        names = z.namelist()
        tops = {n.split("/")[0] for n in names}
        assert "recx_cli" in tops and "recx" not in tops
        packaged = z.read("recx_cli/recx.schema.json")
        entry = [n for n in names if n.endswith("entry_points.txt")]
        assert "recx = recx_cli.cli:main" in z.read(entry[0]).decode()
    assert hashlib.sha256(packaged).digest() == hashlib.sha256(
        (ROOT / "spec" / "recx.schema.json").read_bytes()
    ).digest()
    # a wheel built from the sdist still finds the schema
    import tarfile

    with tarfile.open(sdists[0]) as t:
        assert any(n.endswith("spec/recx.schema.json") for n in t.getnames())


def test_bidi_controls_in_echoed_text_are_escaped():
    from recx_cli.check import _clean

    shown = _clean("evil‮txt.exe⁦x⁩\x1b[31m")
    assert "‮" not in shown and "⁦" not in shown and "\x1b" not in shown
    assert "\\u202e" in shown and "\\x1b" in shown


# -- check_text (RECX-14) ---------------------------------------------------

from recx_cli.check import MAX_BYTES, Problem, check_file, check_text  # noqa: E402


def test_check_text_is_exposed_and_returns_problems():
    assert check_text(BASE) == []
    assert all(isinstance(p, Problem) for p in check_text("recx: 1\n"))


@pytest.mark.parametrize(
    "fixture",
    sorted(EXAMPLES.rglob("*.yaml")),
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_check_file_matches_check_text_on_every_fixture(fixture):
    text = fixture.read_bytes().decode("utf-8")
    assert check_file(fixture, repo=False) == check_text(text)


def test_check_file_repo_adds_only_rule_3(tmp_path):
    text = BASE + "modules:\n  - id: a\n    path: missing.py\n    summary: x\n"
    p = write(tmp_path, text)
    plain = check_file(p, repo=False)
    with_repo = check_file(p, repo=True)
    assert with_repo[: len(plain)] == plain
    assert any("(rule 3)" in x.message for x in with_repo[len(plain):])


def test_check_text_rejects_over_64_kib_by_utf8_bytes():
    # 2-byte characters: under the limit by characters, over it by bytes
    text = "# " + "é" * (MAX_BYTES // 2) + "\n"
    assert len(text) <= MAX_BYTES < len(text.encode("utf-8"))
    n = len(text.encode("utf-8"))
    assert check_text(text) == [Problem("/", f"file is {n} bytes, over the 64 KiB limit (rule 5)")]
    assert check_text("x" * (MAX_BYTES + 1))[0].message.endswith("over the 64 KiB limit (rule 5)")
