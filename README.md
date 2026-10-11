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

## What's here

| | |
|---|---|
| [spec/](spec/) | The spec, its JSON Schema, and valid and invalid example manifests |
| `recx-cli` on PyPI | `recx check`, the validator (`pip install recx-cli`) |
| `@jbmv/recx` on npm | The schema and examples, for JavaScript tooling |
| [plugin/](plugin/) | The Claude Code plugin; this repository is its marketplace |
| [RECX.yaml](RECX.yaml) | recx's own manifest |

## Status

Experimental. The spec is a v0.1 draft and may change before 1.0; every manifest states the
version it was written against. `recx check` enforces the whole spec except rule 4, that the
license file matches `license`. The manifest, the validator and the plugin exist; search, the
second part described above, is not built yet.

## Kyosei

I built recx for Kyosei, a record-keeping tool for running projects with coding agents. Kyosei
isn't public yet. It will generate its own RECX.yaml from its records. Nothing in the spec
depends on Kyosei: it is one client of the spec, like any other tool.

## License

MIT
