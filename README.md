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

The npm package `@jbmv/recx` ships the schema: `require('@jbmv/recx')` returns it.

This README is an interim one. The full README is still to come.
