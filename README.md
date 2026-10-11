# recx

*res extra commercium*: things outside commerce. A way for agents to find reusable, permissively
licensed code from solo and small-team projects.

A `RECX.yaml` at the root of a repository tells other people's coding agents what the repository
does, which parts of it they can take on their own, and which checks show those parts work. recx
is that format, a validator for it, and a Claude Code plugin that keeps the file current.

## Why

Coding agents have increased the amount of software that individuals and small teams produce.
Much of it remains internal tooling, and the projects that are published are hard to find among
the number of new repositories. Evaluating an unfamiliar open-source project also takes time, so
writing a similar tool is often the quicker option, and comparable tools get written more than
once.

recx moves that search and evaluation to the agent doing the work, in two parts. The first is
the manifest: a `RECX.yaml` in each participating repository that describes the project's
capabilities, its separable modules and their interfaces, and the commands that verify them, in
a fixed machine-readable form. The second is search: a way for an agent, while planning new
work, to find manifests with relevant modules, check their evidence, and reuse a module rather
than write it again, recording where it came from in its own project's manifest.

Taking part is meant to cost a repository little. One prompt produces a complete, valid
manifest, and the Claude Code plugin checks it whenever a session ends, so it stays valid as
the code changes. No hosted service is involved. The fixed filename and the GitHub topic `recx`
serve as the index, and a scheduled workflow in this repository collects the valid manifests it
finds into a static file each day. In GitHub's web interface, manifests can also be found by
searching code for `path:/(^|\/)recx\.ya?ml$/i`; GitHub's code search API does not accept that
regular expression.

## The manifest

```yaml
recx: "0.1"
name: tinyfmt
summary: Formats byte counts as human-readable strings like 1.2 MB.
license: MIT
status: stable
capabilities:
  - format a byte count as a human-readable size string
```

Those six keys are the minimum. The optional parts carry most of the information: modules with
interfaces and extraction notes, evidence anyone can rerun, and records of what was reused from
where. [spec/SPEC.md](spec/SPEC.md) defines every field, puts a length limit on every string,
and sets out how to read a stranger's manifest safely.

Only permissively licensed projects qualify (MIT, Apache-2.0, the BSDs and similar), so
anything listed can be reused without asking.

## Get started

- **Claude Code:** `/plugin marketplace add jbmv/recx`, then `/plugin install recx@recx`
  (or, on Claude Code 2.1.275 or later, `/plugin install recx --marketplace jbmv/recx`). Then
  ask Claude to add a RECX.yaml. A Stop hook keeps the manifest valid; set
  `RECX_STOP_HOOK=warn` or `off` to soften or disable it.
- **Any other agent:** paste the prompt in [ADOPT.md](ADOPT.md).
- **By hand:** start from the example above, then run `uvx --from recx-cli recx check`.

Then add the GitHub topic `recx` to the repository, so the daily index finds it promptly.

## Searching

```sh
recx search "retry an http request with exponential backoff"
recx search parse rss feeds --language python --json
recx show OWNER/NAME
```

`recx search` downloads the published index (https://jbmv.github.io/recx/v1/index.json),
caches it for six hours, and ranks entries by keyword overlap with the query, weighting
capabilities above summaries and descriptions. It returns every entry that shares a term with
the query, ordered by score, and leaves the judgement of relevance to the agent reading the
results. `--json` gives the structured form a tool or agent should use, and the same result is
available from Python as `recx_cli.search.search()`. `recx show` prints one indexed entry in
full. Neither command needs a GitHub token or runs anything from a repository.

Every result is written by the repository's author and has been checked only against the
schema. The output says so in a fixed `notice`, and the JSON lists the author-written paths in
`untrusted`. The index never contains a manifest's `setup` or `run` commands. Before reusing
anything, read the repository's licence file and its current `RECX.yaml`, and run its evidence
only in a sandbox. [docs/integrating.md](docs/integrating.md) explains how to call search from
another tool, and [docs/design/search.md](docs/design/search.md) describes the index, the crawler
and the ranking in full.

## What's here

| | |
|---|---|
| [spec/](spec/) | The spec, its JSON Schema, and valid and invalid example manifests |
| `recx-cli` on PyPI | `recx check`, `recx evidence`, `recx search` and `recx show` (`pip install recx-cli`) |
| `@jbmv/recx` on npm | The schema and examples, for JavaScript tooling |
| [plugin/](plugin/) | The Claude Code plugin; this repository is its marketplace |
| [crawler/](crawler/) | The daily crawler that builds the search index, run by a scheduled workflow |
| [docs/design/](docs/design/) | Design documents |
| [RECX.yaml](RECX.yaml) | recx's own manifest |

## Status

Experimental. The spec is a v0.1 draft and may change before 1.0; every manifest states the
version it was written against. `recx check` enforces the whole spec except rule 4, that the
license file matches `license`; the search crawler applies it as far as GitHub's licence
detection allows. Search is in its first version: it ranks by keywords, has no synonym handling,
and has no MCP server yet.

## Kyosei

I built recx for Kyosei, a record-keeping tool for running projects with coding agents. Kyosei
isn't public yet. It will generate its own RECX.yaml from its records. Nothing in the spec
depends on Kyosei: it is one client of the spec, like any other tool.

## License

MIT
