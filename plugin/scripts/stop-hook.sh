#!/bin/sh
# Stop hook entry point. Reads the hook JSON on stdin and hands it to
# `recx hook stop`, which reports through JSON on stdout and always exits 0.
# This script always exits 0 too: Claude Code treats exit status 2 from a Stop
# hook as a block, and uv exits 2 on some failures (offline, say), so passing a
# failing recx or uvx status through could trap a session. A missing, outdated
# or failing recx therefore never blocks; it only means no check ran.
[ "${RECX_STOP_HOOK:-}" = "off" ] && exit 0

if command -v recx >/dev/null 2>&1 && recx hook stop --help >/dev/null 2>&1; then
  recx hook stop
elif command -v uvx >/dev/null 2>&1; then
  uvx --quiet --from 'recx-cli>=0.0.4' recx hook stop 2>/dev/null
fi
exit 0
