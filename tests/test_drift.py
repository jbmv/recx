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


# -- RECX-24: the Recx-Checked trailer -------------------------------------


def commit(repo, message, *paths):
    git(repo, "add", "-A", *paths)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def edit_tool(repo, text="print(2)\n"):
    (repo / "tool.py").write_text(text)


def reminded(out):
    """The module ids a reminder names, or None for no output."""
    if out == "":
        return None
    data = json.loads(out)
    assert "decision" not in data
    text = data["hookSpecificOutput"]["additionalContext"]
    return text.split("did not: ")[1].split(". ")[0].split(", ")


@pytest.mark.parametrize(
    "trailers, ids",
    [
        ("Recx-Checked: lib, tool", {"lib", "tool"}),
        ("recx-checked: lib tool", {"lib", "tool"}),
        ("RECX-CHECKED:lib,,\ttool ,", {"lib", "tool"}),
        ("Recx-Checked: lib\nSigned-off-by: t\nrecx-Checked: tool", {"lib", "tool"}),
        ("Recx-Checked:", set()),
        ("Recx-Checked: , ,", set()),
        ("Recx-Checked lib", set()),
        ("Not-Recx-Checked: lib", set()),
        ("", set()),
    ],
)
def test_checked_ids_parsing(trailers, ids):
    assert drift.checked_ids(trailers) == ids


def test_trailer_covers_its_own_commit(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib\n\nRecx-Checked: lib")
    assert stop(capsys, monkeypatch, repo) == (0, "")


@pytest.mark.parametrize(
    "trailers",
    [
        "Recx-Checked: lib, tool",
        "recx-checked: lib tool",
        "RECX-CHECKED: lib\nRecx-Checked: tool",
        "Recx-Checked: tool,lib",
    ],
)
def test_trailer_key_case_repetition_and_separators(capsys, monkeypatch, repo, trailers):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    edit_tool(repo)
    commit(repo, f"change both\n\n{trailers}")
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_trailer_covers_only_the_modules_it_names(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    edit_tool(repo)
    commit(repo, "change both\n\nRecx-Checked: lib")
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["tool"]


def test_trailer_covers_earlier_commits_in_range(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib")
    (repo / "src" / "lib" / "b.py").write_text("b = 1\n")
    commit(repo, "more lib")
    commit(repo, "checked\n\nRecx-Checked: lib")
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_trailer_does_not_cover_later_commits(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib\n\nRecx-Checked: lib")
    (repo / "src" / "lib" / "a.py").write_text("a = 3\n")
    commit(repo, "change lib again")
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


def test_trailer_does_not_cover_uncommitted_changes(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib\n\nRecx-Checked: lib")
    (repo / "src" / "lib" / "new.py").write_text("n = 1\n")
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


def test_unknown_id_in_trailer_is_ignored(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib\n\nRecx-Checked: nosuch")
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


def test_unknown_id_beside_a_known_one(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib\n\nRecx-Checked: nosuch, lib")
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_trailer_must_be_in_the_trailer_block(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib\n\nRecx-Checked: lib\n\nA closing paragraph of prose.")
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


def test_committed_manifest_change_still_suppresses(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib")
    (repo / "RECX.yaml").write_text(MANIFEST + "tags: [x]\n")
    commit(repo, "touch manifest")
    edit_tool(repo)
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_merged_branch_with_trailer_is_covered(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    git(repo, "checkout", "-qb", "worker")
    edit_lib(repo)
    commit(repo, "worker changes lib")
    commit(repo, "worker checked\n\nRecx-Checked: lib")
    git(repo, "checkout", "-q", "-")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", "worker")
    assert stop(capsys, monkeypatch, repo) == (0, "")


def test_merge_does_not_cover_other_side(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    git(repo, "checkout", "-qb", "worker")
    edit_lib(repo)
    commit(repo, "worker changes lib\n\nRecx-Checked: lib")
    git(repo, "checkout", "-q", "-")
    (repo / "src" / "lib" / "other.py").write_text("o = 1\n")
    commit(repo, "main changes lib, unchecked")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", "worker")
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


def test_range_commits_reads_parents_trailers_and_files(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    edit_lib(repo)
    first = commit(repo, "one\n\nRecx-Checked: lib")
    git(repo, "mv", "tool.py", "tool2.py")
    second = commit(repo, "rename")
    commits = drift.range_commits(repo, base)
    assert [c.sha for c in commits] == [second, first]
    assert commits[0].parents == [first] and commits[1].parents == [base]
    assert commits[1].checked == {"lib"} and commits[0].checked == set()
    assert commits[1].files == {"src/lib/a.py"}
    assert commits[0].files == {"tool.py", "tool2.py"}


# -- RECX-24.c3: no recorded HEAD, and commits outside the range -----------


def test_without_recorded_head_trailers_change_nothing(capsys, monkeypatch, repo):
    edit_lib(repo)
    commit(repo, "change lib\n\nRecx-Checked: lib")
    assert stop(capsys, monkeypatch, repo) == (0, "")
    (repo / "src" / "lib" / "a.py").write_text("a = 5\n")
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


def test_trailer_before_the_session_has_no_effect(capsys, monkeypatch, repo):
    commit(repo, "checked earlier\n\nRecx-Checked: lib")
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib")
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


# -- RECX-24.c4/c5: SubagentStop -------------------------------------------


def sub_stop(capsys, monkeypatch, repo, **kw):
    kw.setdefault("agent_type", "general-purpose")
    kw.setdefault("agent_id", "a1")
    return stop(capsys, monkeypatch, repo, hook_event_name="SubagentStop", **kw)


def context_of(out, event):
    data = json.loads(out)
    assert "decision" not in data
    assert data["hookSpecificOutput"]["hookEventName"] == event
    return data["hookSpecificOutput"]["additionalContext"]


def test_subagent_stop_reminder_names_its_event(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    code, out = sub_stop(capsys, monkeypatch, repo)
    assert code == 0 and "lib" in context_of(out, "SubagentStop")


@pytest.mark.parametrize("name, expected", [("Stop", "Stop"), (None, "Stop"), ("PostToolUse", "Stop")])
def test_stop_event_name_is_stop(capsys, monkeypatch, repo, name, expected):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    kw = {} if name is None else {"hook_event_name": name}
    assert "lib" in context_of(stop(capsys, monkeypatch, repo, **kw)[1], expected)


def test_once_per_set_across_subagent_stop_and_stop(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    assert "lib" in context_of(sub_stop(capsys, monkeypatch, repo)[1], "SubagentStop")
    assert stop(capsys, monkeypatch, repo) == (0, "")
    assert sub_stop(capsys, monkeypatch, repo, agent_id="a2") == (0, "")
    edit_tool(repo)
    text = context_of(stop(capsys, monkeypatch, repo, hook_event_name="Stop")[1], "Stop")
    assert "lib" in text and "tool" in text
    assert sub_stop(capsys, monkeypatch, repo) == (0, "")


def test_once_per_set_from_stop_to_subagent_stop(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]
    assert sub_stop(capsys, monkeypatch, repo) == (0, "")


def test_subagent_stop_blocks_on_invalid_manifest(capsys, monkeypatch, repo):
    (repo / "RECX.yaml").write_text(INVALID)
    data = json.loads(sub_stop(capsys, monkeypatch, repo)[1])
    assert data["decision"] == "block" and "recx:manifest" in data["reason"]


@pytest.mark.parametrize("how", ["warn", "active"])
def test_subagent_stop_feedback_without_block(capsys, monkeypatch, repo, how):
    edit_lib(repo)
    (repo / "RECX.yaml").write_text(INVALID)
    kw = {}
    if how == "warn":
        monkeypatch.setenv("RECX_STOP_HOOK", "warn")
    else:
        kw["stop_hook_active"] = True
    code, out = sub_stop(capsys, monkeypatch, repo, **kw)
    assert code == 0 and len(out.strip().splitlines()) == 1
    text = context_of(out, "SubagentStop")
    assert "invalid" in text and "Reminder" not in text


def test_subagent_stop_off(capsys, monkeypatch, repo):
    edit_lib(repo)
    (repo / "RECX.yaml").write_text(INVALID)
    monkeypatch.setenv("RECX_STOP_HOOK", "off")
    assert sub_stop(capsys, monkeypatch, repo) == (0, "")


@pytest.mark.parametrize("agent_type", ["", None])
def test_internal_agent_subagent_stop_is_skipped(capsys, monkeypatch, repo, agent_type):
    # Claude Code fires SubagentStop for its own agents (prompt suggestions, /btw)
    # with an empty agent_type; they neither get the check nor spend the reminder.
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    kw = {"hook_event_name": "SubagentStop"}
    if agent_type is not None:
        kw["agent_type"] = agent_type
    assert stop(capsys, monkeypatch, repo, **kw) == (0, "")
    (repo / "RECX.yaml").write_text(INVALID)
    assert stop(capsys, monkeypatch, repo, **kw) == (0, "")
    (repo / "RECX.yaml").write_text(MANIFEST)
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


def test_subagent_in_another_worktree(capsys, monkeypatch, repo, tmp_path):
    # The session recorded HEAD in the main checkout; a subagent stops in a linked
    # worktree, which shares the object store, so the same range is readable there.
    start(capsys, monkeypatch, repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "worker", str(wt))
    edit_tool(repo, "print('parent edit')\n")
    (wt / "src" / "lib" / "a.py").write_text("a = 7\n")
    commit(wt, "worker changes lib\n\nRecx-Checked: lib")
    assert sub_stop(capsys, monkeypatch, wt) == (0, "")
    (wt / "src" / "lib" / "a.py").write_text("a = 8\n")
    commit(wt, "worker changes lib again")
    assert reminded(sub_stop(capsys, monkeypatch, wt)[1]) == ["lib"]
    assert sub_stop(capsys, monkeypatch, wt) == (0, "")
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["tool"]


def test_subagent_in_unrelated_repository_sees_uncommitted_only(capsys, monkeypatch, repo, tmp_path):
    start(capsys, monkeypatch, repo)
    other = tmp_path / "other"
    (other / "src" / "lib").mkdir(parents=True)
    (other / "src" / "lib" / "a.py").write_text("a = 1\n")
    (other / "tool.py").write_text("print(1)\n")
    (other / "RECX.yaml").write_text(MANIFEST)
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    commit(other, "a different first commit")
    (other / "src" / "lib" / "a.py").write_text("a = 2\n")
    commit(other, "committed lib change, outside the recorded range")
    assert sub_stop(capsys, monkeypatch, other) == (0, "")
    edit_tool(other)
    assert reminded(sub_stop(capsys, monkeypatch, other)[1]) == ["tool"]


def test_hooks_json_registers_subagent_stop():
    data = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text())
    for event in ("Stop", "SubagentStop"):
        entry = data["hooks"][event][0]
        assert entry["hooks"][0]["command"] == '"${CLAUDE_PLUGIN_ROOT}"/scripts/stop-hook.sh'
        assert "matcher" not in entry


# -- RECX-24.c6: failures reading commits or trailers ----------------------


def test_failing_range_read_falls_back_and_keeps_validity(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib\n\nRecx-Checked: lib")

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(drift, "range_commits", boom)
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]
    (repo / "RECX.yaml").write_text(INVALID)
    assert json.loads(stop(capsys, monkeypatch, repo)[1])["decision"] == "block"


def test_failing_git_log_falls_back(capsys, monkeypatch, repo):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, "change lib\n\nRecx-Checked: lib")
    real = drift._git

    def no_log(top, *args):
        if args and args[0] == "log":
            raise subprocess.CalledProcessError(128, "git log")
        return real(top, *args)

    monkeypatch.setattr(drift, "_git", no_log)
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


def test_shallow_history_never_blocks(capsys, monkeypatch, repo, tmp_path):
    first = git(repo, "rev-parse", "HEAD").strip()
    edit_lib(repo)
    commit(repo, "second")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True, capture_output=True
    )
    state_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state_file(tmp_path).write_text(json.dumps({"head": first, "reminded": []}))
    assert stop(capsys, monkeypatch, shallow) == (0, "")
    (shallow / "tool.py").write_text("print(5)\n")
    assert reminded(stop(capsys, monkeypatch, shallow)[1]) == ["tool"]
    (shallow / "RECX.yaml").write_text(INVALID)
    assert json.loads(stop(capsys, monkeypatch, shallow)[1])["decision"] == "block"


@pytest.mark.parametrize(
    "message",
    [
        "x\n\nRecx-Checked:",
        "x\n\nRecx-Checked: ,,,",
        "x\n\nRecx-Checked: éè \x1e\x1f lib-ish",
        "x\n\nRecx-Checked: " + "a" * 5000,
    ],
)
def test_malformed_trailer_neither_raises_nor_covers(capsys, monkeypatch, repo, message):
    start(capsys, monkeypatch, repo)
    edit_lib(repo)
    commit(repo, message)
    assert reminded(stop(capsys, monkeypatch, repo)[1]) == ["lib"]


def test_scripts_end_to_end_subagent_stop(repo, tmp_path):
    recx_dir = Path(sys.executable).parent
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "RECX_STOP_HOOK", "RECX_DRIFT_REMINDER")}
    env["PATH"] = f"{recx_dir}{os.pathsep}{env['PATH']}"
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "data")
    env.update(GIT_ENV)
    base = {"session_id": "e2e", "cwd": str(repo), "stop_hook_active": False}
    assert sh(START_SCRIPT, json.dumps({**base, "hook_event_name": "SessionStart"}), env).stdout == ""
    edit_lib(repo)
    git(repo, "commit", "-qam", "change lib\n\nRecx-Checked: lib")
    sub = {**base, "hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": "general-purpose"}
    assert sh(STOP_SCRIPT, json.dumps(sub), env).stdout == ""
    (repo / "tool.py").write_text("print(9)\n")
    r = sh(STOP_SCRIPT, json.dumps(sub), env)
    assert r.returncode == 0, r.stderr
    assert "tool" in context_of(r.stdout, "SubagentStop")
    assert sh(STOP_SCRIPT, json.dumps({**base, "hook_event_name": "Stop"}), env).stdout == ""
