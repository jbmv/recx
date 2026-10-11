import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from recx_cli import drift, hook
from recx_cli.cli import main

ROOT = Path(__file__).resolve().parents[1]
STOP_SCRIPT = ROOT / "plugin" / "scripts" / "stop-hook.sh"
START_SCRIPT = ROOT / "plugin" / "scripts" / "session-start-hook.sh"

MANIFEST = """recx: "0.1"
name: example
summary: An example project.
license: MIT
status: experimental
capabilities:
  - do one thing
modules:
  - id: lib
    path: src/lib/
    summary: The library.
    interface:
      type: library
      entrypoints:
        - "import lib"
    extract:
      method: copy
  - id: tool
    path: tool.py
    summary: The tool.
    interface:
      type: cli
      entrypoints:
        - "python tool.py"
    extract:
      method: copy
"""
INVALID = MANIFEST.replace("status: experimental\n", "status: bogus\n")
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("RECX_STOP_HOOK", "RECX_DRIFT_REMINDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src" / "lib").mkdir(parents=True)
    (r / "src" / "lib" / "a.py").write_text("a = 1\n")
    (r / "tool.py").write_text("print(1)\n")
    (r / "RECX.yaml").write_text(MANIFEST)
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def run(capsys, monkeypatch, sub, repo, session="s1", **kw):
    ev = {"session_id": session, "cwd": str(repo), "stop_hook_active": False, **kw}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ev)))
    code = main(["hook", sub])
    return code, capsys.readouterr().out


def start(capsys, monkeypatch, repo, **kw):
    assert run(capsys, monkeypatch, "session-start", repo, **kw) == (0, "")


def stop(capsys, monkeypatch, repo, **kw):
    return run(capsys, monkeypatch, "stop", repo, **kw)


def state_file(tmp_path, session="s1"):
    return tmp_path / "data" / f"{session}.json"


def edit_lib(repo):
    (repo / "src" / "lib" / "a.py").write_text("a = 2\n")


def reminder_of(out):
    data = json.loads(out)
    assert "decision" not in data
    spec = data["hookSpecificOutput"]
    assert spec["hookEventName"] == "Stop"
    return spec["additionalContext"]


# -- session-start ---------------------------------------------------------


def test_session_start_records_head(capsys, monkeypatch, repo, tmp_path):
    start(capsys, monkeypatch, repo)
    state = json.loads(state_file(tmp_path).read_text())
    assert state["head"] == git(repo, "rev-parse", "HEAD").strip()


def test_session_start_default_state_dir(capsys, monkeypatch, repo, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    start(capsys, monkeypatch, repo)
    assert (tmp_path / "xdg" / "recx" / "s1.json").is_file()


def test_session_start_skips_without_manifest(capsys, monkeypatch, repo, tmp_path):
    (repo / "RECX.yaml").unlink()
    start(capsys, monkeypatch, repo)
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("var", ["RECX_DRIFT_REMINDER", "RECX_STOP_HOOK"])
def test_session_start_skips_when_off(capsys, monkeypatch, repo, tmp_path, var):
    monkeypatch.setenv(var, "off")
    start(capsys, monkeypatch, repo)
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("stdin", ["", "{nope", "[]", '{"cwd": 3}', '{"session_id": "x"}'])
def test_session_start_bad_input_is_silent(capsys, monkeypatch, stdin):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert main(["hook", "session-start"]) == 0
    assert capsys.readouterr().out == ""


def test_session_start_unsafe_session_id_writes_nothing(capsys, monkeypatch, repo, tmp_path):
    start(capsys, monkeypatch, repo, session="../evil")
    assert not (tmp_path / "data").exists()


def test_session_start_outside_git_is_silent(capsys, monkeypatch, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "RECX.yaml").write_text(MANIFEST)
    start(capsys, monkeypatch, d)


# -- stop: the reminder ----------------------------------------------------


def test_uncommitted_change_in_module_dir_reminds(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    code, out = stop(capsys, monkeypatch, repo)
    text = reminder_of(out)
    assert code == 0 and "lib" in text and "tool" not in text.split(":")[-1]
    assert "recx:manifest" in text and "not an error" in text


def test_file_module_path_matches_exactly(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    (repo / "tool.py").write_text("print(2)\n")
    text = reminder_of(stop(capsys, monkeypatch, repo)[1])
    assert text.rstrip(".").split("did not: ")[1].split(".")[0] == "tool"


def test_sibling_with_shared_prefix_is_not_under_module(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    (repo / "src" / "library.py").write_text("x = 1\n")
    (repo / "tool.py.bak").write_text("x\n")
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_untracked_file_under_module_reminds(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    (repo / "src" / "lib" / "new.py").write_text("b = 1\n")
    assert "lib" in reminder_of(stop(capsys, monkeypatch, repo)[1])


def test_commits_since_recorded_head_count(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    git(repo, "commit", "-qam", "change lib")
    assert git(repo, "status", "--porcelain") == ""
    assert "lib" in reminder_of(stop(capsys, monkeypatch, repo)[1])


def test_without_recorded_head_only_uncommitted_counts(capsys, monkeypatch, repo):
    edit_lib(repo)
    git(repo, "commit", "-qam", "committed before, no record")
    assert stop(capsys, monkeypatch, repo) == (0, "")
    (repo / "tool.py").write_text("print(3)\n")
    assert "tool" in reminder_of(stop(capsys, monkeypatch, repo)[1])


def test_corrupt_state_is_ignored_like_no_record(capsys, monkeypatch, repo, tmp_path):
    state_file(tmp_path).parent.mkdir(parents=True)
    state_file(tmp_path).write_text("{corrupt")
    (repo / "tool.py").write_text("print(3)\n")
    assert "tool" in reminder_of(stop(capsys, monkeypatch, repo)[1])


def test_manifest_changed_means_no_reminder(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    (repo / "RECX.yaml").write_text(MANIFEST + "tags: [x]\n")
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_no_changes_no_reminder(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_reminds_once_then_only_when_set_grows(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    assert "lib" in reminder_of(stop(capsys, monkeypatch, repo)[1])
    assert stop(capsys, monkeypatch, repo) == (0, "")
    assert stop(capsys, monkeypatch, repo) == (0, "")
    (repo / "tool.py").write_text("print(4)\n")
    text = reminder_of(stop(capsys, monkeypatch, repo)[1])
    assert "lib" in text and "tool" in text
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_other_session_is_reminded_separately(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo, session="a")
    start(capsys, monkeypatch, repo, session="b")
    edit_lib(repo)
    assert reminder_of(stop(capsys, monkeypatch, repo, session="a")[1])
    assert reminder_of(stop(capsys, monkeypatch, repo, session="b")[1])


def test_reminder_keeps_recorded_head(capsys, monkeypatch, repo, tmp_path):
    start(capsys, monkeypatch, repo)
    before = json.loads(state_file(tmp_path).read_text())["head"]
    edit_lib(repo)
    stop(capsys, monkeypatch, repo)
    state = json.loads(state_file(tmp_path).read_text())
    assert state["head"] == before and state["reminded"] == ["lib"]


# -- switches and validity -------------------------------------------------


def test_drift_off_silences_reminder_only(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    monkeypatch.setenv("RECX_DRIFT_REMINDER", "off")
    assert stop(capsys, monkeypatch, repo) == (0, "")
    (repo / "RECX.yaml").write_text(INVALID)
    out = stop(capsys, monkeypatch, repo)[1]
    assert json.loads(out)["decision"] == "block"


def test_stop_hook_off_disables_everything(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    monkeypatch.setenv("RECX_STOP_HOOK", "off")
    assert stop(capsys, monkeypatch, repo) == (0, "")


@pytest.mark.parametrize("mode", ["block", "warn"])
def test_invalid_manifest_gets_only_validity_output(capsys, monkeypatch, repo, mode):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    (repo / "RECX.yaml").write_text(INVALID)
    monkeypatch.setenv("RECX_STOP_HOOK", mode)
    code, out = stop(capsys, monkeypatch, repo)
    assert code == 0 and len(out.strip().splitlines()) == 1
    data = json.loads(out)
    if mode == "block":
        assert data["decision"] == "block"
    else:
        assert "invalid" in data["hookSpecificOutput"]["additionalContext"]
    assert "Reminder" not in out


def test_invalid_manifest_with_active_stop_gets_no_reminder(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    (repo / "RECX.yaml").write_text(INVALID)
    out = stop(capsys, monkeypatch, repo, stop_hook_active=True)[1]
    assert "Reminder" not in out and "invalid" in out


# -- failure isolation -----------------------------------------------------


def test_no_git_never_blocks(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    monkeypatch.setenv("PATH", str(repo))
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_non_git_directory_is_silent(capsys, monkeypatch, tmp_path):
    d = tmp_path / "plain"
    (d / "src" / "lib").mkdir(parents=True)
    (d / "tool.py").write_text("x\n")
    (d / "RECX.yaml").write_text(MANIFEST)
    assert stop(capsys, monkeypatch, d) == (0, "")


def test_drift_exception_does_not_alter_validity_output(capsys, monkeypatch, repo):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(drift, "reminder", boom)
    assert stop(capsys, monkeypatch, repo) == (0, "")
    (repo / "RECX.yaml").write_text(INVALID)
    assert json.loads(stop(capsys, monkeypatch, repo)[1])["decision"] == "block"


def test_unwritable_state_dir_still_reminds(capsys, monkeypatch, repo, tmp_path):
    start(capsys, monkeypatch, repo)
    shutil_target = tmp_path / "data"
    for p in shutil_target.iterdir():
        p.unlink()
    shutil_target.rmdir()
    shutil_target.write_text("a file, not a directory")
    edit_lib(repo)
    assert "lib" in reminder_of(stop(capsys, monkeypatch, repo)[1])


def test_missing_state_dir_is_fine(capsys, monkeypatch, repo):
    edit_lib(repo)
    assert "lib" in reminder_of(stop(capsys, monkeypatch, repo)[1])


def test_unknown_recorded_head_falls_back_to_working_tree(capsys, monkeypatch, repo, tmp_path):
    state_file(tmp_path).parent.mkdir(parents=True)
    state_file(tmp_path).write_text(json.dumps({"head": "deadbeef" * 5, "reminded": []}))
    edit_lib(repo)
    assert "lib" in reminder_of(stop(capsys, monkeypatch, repo)[1])


def test_hook_module_exports(monkeypatch):
    assert callable(hook.session_start) and callable(hook.stop)


# -- scripts ---------------------------------------------------------------


def test_scripts_are_executable_in_git():
    out = subprocess.run(
        ["git", "ls-files", "-s", "plugin/scripts"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    modes = {line.split()[-1]: line.split()[0] for line in out.splitlines()}
    assert modes["plugin/scripts/session-start-hook.sh"] == "100755"
    assert modes["plugin/scripts/stop-hook.sh"] == "100755"


def test_hooks_json_registers_session_start():
    data = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text())
    cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cmd == '"${CLAUDE_PLUGIN_ROOT}"/scripts/session-start-hook.sh'


def test_scripts_require_0_0_4():
    for script in (START_SCRIPT, STOP_SCRIPT):
        assert "recx-cli>=0.0.4" in script.read_text()


def sh(script, stdin, env):
    return subprocess.run(["/bin/sh", str(script)], input=stdin, capture_output=True, text=True, env=env)


def test_scripts_end_to_end(repo, tmp_path):
    recx_dir = Path(sys.executable).parent
    assert (recx_dir / "recx").exists()
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "RECX_STOP_HOOK", "RECX_DRIFT_REMINDER")}
    env["PATH"] = f"{recx_dir}{os.pathsep}{env['PATH']}"
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "data")
    env.update(GIT_ENV)
    ev = json.dumps({"session_id": "e2e", "cwd": str(repo), "stop_hook_active": False})

    r = sh(START_SCRIPT, ev, env)
    assert (r.returncode, r.stdout) == (0, "")
    assert json.loads((tmp_path / "data" / "e2e.json").read_text())["head"] == git(repo, "rev-parse", "HEAD").strip()

    assert sh(STOP_SCRIPT, ev, env).stdout == ""
    edit_lib(repo)
    git(repo, "commit", "-qam", "change lib")
    r = sh(STOP_SCRIPT, ev, env)
    assert r.returncode == 0, r.stderr
    assert "lib" in reminder_of(r.stdout) and "recx:manifest" in reminder_of(r.stdout)
    assert sh(STOP_SCRIPT, ev, env).stdout == ""

    env["RECX_DRIFT_REMINDER"] = "off"
    (repo / "tool.py").write_text("print(9)\n")
    assert sh(STOP_SCRIPT, ev, env).stdout == ""


def test_session_start_script_is_silent_and_zero_without_recx(repo, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = {"PATH": str(bindir), "CLAUDE_PLUGIN_DATA": str(tmp_path / "data")}
    r = sh(START_SCRIPT, json.dumps({"session_id": "x", "cwd": str(repo)}), env)
    assert (r.returncode, r.stdout) == (0, "")


def test_session_start_prunes_old_state_files(capsys, monkeypatch, tmp_path, repo):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    old, recent = data / "old-session.json", data / "recent-session.json"
    for f in (old, recent):
        f.write_text('{"head": "abcdef1", "reminded": []}')
    month_ago = __import__("time").time() - 31 * 86400
    os.utime(old, (month_ago, month_ago))
    start(capsys, monkeypatch, repo)
    assert not old.exists()
    assert recent.exists() and state_file(tmp_path).exists()
