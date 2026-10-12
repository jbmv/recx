#!/bin/sh
# Stop and SubagentStop hook entry point. Reads the hook JSON on stdin and hands
# it to `recx hook stop`, which reads the event's name from that JSON, reports
# through JSON on stdout and always exits 0. This script always exits 0 too:
# Claude Code treats exit status 2 from a Stop or SubagentStop hook as a block,
# and uv exits 2 on some failures (offline, say), so passing a failing recx or
# uvx status through could trap a session. A missing, outdated or failing recx
# therefore never blocks; it only means no check ran.
[ "${RECX_STOP_HOOK:-}" = "off" ] && exit 0

# An installed recx is used only when its help names SubagentStop (0.1.1 and
# later); an older one would answer that event as Stop, so uvx runs instead.
if command -v recx >/dev/null 2>&1 && recx hook stop --help 2>/dev/null | grep -q SubagentStop; then
  recx hook stop
elif command -v uvx >/dev/null 2>&1; then
  uvx --quiet --from 'recx-cli>=0.1.1' recx hook stop 2>/dev/null
fi
exit 0
