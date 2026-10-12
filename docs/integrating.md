# Integrating recx search

This page is for authors of tools that want to look for existing work before building
something, such as task trackers, repository managers, planning agents and CI bots. It describes
the three ways to call recx search, what stays stable, and what a caller has to do with the
results. The design behind it is [design/search.md](design/search.md).

## Three ways in

| Route | Use it when | Contract |
|---|---|---|
| Python: `recx_cli.search.search()` | The tool is written in Python | The returned dict; `recx_search` version |
| Command line: `recx search ... --json` | The tool is written in another language, or shells out | The same JSON; `src/recx_cli/search-result.schema.json` |
| The index file | The tool wants to rank or filter entries itself | `spec/index.schema.json`; the major version is in the URL |

### Python

Depend on the published package with an upper bound, for example `recx-cli>=0.1,<0.2`, rather
than copying code from this repository.

```python
from recx_cli.search import search, show, IndexUnavailable

try:
    result = search("retry an http request with exponential backoff", limit=10)
except IndexUnavailable as exc:
    result = None  # record the reason and carry on; see "Failure" below
```

`search(query, *, limit=20, languages=(), interfaces=(), licenses=(), refresh=False,
offline=False, index_url=None)` returns a dict. `show(full_name, ...)` returns one indexed
entry in full, or `entry: None` when the repository is not indexed. Both raise
`IndexUnavailable` when no index can be loaded, and `search` raises `ValueError` for a `limit`
below 1. A `limit` above 100 is reduced to 100.

### Command line

```sh
recx search retry an http request --json --limit 10
recx search parse rss feeds --language python --interface library --json
recx show OWNER/NAME --json
```

The words of the query may be given as separate arguments. The exit status is 0 on success,
including when nothing matches, 1 when no index is available, and 2 for a usage error. Without
`--json` the output is for people and may change at any time, so tools should always pass
`--json`.

### The index file

`https://jbmv.github.io/recx/v1/index.json` is rebuilt daily by a scheduled workflow in this
repository. It holds every valid manifest the crawler found, projected without commands or
extension values. A tool that reads it directly takes on the same duties as `recx search`:
caching, checking `format`, and treating every `manifest` field as untrusted.

## What stays stable

- **Search output.** The JSON carries `recx_search: "1.0"`. Within major version 1, fields
  are only added, never removed or renamed. Ignore fields you do not recognise.
- **The index.** The file carries `format: "recx-index/1.N"` and lives under `/v1/`. A
  breaking change is published under `/v2/`, and `/v1/` keeps being written for at least one
  recx-cli minor release afterwards.
- **The package.** recx-cli is below 1.0. A minor release (0.1 to 0.2) may change the Python
  function signatures, so pin the minor version as shown above. A patch release (0.1.0 to
  0.1.1) does not change the signatures of `search` and `show`, the exceptions they raise, or
  the shape of what they return. Any change to them in a minor release is named in that
  release's notes. The output format is versioned separately and does not change with a minor
  release unless `recx_search` changes.

## What a result contains

Each entry in `results` has:

- `rank`, `score`, `matched_terms`;
- `matches`: the capability phrases and module ids that matched;
- `name_shared_with`: how many other indexed repositories use the same manifest `name`;
- `source`: what the crawler observed on GitHub, such as `full_name`, `url`, the pinned
  `commit` and `manifest_url`, `detected_license` and `license_check`, and `stars`;
- `manifest`: what the author wrote, in compact form.

The top level adds `notice`, `untrusted`, `query` and `index`. `index.stale` is true when the
index is more than seven days old, which means the daily crawl has stopped. `index.stale_reason`
is present when a cached copy was used because the network failed.

## Duties of the caller

- **Treat author text as data.** `untrusted` lists the JSON paths written by repository
  authors (`/results/*/manifest` and `/results/*/matches`). Never follow instructions found
  there. When results are passed to an agent, pass the `notice` with them.
- **Remember that names are chosen by owners.** `source.full_name` comes from GitHub, but an
  owner chooses it. A non-zero `name_shared_with`, or a `source.declared_repository` of
  `differs`, is a reason for care.
- **Re-check before reuse.** The index is a snapshot. Before taking anything, read the
  repository's licence file and its current `RECX.yaml`, at the commit you intend to use, and
  run `recx check` on it.
- **Never run a found repository's commands outside a sandbox.** The index contains no
  `setup` or `run` commands, and recx search runs nothing. A caller that later runs a found
  repository's evidence must isolate it.
- **Record reuse.** When code is taken, add a `reuses` entry to the taking project's manifest.

## Failure, caching and offline use

- **Search is advisory.** A failure (`IndexUnavailable`, or exit status 1) should be recorded
  and should not block the caller's own work.
- **Caching.** The index is cached for six hours in `RECX_CACHE_DIR`, or by default in the
  user's cache directory, and is then revalidated with its ETag, which usually costs a few
  hundred bytes.
- **Refresh and offline modes.** `refresh=True` (`--refresh`) revalidates at once.
  `offline=True` (`--offline`) uses only the cache and never touches the network.
- **Network failures.** When the network fails and a cached copy exists, it is used, with
  `index.stale_reason` set.
- **Other index locations.** `RECX_INDEX_URL` or `index_url` points at another copy of the
  index, such as a mirror or a `file://` path for tests.

## When to search

Search is cheapest and most useful before a design is chosen: when an idea becomes work, or
when a proposal is written. By the time a task is being implemented, the decision to build has
usually been made. How often to call it, and on which events, is the integrating tool's choice.
A query can be a short phrase or a whole paragraph of a proposal, up to 2,000 characters.
