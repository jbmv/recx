# recx

*res extra commercium*: things outside commerce.

A `RECX.yaml` at the root of a repository tells other people's coding agents what the repository
does, which parts of it they can take on their own, and which checks show those parts work. recx
is that format, a validator for it, and a Claude Code plugin that keeps the file current.

## Why

More software is being written than anyone can read. A useful MIT-licensed project gets a
handful of views, and the next person who needs it builds it again.

People can't keep up with that volume, but agents can. recx writes for them. Before an agent
builds something, it can search for manifests, match its plan against what they offer, and
reuse a module that already exists and passes its checks, recording the reuse in its own
manifest.

There is no service to run. The fixed filename is the index: on GitHub, search code for
`path:/(^|\/)recx\.ya?ml$/i`.

## A manifest

```yaml
recx: "0.1"
name: tinyfmt
summary: Formats byte counts as human-readable strings like 1.2 MB.
license: MIT
status: stable
capabilities:
  - format a byte count as a human-readable size string
```

Those six keys are the minimum. Most of the value is in the optional parts: modules with
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
license file matches `license`.

## Kyosei

I built recx for Kyosei, a record-keeping tool for running projects with coding agents. Kyosei
isn't public yet. It will generate its own RECX.yaml from its records. Nothing in the spec
depends on Kyosei: it is one client of the spec, like any other tool.

## License

MIT
