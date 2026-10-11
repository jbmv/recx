import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from recx_cli import hook
from recx_cli.cli import main

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugin" / "scripts" / "stop-hook.sh"

VALID = """recx: "0.1"
name: example
summary: An example project.
license: MIT
status: experimental
capabilities:
  - do one thing
"""
INVALID = VALID.replace("status: experimental\n", "status: bogus\n")


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("RECX_STOP_HOOK", raising=False)


def stop(capsys, monkeypatch, stdin):
    if not isinstance(stdin, str):
        stdin = json.dumps(stdin)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = main(["hook", "stop"])
    out = capsys.readouterr()
    return code, out.out


def event(cwd, **kw):
    return {"hook_event_name": "Stop", "cwd": str(cwd), "stop_hook_active": False, **kw}


def git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_no_manifest_is_silent(capsys, monkeypatch, tmp_path):
    assert stop(capsys, monkeypatch, event(tmp_path)) == (0, "")


def test_valid_manifest_is_silent(capsys, monkeypatch, tmp_path):
    (tmp_path / "RECX.yaml").write_text(VALID)
    assert stop(capsys, monkeypatch, event(tmp_path)) == (0, "")


def test_invalid_blocks_and_matches_recx_check(capsys, monkeypatch, tmp_path):
    (tmp_path / "RECX.yaml").write_text(INVALID)
    code, out = stop(capsys, monkeypatch, event(tmp_path))
    assert code == 0
    data = json.loads(out)
    assert data["decision"] == "block" and "recx:manifest" in data["reason"]
    assert main(["check", str(tmp_path / "RECX.yaml")]) == 1
    check_lines = capsys.readouterr().out.splitlines()
    assert check_lines
    for line in check_lines:
        assert line.split(": ", 1)[1] in data["reason"]


def test_manifest_at_git_toplevel_from_subdirectory(capsys, monkeypatch, tmp_path):
    git_init(tmp_path)
    (tmp_path / "RECX.yaml").write_text(INVALID)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    code, out = stop(capsys, monkeypatch, event(sub))
    assert code == 0 and json.loads(out)["decision"] == "block"


def test_no_manifest_in_subdirectory_outside_git(capsys, monkeypatch, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "RECX.yaml").write_text(INVALID)
    assert stop(capsys, monkeypatch, event(sub)) == (0, "")


@pytest.mark.parametrize("how", ["active", "warn"])
def test_feedback_without_block(capsys, monkeypatch, tmp_path, how):
    (tmp_path / "RECX.yaml").write_text(INVALID)
    if how == "warn":
        monkeypatch.setenv("RECX_STOP_HOOK", "warn")
        ev = event(tmp_path)
    else:
        ev = event(tmp_path, stop_hook_active=True)
    code, out = stop(capsys, monkeypatch, ev)
    data = json.loads(out)
    assert code == 0 and "decision" not in data
    spec = data["hookSpecificOutput"]
    assert spec["hookEventName"] == "Stop" and "status" in spec["additionalContext"]


def test_off_does_not_read_anything(capsys, monkeypatch, tmp_path):
    (tmp_path / "RECX.yaml").write_text(INVALID)
    monkeypatch.setenv("RECX_STOP_HOOK", "off")

    class Boom:
        def read(self):
            raise AssertionError("stdin read")

    monkeypatch.setattr(sys, "stdin", Boom())
    monkeypatch.setattr(hook, "check_file", lambda *a, **k: pytest.fail("manifest read"))
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_unrecognised_value_behaves_as_block(capsys, monkeypatch, tmp_path):
    (tmp_path / "RECX.yaml").write_text(INVALID)
    monkeypatch.setenv("RECX_STOP_HOOK", "sometimes")
    _, out = stop(capsys, monkeypatch, event(tmp_path))
    assert json.loads(out)["decision"] == "block"


@pytest.mark.parametrize(
    "stdin", ["", "   \n", "{not json", "[1, 2]", "null", '{"hook_event_name": "Stop"}', '{"cwd": 5}']
)
def test_bad_input_never_blocks(capsys, monkeypatch, stdin):
    assert stop(capsys, monkeypatch, stdin) == (0, "")


def test_internal_error_never_blocks(capsys, monkeypatch, tmp_path):
    (tmp_path / "RECX.yaml").write_text(INVALID)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "check_file", boom)
    assert stop(capsys, monkeypatch, event(tmp_path)) == (0, "")


def test_unreadable_manifest_never_blocks(capsys, monkeypatch, tmp_path):
    (tmp_path / "RECX.yaml").mkdir()
    assert stop(capsys, monkeypatch, event(tmp_path)) == (0, "")


# -- the entry script ------------------------------------------------------


def run_script(stdin, env_extra=None, path=None):
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "RECX_STOP_HOOK")}
    if path is not None:
        env["PATH"] = path
    env.update(env_extra or {})
    return subprocess.run(["/bin/sh", str(SCRIPT)], input=stdin, capture_output=True, text=True, env=env)


def test_script_is_executable_in_git():
    out = subprocess.run(
        ["git", "ls-files", "-s", "plugin/scripts/stop-hook.sh"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert out.startswith("100755")


def test_hooks_json_runs_the_script():
    data = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text())
    cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd == '"${CLAUDE_PLUGIN_ROOT}"/scripts/stop-hook.sh'
    assert "matcher" not in data["hooks"]["Stop"][0]


def test_script_end_to_end_blocks_in_git_repo(tmp_path):
    git_init(tmp_path)
    (tmp_path / "RECX.yaml").write_text(INVALID)
    sub = tmp_path / "src"
    sub.mkdir()
    recx_dir = Path(sys.executable).parent
    assert (recx_dir / "recx").exists()
    path = f"{recx_dir}{os.pathsep}{os.environ['PATH']}"
    r = run_script(json.dumps(event(sub)), path=path)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["decision"] == "block" and "recx:manifest" in data["reason"]


def test_script_off_exits_at_once(tmp_path):
    (tmp_path / "RECX.yaml").write_text(INVALID)
    r = run_script(json.dumps(event(tmp_path)), {"RECX_STOP_HOOK": "off"})
    assert (r.returncode, r.stdout) == (0, "")


def test_script_without_recx_or_uvx_is_silent(tmp_path):
    (tmp_path / "RECX.yaml").write_text(INVALID)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    r = run_script(json.dumps(event(tmp_path)), path=str(bindir))
    assert (r.returncode, r.stdout) == (0, "")


def test_script_with_outdated_recx_falls_through(tmp_path):
    (tmp_path / "RECX.yaml").write_text(INVALID)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    old = bindir / "recx"
    old.write_text("#!/bin/sh\nexit 2\n")
    old.chmod(0o755)
    r = run_script(json.dumps(event(tmp_path)), path=str(bindir))
    assert (r.returncode, r.stdout) == (0, "")


def test_script_never_passes_a_failing_status_through(tmp_path):
    # Claude Code reads exit status 2 from a Stop hook as a block; uv exits 2 on
    # some failures, so a failing uvx must not become a blocked session.
    (tmp_path / "RECX.yaml").write_text(INVALID)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uvx = bindir / "uvx"
    uvx.write_text("#!/bin/sh\necho 'error: offline' >&2\nexit 2\n")
    uvx.chmod(0o755)
    r = run_script(json.dumps(event(tmp_path)), path=str(bindir))
    assert (r.returncode, r.stdout) == (0, "")


def test_plugin_version_matches_recx_cli():
    # Claude Code updates an installed plugin only when plugin.json's version string changes,
    # so a release that changes the hooks must bump both together.
    import tomllib

    plugin = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    package = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert plugin["version"] == package
