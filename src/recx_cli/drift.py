"""Drift reminder: a manifest can be valid and stale.

`recx hook session-start` records the repository's HEAD for the session.
`recx hook stop` then asks which modules have changed files since, and, when
RECX.yaml is not among them, reminds once per set of modules. The reminder is
only ever feedback, never a block. Callers swallow every exception raised here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from recx_cli.check import parse

MANIFEST = "RECX.yaml"
SKILL = "recx:manifest"
_HEAD = re.compile(r"[0-9a-fA-F]{7,64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def enabled() -> bool:
    return os.environ.get("RECX_DRIFT_REMINDER", "").strip().lower() != "off"


def state_dir() -> Path:
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "recx"


def _state_file(session_id: str) -> Path | None:
    if not isinstance(session_id, str) or not _SAFE_ID.fullmatch(session_id):
        return None
    return state_dir() / f"{session_id}.json"


def read_state(session_id: str) -> dict:
    """The session's state, or {} when absent, unreadable or corrupt."""
    try:
        path = _state_file(session_id)
        if path is None:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        state: dict = {}
        head = data.get("head")
        if isinstance(head, str) and _HEAD.fullmatch(head):
            state["head"] = head
        reminded = data.get("reminded")
        if isinstance(reminded, list):
            state["reminded"] = [m for m in reminded if isinstance(m, str)]
        return state
    except Exception:  # noqa: BLE001 - a bad state file is ignored
        return {}


def write_state(session_id: str, state: dict) -> None:
    path = _state_file(session_id)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _git(top: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(top), *args], capture_output=True, text=True, timeout=20, check=True
    )
    return out.stdout


def head_of(top: Path) -> str | None:
    value = _git(top, "rev-parse", "HEAD").strip()
    return value if _HEAD.fullmatch(value) else None


STATE_MAX_AGE_DAYS = 30


def prune_state(max_age_days: int = STATE_MAX_AGE_DAYS) -> None:
    """Delete session state files older than max_age_days; one is written per session."""
    try:
        cutoff = time.time() - max_age_days * 86400
        for path in state_dir().glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass


def session_start(session_id: str, top: Path) -> None:
    """Record HEAD for the session. Skips when there is no manifest or the reminder is off."""
    if not enabled() or not (top / MANIFEST).is_file():
        return
    prune_state()
    head = head_of(top)
    if head:
        write_state(session_id, {"head": head, "reminded": []})


def changed_files(top: Path, head: str | None) -> set[str]:
    files: set[str] = set()
    for base in [head, "HEAD"] if head else ["HEAD"]:
        try:
            files.update(_git(top, "diff", "--name-only", "-z", base, "--").split("\0"))
            break
        except (OSError, subprocess.SubprocessError):
            continue
    files.update(_git(top, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    files.discard("")
    return files


def affected_modules(manifest: Path, files: set[str]) -> list[str]:
    data = parse(manifest.read_text(encoding="utf-8"))
    modules = data.get("modules") if isinstance(data, dict) else None
    found: list[str] = []
    for mod in modules if isinstance(modules, list) else []:
        if not isinstance(mod, dict):
            continue
        mid, path = mod.get("id"), mod.get("path")
        if not isinstance(mid, str) or not isinstance(path, str):
            continue
        if path.endswith("/"):
            hit = any(f.startswith(path) for f in files)
        else:
            hit = path in files
        if hit and mid not in found:
            found.append(mid)
    return found


def reminder(session_id: str | None, top: Path) -> str | None:
    """The reminder text to emit now, or None. Records what it reminded about."""
    if not enabled():
        return None
    state = read_state(session_id) if session_id else {}
    files = changed_files(top, state.get("head"))
    if MANIFEST in files:
        return None
    affected = affected_modules(top / MANIFEST, files)
    already = set(state.get("reminded", []))
    if not affected or set(affected) <= already:
        return None
    if session_id:
        try:
            write_state(session_id, {**state, "reminded": sorted(already | set(affected))})
        except Exception:  # noqa: BLE001 - failing to record must not lose the reminder
            pass
    names = ", ".join(affected)
    return (
        f"Reminder, not an error: files under these {MANIFEST} modules changed this session "
        f"while {MANIFEST} did not: {names}. Check whether the manifest still describes them, "
        f"using the {SKILL} skill, and update it if it does not. If it is still accurate, "
        f"nothing needs doing."
    )
