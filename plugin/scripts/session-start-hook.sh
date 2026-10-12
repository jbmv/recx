#!/bin/sh
# SessionStart hook entry point. Reads the hook JSON on stdin and hands it to
# `recx hook session-start`, which records the repository's HEAD for the session
# so the Stop hook can remind about manifest drift. It prints nothing. This
# script always exits 0: a missing, outdated or failing recx never affects a
# session; it only means no HEAD was recorded.
[ "${RECX_STOP_HOOK:-}" = "off" ] && exit 0
[ "${RECX_DRIFT_REMINDER:-}" = "off" ] && exit 0

if command -v recx >/dev/null 2>&1 && recx hook session-start --help >/dev/null 2>&1; then
  recx hook session-start
elif command -v uvx >/dev/null 2>&1; then
  uvx --quiet --from 'recx-cli>=0.1.1' recx hook session-start 2>/dev/null
fi
exit 0
