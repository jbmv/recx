"""Drift reminder: a manifest can be valid and stale.

`recx hook session-start` records the repository's HEAD for the session.
`recx hook stop` (run on Stop and on SubagentStop) then asks which modules have
changed files since, and, when RECX.yaml is not among them, reminds once per set
of modules. A commit whose message carries the trailer
`Recx-Checked: <module-id>[, <module-id>...]` records that those modules' entries
were checked against the tree at that commit, so its changes to them, and those
of its ancestors in the session's range, do not count. The reminder is only ever
feedback, never a block. Callers swallow every exception raised here.
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
        agents = data.get("agents")
        if isinstance(agents, dict):
            state["agents"] = {
                a: [m for m in r if isinstance(m, str)]
                for a, r in agents.items()
                if isinstance(a, str) and _SAFE_ID.fullmatch(a) and isinstance(r, list)
            }
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


_TRAILER_LINE = re.compile(r"\s*recx-checked\s*:(.*)", re.IGNORECASE)
_ID_SEP = re.compile(r"[,\s]+")


def _uncommitted(top: Path) -> set[str]:
    """Files changed against HEAD in the index or working tree, plus untracked files."""
    files = set(_git(top, "diff", "--name-only", "-z", "HEAD", "--").split("\0"))
    files.update(_git(top, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    files.discard("")
    return files


def changed_files(top: Path, head: str | None) -> set[str]:
    """The net change since head (or HEAD) plus untracked files: the reading without trailers."""
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


def checked_ids(trailers: str) -> set[str]:
    """Module ids named by Recx-Checked trailers; anything malformed names nothing."""
    ids: set[str] = set()
    for line in trailers.splitlines():
        m = _TRAILER_LINE.fullmatch(line)
        if m:
            ids.update(i for i in _ID_SEP.split(m.group(1)) if i)
    return ids


class Commit:
    __slots__ = ("sha", "parents", "checked", "files")

    def __init__(self, sha: str, parents: list[str], checked: set[str], files: set[str]) -> None:
        self.sha, self.parents, self.checked, self.files = sha, parents, checked, files


def range_commits(top: Path, head: str) -> list[Commit]:
    """Every commit in head..HEAD with its parents, Recx-Checked ids and touched files.

    Two `git log` calls: one for the graph and the files, which holds no free text,
    and one for the trailers, NUL-separated, since a commit message cannot hold a
    NUL. Merge commits list no files of their own; the commits they bring in are in
    the range themselves. Raises when git fails (unknown head, shallow history, no
    git); a record that does not parse is skipped.
    """
    rng = f"{head}..HEAD"
    graph = _git(
        top, "log", "-z", "--no-renames", "--no-ext-diff", "--format=%x1e%H %P%x1f", "--name-only", rng, "--"
    )
    trailers = _git(top, "log", "-z", "--format=%H%n%(trailers:only,unfold)", rng, "--")
    checked: dict[str, set[str]] = {}
    for record in trailers.split("\0"):
        sha, _, text = record.partition("\n")
        if _HEAD.fullmatch(sha):
            checked[sha] = checked_ids(text)
    commits: list[Commit] = []
    for record in graph.split("\x1e"):
        header, sep, rest = record.partition("\x1f")
        shas = header.split()
        if not sep or not shas or not all(_HEAD.fullmatch(x) for x in shas):
            continue
        rest = rest.lstrip("\0")
        if rest.startswith("\n"):
            rest = rest[1:]
        files = {f for f in rest.split("\0") if f}
        commits.append(Commit(shas[0], shas[1:], checked.get(shas[0], set()), files))
    return commits


def _covered(commits: list[Commit], module_id: str) -> set[str]:
    """Commits in the range that a Recx-Checked trailer naming module_id vouches for.

    A check vouches for the tree it was made on, so a checking commit covers itself
    and every ancestor of it within the range. Every path from a commit in the range
    to an ancestor in the range stays inside the range, so walking parents that are
    in the range finds exactly those ancestors.
    """
    by_sha = {c.sha: c for c in commits}
    covered: set[str] = set()
    stack = [c.sha for c in commits if module_id in c.checked]
    while stack:
        sha = stack.pop()
        if sha in covered:
            continue
        covered.add(sha)
        stack.extend(p for p in by_sha[sha].parents if p in by_sha and p not in covered)
    return covered


def _modules(manifest: Path) -> list[tuple[str, str]]:
    data = parse(manifest.read_text(encoding="utf-8"))
    modules = data.get("modules") if isinstance(data, dict) else None
    found: list[tuple[str, str]] = []
    for mod in modules if isinstance(modules, list) else []:
        if not isinstance(mod, dict):
            continue
        mid, path = mod.get("id"), mod.get("path")
        if isinstance(mid, str) and isinstance(path, str):
            found.append((mid, path))
    return found


def _touches(path: str, files: set[str]) -> bool:
    if path.endswith("/"):
        return any(f.startswith(path) for f in files)
    return path in files


def affected_modules(manifest: Path, files: set[str], commits: list[Commit] | None = None) -> list[str]:
    """Modules whose path is touched by files, or by a commit no trailer for them covers."""
    found: list[str] = []
    for mid, path in _modules(manifest):
        if mid in found:
            continue
        hit = _touches(path, files)
        if not hit and commits:
            covered = _covered(commits, mid)
            hit = any(c.sha not in covered and _touches(path, c.files) for c in commits)
        if hit:
            found.append(mid)
    return found


def _changes(top: Path, head: str | None) -> tuple[set[str], list[Commit] | None]:
    """(files changed outside any commit's trailer, the session's commits).

    With a recorded head whose range git can read, the files are the uncommitted
    and untracked ones and the commits are read one by one. Otherwise (no head,
    or any failure reading the range) this is the reading without trailers: the
    net change since head, or the uncommitted change alone, with no commits.
    """
    if head:
        try:
            return _uncommitted(top), range_commits(top, head)
        except Exception:  # noqa: BLE001 - reading commits or trailers never costs the reminder
            pass
    return changed_files(top, head), None


def reminder(session_id: str | None, top: Path, agent_id: str | None = None) -> str | None:
    """The reminder text to emit now, or None. Records what it reminded about.

    The session and each subagent (by agent_id) keep their own record of what they
    were reminded about, so a subagent that is reminded never silences the session.
    """
    if not enabled():
        return None
    state = read_state(session_id) if session_id else {}
    files, commits = _changes(top, state.get("head"))
    if MANIFEST in files or any(MANIFEST in c.files for c in commits or []):
        return None
    affected = affected_modules(top / MANIFEST, files, commits)
    if agent_id is not None and not _SAFE_ID.fullmatch(agent_id):
        agent_id = None
    agents = state.get("agents", {})
    already = set(agents.get(agent_id, []) if agent_id else state.get("reminded", []))
    if not affected or set(affected) <= already:
        return None
    if session_id:
        seen = sorted(already | set(affected))
        update = {"agents": {**agents, agent_id: seen}} if agent_id else {"reminded": seen}
        try:
            write_state(session_id, {**state, **update})
        except Exception:  # noqa: BLE001 - failing to record must not lose the reminder
            pass
    names = ", ".join(affected)
    return (
        f"Reminder, not an error: files under these {MANIFEST} modules changed this session "
        f"while {MANIFEST} did not: {names}. Check whether the manifest still describes them, "
        f"using the {SKILL} skill, and update it if it does not. If it is still accurate, "
        f"nothing needs doing."
    )
