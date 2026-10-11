"""Projection of a manifest into the search index (docs/design/search.md, section 1)."""

from __future__ import annotations

import unicodedata
from typing import Any

TOP_KEPT = (
    "recx", "name", "summary", "description", "license", "status", "capabilities",
    "languages", "tags", "repository", "homepage", "generator",
)
MODULE_KEPT = ("id", "path", "summary", "capabilities", "language", "interface", "requires", "extract")
EVIDENCE_KEPT = ("id", "summary", "covers", "last_result")


def _escape(ch: str) -> str:
    n = ord(ch)
    if n < 0x100:
        return "\\x%02x" % n
    if n < 0x10000:
        return "\\u%04x" % n
    return "\\U%08x" % n


_KEEP = frozenset("\n\t")  # multi-line fields (description, extract.notes) need them; JSON encodes them


def _normalise_str(text: str) -> str:
    return "".join(
        _escape(c) if c not in _KEEP and unicodedata.category(c) in ("Cf", "Cc") else c
        for c in text
    )


def _clean(value: Any) -> Any:
    """Deep copy with every string normalised and every x- key dropped."""
    if isinstance(value, str):
        return _normalise_str(value)
    if isinstance(value, dict):
        return {
            (_normalise_str(k) if isinstance(k, str) else k): _clean(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.startswith("x-"))
        }
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _pick(item: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(item, dict):
        return item
    return {k: item[k] for k in keys if k in item}


def project(manifest: dict) -> dict:
    """Return the `manifest` part of an index entry. Pure; the input is not changed."""
    out = _pick(manifest, TOP_KEPT)
    if "modules" in manifest:
        out["modules"] = [_pick(m, MODULE_KEPT) for m in manifest["modules"]]
    if "evidence" in manifest:
        out["evidence"] = [_pick(e, EVIDENCE_KEPT) for e in manifest["evidence"]]
    if "reuses" in manifest:
        out["reuses"] = list(manifest["reuses"])
    return _clean(out)
