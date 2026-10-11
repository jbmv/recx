# Adopting recx

recx asks one thing of a repository: a `RECX.yaml` at its root that describes the repository to
other people's coding agents. The spec is [spec/SPEC.md](spec/SPEC.md).

## With Claude Code

Install the plugin once:

```
/plugin marketplace add jbmv/recx
/plugin install recx@recx --scope project
```

Then ask Claude to add a RECX.yaml. The plugin's `recx:manifest` skill writes the manifest, and
its Stop hook keeps it valid as the repository changes. Set `RECX_STOP_HOOK=warn` to get warnings
instead of blocks, or `RECX_STOP_HOOK=off` to turn the hook off.

## With any other coding agent

Paste this prompt:

```text
Add a RECX.yaml manifest to this repository. Follow the guide at
https://raw.githubusercontent.com/jbmv/recx/main/plugin/skills/manifest/SKILL.md
and the spec it links to. Declare only what exists now, run every evidence command
before listing it, and finish when `uvx --from recx-cli recx check` prints "valid".
```

The guide is the same text the plugin's skill uses, so both routes give the same result.

## By hand

Start from [spec/examples/valid/minimal.yaml](spec/examples/valid/minimal.yaml), add to it from
the spec, and run `recx check` (`pip install recx-cli`, or `uvx --from recx-cli recx check`).
