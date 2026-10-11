"""The data passed from the GitHub client (crawler/github.py) to the core (crawler/core.py).

The client discovers and fetches; the core decides. Keeping this boundary in plain dataclasses
lets the core be tested offline with no network and no recorded HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Discovery channels, as they appear in an index entry's `source.found_by`.
CODE_SEARCH = "code-search"
TOPIC = "topic"
SEED = "seed"
RECHECK = "recheck"


@dataclass(frozen=True)
class Candidate:
    """A repository that may hold a manifest, found by one or more discovery channels."""

    full_name: str  # owner/name, as GitHub reports it
    found_by: frozenset[str]  # subset of CODE_SEARCH, TOPIC, SEED, RECHECK
    node_id: str | None = None  # GraphQL node id, when the channel supplied one
    blob_sha: str | None = None  # RECX.yaml blob sha, from code search only
    commit: str | None = None  # commit from code search's `?ref=`, from code search only


@dataclass(frozen=True)
class Fetched:
    """What the client read from GitHub about one candidate.

    `exists` is False when the repository is gone, private or renamed away. `blob_sha` is None
    when the default branch has no RECX.yaml at the root. `text` is None when the file is
    absent, binary, or deliberately not re-read because its blob sha matched the previous
    index (`unchanged` is then True and the core reuses the previous projection).
    """

    full_name: str
    exists: bool
    default_branch: str | None = None
    commit: str | None = None  # the default branch's head commit oid
    blob_sha: str | None = None
    byte_size: int | None = None
    text: str | None = None
    unchanged: bool = False
    is_fork: bool = False
    parent: str | None = None  # owner/name of the parent, for forks
    is_archived: bool = False
    stars: int = 0
    created_at: str | None = None  # RFC 3339
    pushed_at: str | None = None  # RFC 3339
    detected_license: str | None = None  # GitHub's licenseInfo.spdxId, verbatim (may be NOASSERTION or OTHER)


@dataclass
class CrawlResult:
    """The core's output: the new index file and the rejections file, as JSON-ready dicts."""

    index: dict
    rejected: dict
    notes: list[str] = field(default_factory=list)  # human-readable run notes for the job log
