"""Keyword matching of a query against index entries: BM25F, v0 (docs/design/search.md, section 3).

Standard library only, deterministic and pure: no I/O, no clock, no randomness. `rank` takes
the index entries (`{"source", "manifest"}`) and a query, and returns `Match` records that
say what matched. Rendering, the notice and the JSON contract belong to the search command.

Section 3 fixes the tokeniser, the stemmer, the field weights and the formula. Where it left
something open, this module takes the plain reading:

- **A field is a row of the weight table.** Each entry is one document with five fields:
  `capabilities` (project and every module's capabilities), `summary` (project and every
  module's summary), `labels` (`tags`, `languages`, every module's `language`, and `name`),
  `description`, and `entrypoints` (every module's `interface.entrypoints`). A field's text is
  all of its values together, so a field that spans several modules is one bag of terms.
- **Lengths.** `len_f` is the number of terms in the field after stopwords are dropped,
  counting repeats. `avglen_f` is the mean of `len_f` over every entry passed to `rank`,
  counting entries where the field is empty as length 0.
- **Corpus statistics ignore filters.** `N`, `df` and `avglen_f` are computed over every entry
  passed in, before the language, interface and licence filters, so a filter removes results
  without changing the scores of the rest. `df(t)` counts entries with `t` in any scored field.
- **Stemming threshold.** "At least four characters remain" counts the characters left once
  the suffix is removed and before anything is added, so `queries` becomes `query` (`quer`
  remains) but `series` keeps its `ies` (only `ser` would remain). The second rule tries `ing`,
  then `ed`, then a final `s`; a word can end in only one of them.
- **Formula as written.** `score = Σ idf(t) · tf' / (k1 + tf')`, without the `(k1 + 1)`
  factor some BM25 texts put in the numerator. The factor is a constant and changes no order.
- **Tokens.** Splitting is as section 3 says, and a token made only of `+` and `#` (such as
  `##` from a Markdown heading) is dropped, since it names nothing.
- **Query cap.** The query is cut to its first 2,000 characters before tokenising.
- **Limit.** `limit` is capped at 100; a limit below 1 is a `ValueError`.
- **Filters.** Values are compared case-insensitively. Several values of one filter keep an
  entry matching any of them; different filters must all hold.
- **Explanation.** `matched_terms` follows the query's term order. `capabilities` lists each
  distinct capability phrase (project first, then modules in order) that contains a query
  term, ordered by the number of distinct query terms it contains, most first, then by that
  original order. `modules` lists, in manifest order, the ids of modules whose capabilities,
  summary, language or entrypoints contain a query term.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

K1 = 1.2
B = 0.75
MAX_QUERY_CHARS = 2000
MAX_LIMIT = 100

WEIGHTS = {
    "capabilities": 3.0,
    "summary": 2.0,
    "labels": 1.5,
    "description": 1.0,
    "entrypoints": 0.5,
}
FIELDS = tuple(WEIGHTS)

# Function words only: anything that names a thing or an action is left to idf.
STOPWORDS = frozenset("""
    a about all an and any are as at be been but by can could do does for from had has have
    how i if in into is it its may more must my no not of on or our should so some such than
    that the their them then there these they this those to too very was we were what when
    where which while who will with would you your
""".split())

_SPLIT = re.compile(r"[^a-z0-9+#]+")


def _stem(token: str) -> str:
    if token.endswith(("ies", "ied")) and len(token) - 3 >= 4:
        token = token[:-3] + "y"
    if token.endswith("ing"):
        if len(token) - 3 >= 4:
            token = token[:-3]
    elif token.endswith("ed"):
        if len(token) - 2 >= 4:
            token = token[:-2]
    elif token.endswith("s") and not token.endswith("ss") and len(token) - 1 >= 4:
        token = token[:-1]
    if token.endswith("e") and len(token) - 1 >= 4:
        token = token[:-1]
    return token


def _tokens(text: str) -> list[str]:
    """Every term in `text`, in order, with repeats."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    out = []
    for token in _SPLIT.split(folded):
        if (
            not token.strip("+#")
            or (len(token) == 1 and token not in ("c", "r"))
            or token in STOPWORDS
        ):
            continue
        out.append(_stem(token))
    return out


def terms(text: str) -> list[str]:
    """The distinct terms of `text`, in first-seen order."""
    return list(dict.fromkeys(_tokens(text)))


@dataclass
class Match:
    entry: dict
    score: float
    matched_terms: list[str]
    capabilities: list[str]
    modules: list[str]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _modules(manifest: dict) -> list[dict]:
    mods = manifest.get("modules")
    return [m for m in mods if isinstance(m, dict)] if isinstance(mods, list) else []


@dataclass
class _Doc:
    entry: dict
    counts: dict[str, Counter]
    lengths: dict[str, int]
    phrases: list[tuple[str, frozenset[str]]]  # capability phrase, its terms
    modules: list[tuple[str, frozenset[str]]]  # module id, terms of its scored fields

    @property
    def vocabulary(self) -> set[str]:
        return {t for c in self.counts.values() for t in c}


def _doc(entry: dict) -> _Doc:
    manifest = entry.get("manifest") or {}
    texts: dict[str, list[str]] = {f: [] for f in FIELDS}
    phrases = _strings(manifest.get("capabilities"))
    texts["capabilities"] += phrases
    texts["summary"] += _strings(manifest.get("summary"))
    texts["labels"] += _strings(manifest.get("tags")) + _strings(manifest.get("languages"))
    texts["labels"] += _strings(manifest.get("name"))
    texts["description"] += _strings(manifest.get("description"))
    modules = []
    for mod in _modules(manifest):
        caps = _strings(mod.get("capabilities"))
        summary = _strings(mod.get("summary"))
        language = _strings(mod.get("language"))
        interface = mod.get("interface")
        entrypoints = _strings(interface.get("entrypoints")) if isinstance(interface, dict) else []
        phrases += caps
        texts["capabilities"] += caps
        texts["summary"] += summary
        texts["labels"] += language
        texts["entrypoints"] += entrypoints
        mod_terms = frozenset(t for s in caps + summary + language + entrypoints for t in _tokens(s))
        modules.append((mod.get("id") if isinstance(mod.get("id"), str) else "", mod_terms))
    counts = {}
    lengths = {}
    for f in FIELDS:
        toks = [t for s in texts[f] for t in _tokens(s)]
        counts[f] = Counter(toks)
        lengths[f] = len(toks)
    distinct = list(dict.fromkeys(phrases))
    return _Doc(entry, counts, lengths, [(p, frozenset(_tokens(p))) for p in distinct], modules)


def _folded(values: Iterable[str]) -> set[str]:
    return {v.casefold() for v in values}


def _passes(entry: dict, languages: set[str], interfaces: set[str], licenses: set[str]) -> bool:
    manifest = entry.get("manifest") or {}
    modules = _modules(manifest)
    if languages:
        have = _folded(_strings(manifest.get("languages")))
        have |= _folded(s for m in modules for s in _strings(m.get("language")))
        if not have & languages:
            return False
    if interfaces:
        have = _folded(
            s for m in modules if isinstance(m.get("interface"), dict)
            for s in _strings(m["interface"].get("type"))
        )
        if not have & interfaces:
            return False
    if licenses and not _folded(_strings(manifest.get("license"))) & licenses:
        return False
    return True


def _full_name(entry: dict) -> str:
    source = entry.get("source") or {}
    name = source.get("full_name")
    return name if isinstance(name, str) else ""


def rank(
    entries: list[dict],
    query: str,
    *,
    limit: int = 20,
    languages: Iterable[str] = (),
    interfaces: Iterable[str] = (),
    licenses: Iterable[str] = (),
) -> list[Match]:
    """Score every entry against `query` and return the best `limit`, best first.

    Any entry containing at least one query term is a candidate; there is no score threshold.
    Ties are broken by `source.full_name`, ascending.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    limit = min(limit, MAX_LIMIT)
    query_terms = terms(query[:MAX_QUERY_CHARS])
    if not query_terms or not entries:
        return []
    docs = [_doc(e) for e in entries]
    n = len(docs)
    avglen = {f: sum(d.lengths[f] for d in docs) / n for f in FIELDS}
    vocabularies = [d.vocabulary for d in docs]
    idf = {}
    for t in query_terms:
        df = sum(1 for v in vocabularies if t in v)
        idf[t] = math.log(1 + (n - df + 0.5) / (df + 0.5))
    wanted = (_folded(languages), _folded(interfaces), _folded(licenses))
    qset = frozenset(query_terms)

    results = []
    for doc in docs:
        score = 0.0
        matched = []
        for t in query_terms:
            tf_prime = 0.0
            for f in FIELDS:
                tf = doc.counts[f].get(t, 0)
                if tf:
                    tf_prime += WEIGHTS[f] * tf / (1 - B + B * doc.lengths[f] / avglen[f])
            if tf_prime:
                matched.append(t)
                score += idf[t] * tf_prime / (K1 + tf_prime)
        if not matched or not _passes(doc.entry, *wanted):
            continue
        overlaps = [(len(ts & qset), i, p) for i, (p, ts) in enumerate(doc.phrases)]
        capabilities = [p for k, _, p in sorted((-k, i, p) for k, i, p in overlaps if k)]
        modules = [mid for mid, ts in doc.modules if ts & qset]
        results.append(Match(doc.entry, score, matched, capabilities, modules))

    results.sort(key=lambda m: (-m.score, _full_name(m.entry)))
    return results[:limit]
