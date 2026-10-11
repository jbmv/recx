"""Validate a RECX.yaml manifest: the schema plus the rules SPEC.md adds to it.

Manifest text is untrusted. It is parsed with a restricted YAML loader, never
evaluated, and anything echoed back is escaped and truncated.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

MAX_BYTES = 64 * 1024  # SPEC.md rule 5
ROOT = "/"  # how the whole document is named in an error line
_SCHEMA_NAME = "recx.schema.json"


@dataclass(frozen=True)
class Problem:
    pointer: str
    message: str


def format_problem(label: str, problem: Problem) -> str:
    """One problem as `recx check` prints it."""
    return f"{label}: {problem.pointer}: {problem.message}"


# -- schema ---------------------------------------------------------------


def load_schema() -> dict:
    """The bundled schema; in a source checkout, spec/recx.schema.json itself."""
    try:
        text = (resources.files("recx_cli") / _SCHEMA_NAME).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        # Not built into a wheel (editable install or plain checkout): the one
        # copy under version control sits at <repo>/spec/.
        text = (Path(__file__).resolve().parents[2] / "spec" / _SCHEMA_NAME).read_text(
            encoding="utf-8"
        )
    return json.loads(text)


# -- parsing (rule 6) -----------------------------------------------------


class ManifestError(Exception):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


_MERGE_TAG = "tag:yaml.org,2002:merge"


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses anchors, aliases, merge keys and repeated keys."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            event = self.peek_event()
            raise ManifestError(
                "aliases (*name) are not allowed (SPEC.md rule 6)",
                event.start_mark.line + 1,
            )
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise ManifestError(
                "anchors (&name) are not allowed (SPEC.md rule 6)",
                event.start_mark.line + 1,
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen: set = set()
            for key_node, _ in node.value:
                line = key_node.start_mark.line + 1
                if key_node.tag == _MERGE_TAG:
                    raise ManifestError(
                        "merge keys (<<) are not allowed (SPEC.md rule 6)", line
                    )
                if isinstance(key_node, yaml.ScalarNode):
                    key = self.construct_object(key_node)
                    try:
                        if key in seen:
                            raise ManifestError(
                                f"mapping key {_show(key)} is repeated (SPEC.md rule 6)",
                                line,
                            )
                        seen.add(key)
                    except TypeError:
                        pass
        return super().construct_mapping(node, deep=deep)


def parse(text: str) -> Any:
    try:
        return yaml.load(text, Loader=StrictLoader)  # noqa: S506 - SafeLoader subclass
    except ManifestError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise ManifestError(
            f"invalid YAML: {problem}", mark.line + 1 if mark is not None else None
        ) from None


# -- output hygiene -------------------------------------------------------


# Control characters, plus the line separators and bidi controls a terminal would
# otherwise act on: a bidi override can make echoed manifest text read differently
# from what it is.
_UNSAFE = re.compile(r"[^\x20-\x7e -\U0010ffff]|[؜‎‏ -‮⁦-⁩]")


def _clean(text: str, limit: int = 200) -> str:
    if len(text) > limit:
        text = text[:limit] + "..."
    return _UNSAFE.sub(
        lambda m: "\\x%02x" % ord(m.group()) if ord(m.group()) < 256 else "\\u%04x" % ord(m.group()),
        text,
    )


def _show(value: Any) -> str:
    return _clean(repr(value), 80)


def _pointer(parts) -> str:
    if not parts:
        return ROOT
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


# -- schema errors, with quoting hints ------------------------------------

_YAML_SCALAR_KINDS = (
    (bool, "a boolean"),
    (int, "an integer"),
    (float, "a number"),
    (datetime.datetime, "a date-time"),
    (datetime.date, "a date"),
)


def _yaml_kind(value: Any) -> str | None:
    for cls, name in _YAML_SCALAR_KINDS:
        if isinstance(value, cls):
            return name
    return None


def _expects_string(error) -> bool:
    if error.validator == "type":
        expected = error.validator_value
        return "string" in (expected if isinstance(expected, list) else [expected])
    if error.validator in ("enum", "const"):
        options = error.validator_value if error.validator == "enum" else [error.validator_value]
        return any(isinstance(o, str) for o in options)
    return False


def _value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def schema_problems(data: Any, schema: dict) -> list[Problem]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path])
    type_error_at = {
        _pointer(e.absolute_path)
        for e in errors
        if e.validator == "type" and _yaml_kind(e.instance) and _expects_string(e)
    }
    problems = []
    for error in errors:
        pointer = _pointer(error.absolute_path)
        kind = _yaml_kind(error.instance)
        if kind and _expects_string(error):
            value = _clean(_value_text(error.instance), 60)
            if error.validator != "type" and pointer in type_error_at:
                continue  # the type error says it all
            problems.append(
                Problem(
                    pointer,
                    f"expected a string, but YAML read {value} as {kind}; "
                    f'quote the value (for example "{value}")',
                )
            )
            continue
        problems.append(Problem(pointer, _clean(error.message)))
    return problems


# -- rules 1, 2 and 3 -----------------------------------------------------


def _entries(data: Any, key: str) -> list[tuple[int, dict]]:
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [(i, item) for i, item in enumerate(items) if isinstance(item, dict)]


def rule_problems(data: Any) -> list[Problem]:
    problems: list[Problem] = []
    modules = _entries(data, "modules")
    evidence = _entries(data, "evidence")

    module_ids: set[str] = set()
    first_seen: dict[str, str] = {}
    for i, mod in modules:
        mid = mod.get("id")
        if not isinstance(mid, str):
            continue
        where = f"/modules/{i}/id"
        if mid in first_seen:
            problems.append(
                Problem(where, f"duplicate module id {_show(mid)} (rule 1; first used at {first_seen[mid]})")
            )
        else:
            first_seen[mid] = where
        module_ids.add(mid)

    seen_ev: dict[str, str] = {}
    for i, ev in evidence:
        eid = ev.get("id")
        if not isinstance(eid, str):
            continue
        where = f"/evidence/{i}/id"
        if eid in seen_ev:
            problems.append(
                Problem(where, f"duplicate evidence id {_show(eid)} (rule 1; first used at {seen_ev[eid]})")
            )
        else:
            seen_ev[eid] = where

    def check_refs(base: str, refs: Any, what: str) -> None:
        if not isinstance(refs, list):
            return
        for j, ref in enumerate(refs):
            if isinstance(ref, str) and ref not in module_ids:
                problems.append(
                    Problem(f"{base}/{j}", f"{what} {_show(ref)} names no module in this manifest (rule 2)")
                )

    for i, mod in modules:
        requires = mod.get("requires")
        if isinstance(requires, dict):
            check_refs(f"/modules/{i}/requires/modules", requires.get("modules"), "required module")
    for i, ev in evidence:
        check_refs(f"/evidence/{i}/covers", ev.get("covers"), "covered module")
    return problems


def path_problems(data: Any, base: Path, skip: set[str]) -> list[Problem]:
    """Rule 3. Paths the schema already rejected are never touched on disk."""
    problems = []
    for i, mod in _entries(data, "modules"):
        path = mod.get("path")
        pointer = f"/modules/{i}/path"
        if not isinstance(path, str) or pointer in skip:
            continue
        target = base / path
        try:
            exists = target.exists()
            is_dir = target.is_dir()
        except OSError as exc:
            problems.append(Problem(pointer, f"cannot inspect path: {_clean(exc.strerror or 'error')} (rule 3)"))
            continue
        shown = _show(path)
        if not exists:
            problems.append(Problem(pointer, f"path {shown} does not exist relative to the manifest (rule 3)"))
        elif path.endswith("/") and not is_dir:
            problems.append(Problem(pointer, f"path {shown} ends in / but is a file (rule 3)"))
        elif not path.endswith("/") and is_dir:
            problems.append(Problem(pointer, f"path {shown} is a directory, so it must end in / (rule 3)"))
    return problems


# -- entry point ----------------------------------------------------------


class Unreadable(Exception):
    pass


def check_file(path: Path, *, repo: bool = True) -> list[Problem]:
    """Return every problem with the manifest at `path`; raise Unreadable if it cannot be read."""
    try:
        size = path.stat().st_size
        if not path.is_file():
            raise Unreadable("not a regular file")
        if size > MAX_BYTES:
            return [Problem(ROOT, f"file is {size} bytes, over the 64 KiB limit (rule 5)")]
        raw = path.read_bytes()
    except OSError as exc:
        raise Unreadable(_clean(exc.strerror or str(exc))) from None
    if len(raw) > MAX_BYTES:
        return [Problem(ROOT, f"file is {len(raw)} bytes, over the 64 KiB limit (rule 5)")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [Problem(ROOT, "file is not valid UTF-8")]

    problems, data, ok = _check_decoded(text)
    if repo and ok:
        skip = {p.pointer for p in problems}
        problems += path_problems(data, path.resolve().parent, skip)
    return problems


def check_text(text: str) -> list[Problem]:
    """Rules 1, 2, 5 and 6 and the schema, for manifest text with no file or repository behind it.

    Gives the same problems as `check_file(path, repo=False)` for a file holding
    the same text. Invalid UTF-8 cannot be a `str`, so that one problem is only
    reachable through `check_file`.
    """
    # surrogatepass: a lone surrogate cannot be UTF-8, but must not crash the size check
    size = len(text.encode("utf-8", errors="surrogatepass"))
    if size > MAX_BYTES:
        return [Problem(ROOT, f"file is {size} bytes, over the 64 KiB limit (rule 5)")]
    return _check_decoded(text)[0]


def _check_decoded(text: str) -> tuple[list[Problem], Any, bool]:
    """Parse, schema and rules 1-2. The bool is False when parsing failed."""
    try:
        data = parse(text)
    except ManifestError as exc:
        where = f"line {exc.line}: " if exc.line else ""
        return [Problem(ROOT, f"{where}{_clean(str(exc))}")], None, False

    problems = schema_problems(data, load_schema())
    problems += rule_problems(data)
    return problems, data, True
