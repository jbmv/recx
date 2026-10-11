"""`recx hook stop`: Claude Code's Stop hook, keeping a RECX.yaml valid as a session ends.

The hook must never trap a session on its own failure, so every error path
exits 0 without output. Only an invalid manifest produces output, and it is
always JSON on stdout with exit status 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from recx_cli.check import Unreadable, check_file, format_problem

MANIFEST = "RECX.yaml"
SKILL = "recx:manifest"


def _mode() -> str:
    value = os.environ.get("RECX_STOP_HOOK", "block").strip().lower()
    return value if value in ("block", "warn", "off") else "block"


def _manifest_dir(cwd: str) -> Path:
    start = Path(cwd)
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        top = out.stdout.strip()
        if out.returncode == 0 and top:
            return Path(top)
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def _run() -> None:
    if _mode() == "off":
        return
    raw = sys.stdin.read()
    if not raw.strip():
        return
    event = json.loads(raw)
    if not isinstance(event, dict):
        return
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return
    manifest = _manifest_dir(cwd) / MANIFEST
    if not manifest.is_file():
        return
    try:
        problems = check_file(manifest, repo=True)
    except Unreadable:
        return
    if not problems:
        return
    lines = "\n".join(format_problem(MANIFEST, p) for p in problems)
    if _mode() == "block" and event.get("stop_hook_active") is not True:
        reason = (
            f"{MANIFEST} is invalid. Fix every problem below, using the {SKILL} skill, "
            f"then check it with `recx check` (or `uvx --from recx-cli recx check`):\n{lines}"
        )
        print(json.dumps({"decision": "block", "reason": reason}))
    else:
        still = " still" if event.get("stop_hook_active") is True else ""
        context = (
            f"{MANIFEST} is{still} invalid. The {SKILL} skill explains how to fix it:\n{lines}"
        )
        print(
            json.dumps(
                {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": context}}
            )
        )


def stop() -> int:
    try:
        _run()
    except BaseException:  # noqa: BLE001 - a hook failure must not block the session
        pass
    return 0
