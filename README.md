# recx

*res extra commercium*: things outside commerce. A repository drops a `RECX.yaml` at its
root to tell other people's coding agents what it does, which of its modules can be taken on
their own, how to take them, and which checks anyone can rerun to see that they work.

The format is specified in [spec/SPEC.md](spec/SPEC.md); the schema is
[spec/recx.schema.json](spec/recx.schema.json).

## Checking a manifest

```sh
pip install recx-cli          # or: uvx --from recx-cli recx check

recx check                    # ./RECX.yaml, including that module paths exist
recx check path/to/repo       # a directory holding RECX.yaml
recx check --no-repo FILE     # the file alone, without looking at the repository
```

One line per problem, `<file>: <json-pointer>: <message>`, or `<file>: valid`. Exit status 0
means valid, 1 invalid, 2 a usage error or an unreadable file. `recx check` does not yet
check that the license file agrees with `license` (rule 4 of the spec).

## The Claude Code plugin

The `recx` plugin adds the `/recx:manifest` skill, for writing and maintaining a `RECX.yaml`,
and a Stop hook that keeps the manifest valid as a session ends.

```
/plugin marketplace add jbmv/recx
/plugin install recx@recx
```

On Claude Code 2.1.275 or later, `/plugin install recx --marketplace jbmv/recx` does both in one
step. The hook runs `recx hook stop`, from `recx` on your PATH or else through `uvx`; it needs
recx-cli 0.0.2 or later, and a missing `recx` never blocks a session. `RECX_STOP_HOOK` sets it:
`block` (the default), `warn` for feedback only, or `off`.

To adopt recx in a repository with any coding agent, see [ADOPT.md](ADOPT.md).

The npm package `@jbmv/recx` ships the schema: `require('@jbmv/recx')` returns it.

This README is an interim one. The full README is still to come.
