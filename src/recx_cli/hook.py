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

from recx_cli import drift
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


def _read_event() -> tuple[dict, Path] | None:
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    event = json.loads(raw)
    if not isinstance(event, dict):
        return None
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    return event, _manifest_dir(cwd)


def _validity(event: dict, top: Path) -> tuple[bool, str | None]:
    """(manifest is valid, output to print). Knows nothing about drift."""
    manifest = top / MANIFEST
    if not manifest.is_file():
        return False, None
    try:
        problems = check_file(manifest, repo=True)
    except Unreadable:
        return False, None
    if not problems:
        return True, None
    lines = "\n".join(format_problem(MANIFEST, p) for p in problems)
    if _mode() == "block" and event.get("stop_hook_active") is not True:
        reason = (
            f"{MANIFEST} is invalid. Fix every problem below, using the {SKILL} skill, "
            f"then check it with `recx check` (or `uvx --from recx-cli recx check`):\n{lines}"
        )
        return False, json.dumps({"decision": "block", "reason": reason})
    still = " still" if event.get("stop_hook_active") is True else ""
    context = f"{MANIFEST} is{still} invalid. The {SKILL} skill explains how to fix it:\n{lines}"
    return False, json.dumps(
        {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": context}}
    )


def _drift(event: dict, top: Path) -> str | None:
    try:
        session = event.get("session_id")
        text = drift.reminder(session if isinstance(session, str) else None, top)
    except BaseException:  # noqa: BLE001 - the drift path must never block or raise
        return None
    if text is None:
        return None
    return json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}})


def _run() -> None:
    if _mode() == "off":
        return
    parsed = _read_event()
    if parsed is None:
        return
    event, top = parsed
    valid, output = _validity(event, top)
    if output is not None:
        print(output)
    elif valid:
        extra = _drift(event, top)
        if extra is not None:
            print(extra)


def _session_start() -> None:
    if _mode() == "off" or not drift.enabled():
        return
    parsed = _read_event()
    if parsed is None:
        return
    event, top = parsed
    session = event.get("session_id")
    if isinstance(session, str):
        drift.session_start(session, top)


def session_start() -> int:
    try:
        _session_start()
    except BaseException:  # noqa: BLE001 - a hook failure must not block the session
        pass
    return 0


def stop() -> int:
    try:
        _run()
    except BaseException:  # noqa: BLE001 - a hook failure must not block the session
        pass
    return 0
