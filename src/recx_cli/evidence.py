"""`recx evidence`: run a manifest's own evidence commands, in the repository owner's CI."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from recx_cli.check import Unreadable, check_file, format_problem, parse

HELP = (
    "Run each evidence entry's setup and run commands (sh -c, from the manifest's directory, "
    "with your environment) and report pass or fail. This runs the manifest's commands: use it "
    "on your own repository or inside a sandbox, never on a stranger's manifest outside one "
    "(SPEC, Trust). The manifest must pass `recx check` first. "
    "Exit status: 0 all selected entries passed, 1 an entry failed or the manifest is invalid, "
    "2 usage error or unreadable file."
)


def _run(command: str, cwd: Path) -> int:
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run(["sh", "-c", command], cwd=cwd).returncode


def evidence(path: str, list_only: bool = False, only: list[str] | None = None) -> int:
    target = Path(path)
    if target.is_dir():
        target = target / "RECX.yaml"
    label = str(target)
    try:
        problems = check_file(target, repo=True)
    except Unreadable as exc:
        print(f"{label}: cannot read: {exc}", file=sys.stderr)
        return 2
    if problems:
        for p in problems:
            print(format_problem(label, p))
        return 1
    entries = parse(target.read_text(encoding="utf-8")).get("evidence") or []
    if only:
        known = [e["id"] for e in entries]
        unknown = [i for i in only if i not in known]
        if unknown:
            print(
                f"recx evidence: no evidence entry with id {', '.join(unknown)} "
                f"(known: {', '.join(known) or 'none'})",
                file=sys.stderr,
            )
            return 2
        entries = [e for e in entries if e["id"] in only]
    if list_only:
        for e in entries:
            print(e["id"])
            if "setup" in e:
                print(f"  setup: {e['setup']}")
            print(f"  run: {e['run']}")
        return 0
    cwd = target.resolve().parent
    failed = False
    for e in entries:
        start = time.monotonic()
        code = 0
        for step in ("setup", "run"):
            if step in e:
                code = _run(e[step], cwd)
                if code != 0:
                    break
        secs = f"{time.monotonic() - start:.1f}s"
        if code == 0:
            print(f"{e['id']}: pass ({secs})", flush=True)
        else:
            failed = True
            print(f"{e['id']}: fail ({secs}, exit {code})", flush=True)
    return 1 if failed else 0
