#!/bin/sh
# Stop hook entry point. Reads the hook JSON on stdin and hands it to
# `recx hook stop`. A missing or outdated recx never blocks a session.
[ "${RECX_STOP_HOOK:-}" = "off" ] && exit 0

if command -v recx >/dev/null 2>&1 && recx hook stop --help >/dev/null 2>&1; then
  exec recx hook stop
fi
if command -v uvx >/dev/null 2>&1; then
  exec uvx --from 'recx-cli>=0.0.2' recx hook stop
fi
exit 0
