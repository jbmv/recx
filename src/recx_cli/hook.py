"""`recx hook stop`: Claude Code's Stop and SubagentStop hook, keeping a RECX.yaml valid.

The same entry point serves both events; the event's own `hook_event_name` is
named back in any `hookSpecificOutput`. The hook must never trap a session on
its own failure, so every error path exits 0 without output. Output is always
JSON on stdout with exit status 0.
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
EVENTS = ("Stop", "SubagentStop")


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


def _event_name(event: dict) -> str:
    name = event.get("hook_event_name")
    return name if name in EVENTS else "Stop"


def _internal_agent(event: dict) -> bool:
    """A SubagentStop from one of Claude Code's own agents (prompt suggestions, /btw).

    Claude Code documents an empty `agent_type` for those when the session runs
    without --agent. Such an agent did not do the session's work and cannot act on
    feedback, so neither the check nor the reminder is spent on it.
    """
    return _event_name(event) == "SubagentStop" and not event.get("agent_type")


def _context(event: dict, text: str) -> str:
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": _event_name(event), "additionalContext": text}}
    )


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
    return False, _context(event, context)


def _drift(event: dict, top: Path) -> str | None:
    try:
        session = event.get("session_id")
        agent = event.get("agent_id") if _event_name(event) == "SubagentStop" else None
        text = drift.reminder(
            session if isinstance(session, str) else None,
            top,
            agent if isinstance(agent, str) else None,
        )
    except BaseException:  # noqa: BLE001 - the drift path must never block or raise
        return None
    if text is None:
        return None
    return _context(event, text)


def _run() -> None:
    if _mode() == "off":
        return
    parsed = _read_event()
    if parsed is None:
        return
    event, top = parsed
    if _internal_agent(event):
        return
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
