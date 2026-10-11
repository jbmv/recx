"""The crawler's decisions, offline (docs/design/search.md, section 2).

`build` turns the previous index, this run's candidates and what the client fetched for them
into the new index and the rejections file. It is pure: no network, no clock (`now` is passed
in), and no file reads beyond the spec's own schema, which `check_text` and the licence
allowlist load. `load_list` is the one helper that reads an input file, for seeds.txt and
blocklist.txt.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from crawler.model import RECHECK, Candidate, CrawlResult, Fetched
from recx_cli.check import MAX_BYTES, Problem, check_text, load_schema, parse
from recx_cli.index import project

INDEX_FORMAT = "recx-index/1.0"
REJECTED_FORMAT = "recx-rejected/1.0"

# Reason codes in rejected.json. Fixed strings only: never text from a manifest.
SCHEMA = "schema"
YAML = "yaml"
UTF8 = "utf-8"
LICENSE_NOT_PERMISSIVE = "license-not-permissive"
BLOCKED = "blocked"
FORK_DUPLICATE = "fork-duplicate"
_RULE_CODES = {"1": "rule-1", "2": "rule-2", "5": "rule-5", "6": "rule-6"}

_UNDETECTED = {"noassertion", "other"}
_SHA = re.compile(r"^[0-9a-f]{40}$")
# check.py ends each rule message with its marker: "(rule 2)", "(SPEC.md rule 6)" or
# "(rule 1; first used at ...)". Anchoring at the end keeps author text quoted earlier in a
# message from choosing the code.
_RULE_MARKER = re.compile(r"\((?:SPEC\.md )?rule (\d+)(?:;[^()]*)?\)$")
_YAML_MARKER = re.compile(r"^(?:line \d+: )?invalid YAML\b")


def load_list(path: str | Path) -> set[str]:
    """Read seeds.txt or blocklist.txt: one owner or owner/name per line, lowercased.

    Blank lines and lines starting with `#` are skipped, as is anything after a `#` on a line.
    """
    out: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        item = line.split("#", 1)[0].strip()
        if item:
            out.add(item.lower())
    return out


def license_allowlist() -> frozenset[str]:
    """The spec's permissive licences, from recx.schema.json's `$defs.license.enum`."""
    return frozenset(load_schema()["$defs"]["license"]["enum"])


def reason_codes(problems: Iterable[Problem]) -> list[str]:
    """Map check_text problems to sorted, distinct reason codes. Messages are never copied."""
    codes = set()
    for p in problems:
        m = _RULE_MARKER.search(p.message)
        if m and m.group(1) in _RULE_CODES:
            codes.add(_RULE_CODES[m.group(1)])
        elif _YAML_MARKER.match(p.message):
            codes.add(YAML)
        elif p.message == "file is not valid UTF-8":
            codes.add(UTF8)
        else:
            codes.add(SCHEMA)
    return sorted(codes)


def license_check(declared: str | None, detected: str | None, allowlist: frozenset[str]) -> str | None:
    """`match`, `undetected` or `mismatch`; None means reject as `license-not-permissive`."""
    if detected is not None and declared is not None and detected.casefold() == declared.casefold():
        return "match"
    if detected is None or detected.casefold() in _UNDETECTED:
        return "undetected"
    if detected.casefold() in {a.casefold() for a in allowlist}:
        return "mismatch"
    return None


def declared_repository(manifest: dict, full_name: str) -> str:
    """`absent`, `matches` or `differs`: does the manifest's `repository` name this repository?"""
    value = manifest.get("repository")
    if not isinstance(value, str):
        return "absent"
    url = value.strip().rstrip("/")
    if url.casefold().endswith(".git"):
        url = url[:-4].rstrip("/")
    return "matches" if url.casefold() == f"https://github.com/{full_name}".casefold() else "differs"


def is_blocked(full_name: str, blocklist: set[str]) -> bool:
    name = full_name.lower()
    owner = name.split("/", 1)[0]
    return name in blocklist or owner in blocklist


@dataclass
class _Run:
    """Mutable state for one build: what is indexed, rejected and noted so far."""

    entries: dict[str, dict]  # key: lowercased full_name
    rejected: dict[str, tuple[str, list[str]]]  # key: lowercased full_name -> (full_name, reasons)
    notes: list[str]

    def reject(self, full_name: str, reasons: list[str]) -> None:
        self.rejected[full_name.lower()] = (full_name, sorted(set(reasons)))


def _merge_candidates(candidates: Iterable[Candidate]) -> dict[str, Candidate]:
    """De-duplicate by full_name, case-insensitively, merging found_by. The first spelling wins."""
    merged: dict[str, Candidate] = {}
    for c in candidates:
        key = c.full_name.lower()
        if key not in merged:
            merged[key] = c
            continue
        prev = merged[key]
        merged[key] = replace(
            prev,
            found_by=prev.found_by | c.found_by,
            node_id=prev.node_id or c.node_id,
            blob_sha=prev.blob_sha or c.blob_sha,
            commit=prev.commit or c.commit,
        )
    return merged


def _incomplete(f: Fetched) -> bool:
    """True when a fetched record lacks a field an index entry requires."""
    return (
        not f.default_branch
        or f.commit is None
        or not _SHA.match(f.commit)
        or f.blob_sha is None
        or not _SHA.match(f.blob_sha)
        or f.created_at is None
        or f.pushed_at is None
    )


def _source(
    f: Fetched,
    found_by: frozenset[str],
    manifest: dict,
    check: str,
    previous: dict | None,
    now: str,
) -> dict:
    full_name = f.full_name
    return {
        "full_name": full_name,
        "url": f"https://github.com/{full_name}",
        "default_branch": f.default_branch,
        "commit": f.commit,
        "blob_sha": f.blob_sha,
        "manifest_url": f"https://raw.githubusercontent.com/{full_name}/{f.commit}/RECX.yaml",
        "detected_license": f.detected_license,
        "license_check": check,
        "declared_repository": declared_repository(manifest, full_name),
        "is_fork": f.is_fork,
        "parent": f.parent if f.is_fork else None,
        "is_archived": f.is_archived,
        "stars": max(0, f.stars),
        "repo_created_at": f.created_at,
        "pushed_at": f.pushed_at,
        "found_by": sorted(found_by),
        "first_indexed": previous["source"]["first_indexed"] if previous else now,
        "last_verified": now,
    }


def _validate(f: Fetched) -> tuple[dict | None, list[str]]:
    """Return (projection, []) for a valid manifest, or (None, reason codes)."""
    if f.text is None:
        # Fetched but no text: GitHub reported the blob as binary, or too large to return.
        if f.byte_size is not None and f.byte_size > MAX_BYTES:
            return None, [_RULE_CODES["5"]]
        return None, [UTF8]
    try:
        problems = check_text(f.text)
        if problems:
            return None, reason_codes(problems)
        data = parse(f.text)
    except RecursionError:
        # Deeply nested YAML exhausts the recursive parser. Reject it rather than end the run.
        return None, [YAML]
    return project(data), []


def _process(
    run: _Run,
    cand: Candidate,
    f: Fetched | None,
    prev: dict | None,
    *,
    now: str,
    blocklist: set[str],
    allowlist: frozenset[str],
) -> None:
    # Prefer GitHub's spelling: this run's fetch, else the previous entry, else the candidate.
    if f is not None and f.full_name.lower() == cand.full_name.lower():
        name = f.full_name
    elif prev is not None:
        name = prev["source"]["full_name"]
    else:
        name = cand.full_name
    key = name.lower()

    if is_blocked(name, blocklist):
        run.reject(name, [BLOCKED])
        if prev:
            run.notes.append(f"removed {name}: blocked")
        return

    if f is None:
        if prev:
            run.entries[key] = prev
            run.notes.append(f"kept {name} unchanged: not fetched this run")
        else:
            run.notes.append(f"skipped {name}: not fetched this run")
        return

    if f.full_name.lower() != cand.full_name.lower():
        # GitHub answered with another repository: the candidate was renamed away. The new
        # name is tracked if a discovery channel finds it.
        f = replace(f, exists=False)

    if not f.exists or f.blob_sha is None:
        why = "repository gone or private" if not f.exists else "no RECX.yaml at the root"
        if prev:
            run.notes.append(f"removed {name}: {why}")
        else:
            run.notes.append(f"skipped {name}: {why}")
        return

    if _incomplete(f):
        if prev:
            run.entries[key] = prev
            run.notes.append(f"kept {name} unchanged: incomplete metadata this run")
        else:
            run.notes.append(f"skipped {name}: incomplete metadata this run")
        return

    reuse = f.unchanged and prev is not None and prev["source"]["blob_sha"] == f.blob_sha
    if reuse:
        manifest = prev["manifest"]
    elif f.unchanged and f.text is None:
        # The client skipped the read, but the blob does not match the previous entry.
        if prev:
            run.entries[key] = prev
            run.notes.append(f"kept {name} unchanged: marked unchanged but blob differs")
        else:
            run.notes.append(f"skipped {name}: marked unchanged but not previously indexed")
        return
    else:
        manifest, reasons = _validate(f)
        if manifest is None:
            run.reject(name, reasons)
            if prev:
                run.notes.append(f"removed {name}: now invalid")
            return

    check = license_check(manifest.get("license"), f.detected_license, allowlist)
    if check is None:
        run.reject(name, [LICENSE_NOT_PERMISSIVE])
        if prev:
            run.notes.append(f"removed {name}: detected licence not permissive")
        return

    run.entries[key] = {
        "source": _source(f, cand.found_by, manifest, check, prev, now),
        "manifest": manifest,
    }


def _drop_fork_duplicates(run: _Run) -> None:
    """Drop forks whose blob equals their parent's indexed blob. Decided against the full set,
    before any drop, so the order of entries never matters."""
    blobs = {k: e["source"]["blob_sha"] for k, e in run.entries.items()}
    for key, entry in list(run.entries.items()):
        src = entry["source"]
        parent = src.get("parent")
        if src.get("is_fork") and parent and blobs.get(parent.lower()) == src["blob_sha"]:
            del run.entries[key]
            run.reject(src["full_name"], [FORK_DUPLICATE])


def build(
    previous: dict | None,
    candidates: list[Candidate],
    fetched: dict[str, Fetched],
    *,
    now: str,
    blocklist: set[str],
    generator: str,
) -> CrawlResult:
    """Build the new index and rejections from the previous index and this run's fetches."""
    blocklist = {b.lower() for b in blocklist}
    allowlist = license_allowlist()
    prev_entries = {e["source"]["full_name"].lower(): e for e in (previous or {}).get("entries", [])}
    by_name = {k.lower(): v for k, v in fetched.items()}

    merged = _merge_candidates(candidates)
    # Every previous entry is rechecked, even if the client did not pass it as a candidate.
    for key, entry in prev_entries.items():
        if key not in merged:
            merged[key] = Candidate(entry["source"]["full_name"], frozenset({RECHECK}))

    run = _Run(entries={}, rejected={}, notes=[])
    for key in sorted(merged):
        _process(
            run, merged[key], by_name.get(key), prev_entries.get(key),
            now=now, blocklist=blocklist, allowlist=allowlist,
        )
    _drop_fork_duplicates(run)

    entries = sorted(run.entries.values(), key=lambda e: e["source"]["full_name"])
    rejected = [
        {"full_name": name, "reasons": reasons}
        for name, reasons in sorted(run.rejected.values(), key=lambda r: r[0])
    ]
    index: dict[str, Any] = {
        "format": INDEX_FORMAT,
        "generated_at": now,
        "generator": generator,
        "spec_versions": sorted({e["manifest"]["recx"] for e in entries}),
        "counts": {"entries": len(entries), "rejected": len(rejected)},
        "entries": entries,
    }
    removed = sum(1 for k in prev_entries if k not in run.entries)
    added = sum(1 for k in run.entries if k not in prev_entries)
    notes = [
        f"candidates: {len(merged)}; fetched: {sum(1 for k in merged if k in by_name)}",
        f"indexed: {len(entries)} ({added} new, {removed} previous removed); rejected: {len(rejected)}",
        *run.notes,
    ]
    return CrawlResult(
        index=index,
        rejected={"format": REJECTED_FORMAT, "generated_at": now, "rejected": rejected},
        notes=notes,
    )
