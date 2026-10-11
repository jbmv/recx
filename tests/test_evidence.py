import re

from recx_cli.cli import main

HEAD = """recx: "0.1"
name: example
summary: An example project.
license: MIT
status: experimental
capabilities:
  - do a thing
modules:
  - id: m
    path: RECX.yaml
    summary: The manifest.
    interface:
      type: other
      entrypoints: ["x"]
    extract:
      method: copy
evidence:
"""


def manifest(tmp_path, body):
    body = body.replace("    summary: s\n", "    summary: s\n    covers: [m]\n")
    (tmp_path / "RECX.yaml").write_text(HEAD + body)
    return tmp_path


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_runs_setup_then_run_in_order_from_manifest_dir(tmp_path, capsys, monkeypatch):
    d = manifest(
        tmp_path,
        "  - id: first\n    summary: s\n    setup: echo setup >> log\n    run: echo first >> log\n"
        "  - id: second\n    summary: s\n    run: echo second >> log\n",
    )
    monkeypatch.chdir(tmp_path.parent)  # not the manifest dir
    code, out, _ = run(capsys, "evidence", str(d))
    assert code == 0
    assert (d / "log").read_text().split() == ["setup", "first", "second"]
    lines = out.strip().splitlines()
    assert re.fullmatch(r"first: pass \(\d+\.\d+s\)", lines[0])
    assert re.fullmatch(r"second: pass \(\d+\.\d+s\)", lines[1])


def test_command_output_streams_through(tmp_path, capfd):
    d = manifest(tmp_path, "  - id: a\n    summary: s\n    run: echo hello-from-child\n")
    assert main(["evidence", str(d)]) == 0
    assert "hello-from-child" in capfd.readouterr().out


def test_failure_reports_exit_code_and_exits_1(tmp_path, capsys):
    d = manifest(
        tmp_path,
        "  - id: bad\n    summary: s\n    run: exit 3\n"
        "  - id: good\n    summary: s\n    run: 'true'\n",
    )
    code, out, _ = run(capsys, "evidence", str(d / "RECX.yaml"))
    assert code == 1
    lines = out.strip().splitlines()
    assert re.fullmatch(r"bad: fail \(\d+\.\d+s, exit 3\)", lines[0])
    assert lines[1].startswith("good: pass")


def test_failed_setup_skips_run(tmp_path, capsys):
    d = manifest(
        tmp_path,
        "  - id: a\n    summary: s\n    setup: exit 4\n    run: touch ran\n",
    )
    code, out, _ = run(capsys, "evidence", str(d))
    assert code == 1
    assert "a: fail" in out and "exit 4" in out
    assert not (d / "ran").exists()


def test_list_prints_commands_and_runs_nothing(tmp_path, capsys):
    d = manifest(
        tmp_path,
        "  - id: a\n    summary: s\n    setup: touch ran\n    run: touch ran2\n",
    )
    code, out, _ = run(capsys, "evidence", str(d), "--list")
    assert code == 0
    assert "a" in out and "setup: touch ran" in out and "run: touch ran2" in out
    assert not (d / "ran").exists() and not (d / "ran2").exists()


def test_only_selects_entries(tmp_path, capsys):
    d = manifest(
        tmp_path,
        "  - id: a\n    summary: s\n    run: touch a\n"
        "  - id: b\n    summary: s\n    run: touch b\n"
        "  - id: c\n    summary: s\n    run: touch c\n",
    )
    code, out, _ = run(capsys, "evidence", str(d), "--only", "c", "a")
    assert code == 0
    assert (d / "a").exists() and (d / "c").exists() and not (d / "b").exists()
    assert [l.split(":")[0] for l in out.strip().splitlines()] == ["a", "c"]
    code, out, _ = run(capsys, "evidence", str(d), "--list", "--only", "b")
    assert out.splitlines()[0] == "b" and "a" not in out.split()


def test_unknown_only_id_is_usage_error(tmp_path, capsys):
    d = manifest(tmp_path, "  - id: a\n    summary: s\n    run: touch a\n")
    code, _, err = run(capsys, "evidence", str(d), "--only", "nope")
    assert code == 2 and "nope" in err
    assert not (d / "a").exists()


def test_unreadable_file_is_usage_error(tmp_path, capsys):
    code, _, err = run(capsys, "evidence", str(tmp_path / "missing.yaml"))
    assert code == 2 and "cannot read" in err


def test_invalid_manifest_refuses_and_runs_nothing(tmp_path, capsys):
    d = manifest(
        tmp_path,
        "  - id: a\n    summary: s\n    run: touch ran\n    bogus_field: 1\n",
    )
    code, out, _ = run(capsys, "evidence", str(d))
    assert code == 1
    assert "bogus_field" in out and "pass" not in out
    assert not (d / "ran").exists()


def test_help_warns_about_strangers(capsys):
    try:
        main(["evidence", "--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "your own repository or inside a sandbox" in " ".join(out.split())
