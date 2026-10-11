"""Command line: `recx check`, `recx evidence`, `recx hook stop`."""

from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

from recx_cli.check import Unreadable, check_file, format_problem
from recx_cli.evidence import HELP as EVIDENCE_HELP
from recx_cli.evidence import evidence


def _version() -> str:
    try:
        return metadata.version("recx-cli")
    except metadata.PackageNotFoundError:
        return "unknown"


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse exits 2 already; keep it explicit
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="recx", description="Tools for RECX.yaml manifests.")
    parser.add_argument("--version", action="version", version=f"recx {_version()}")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    check = sub.add_parser(
        "check",
        help="validate a RECX.yaml manifest",
        description="Validate a manifest against the schema and the rules in spec/SPEC.md. "
        "Exit status: 0 valid, 1 invalid, 2 usage error or unreadable file.",
    )
    check.add_argument(
        "path",
        nargs="?",
        default="RECX.yaml",
        metavar="PATH",
        help="a manifest file, or a directory holding RECX.yaml (default: ./RECX.yaml)",
    )
    check.add_argument(
        "--no-repo",
        action="store_true",
        help="skip the checks that look at the repository (rule 3: module paths exist)",
    )
    ev = sub.add_parser(
        "evidence",
        help="run a manifest's evidence commands (your own repository or a sandbox only)",
        description=EVIDENCE_HELP,
    )
    ev.add_argument(
        "path",
        nargs="?",
        default="RECX.yaml",
        metavar="PATH",
        help="a manifest file, or a directory holding RECX.yaml (default: ./RECX.yaml)",
    )
    ev.add_argument(
        "--list", action="store_true", help="print each selected entry's commands without running anything"
    )
    ev.add_argument("--only", nargs="+", metavar="ID", help="run only the named evidence entries")
    hook = sub.add_parser(
        "hook",
        help="entry points for Claude Code hooks",
        description="Commands that Claude Code hooks run. They read the hook's JSON on stdin.",
    )
    hook_sub = hook.add_subparsers(dest="hook_command", required=True, parser_class=_Parser)
    hook_sub.add_parser(
        "stop",
        help="Stop hook: keep the repository's RECX.yaml valid as a session ends",
        description="Read Claude Code's Stop hook JSON on stdin. Always exits 0. "
        "RECX_STOP_HOOK=block (default) blocks the stop while RECX.yaml is invalid, "
        "warn only gives feedback, off does nothing.",
    )
    hook_sub.add_parser(
        "session-start",
        help="SessionStart hook: record HEAD so the Stop hook can remind about manifest drift",
        description="Read Claude Code's SessionStart hook JSON on stdin and record the "
        "repository's HEAD for the session. Always exits 0 with no output. "
        "RECX_DRIFT_REMINDER=off or RECX_STOP_HOOK=off does nothing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "hook":
        from recx_cli.hook import session_start, stop

        return session_start() if args.hook_command == "session-start" else stop()
    if args.command == "evidence":
        return evidence(args.path, args.list, args.only)
    target = Path(args.path)
    if target.is_dir():
        target = target / "RECX.yaml"
    label = str(target)
    try:
        problems = check_file(target, repo=not args.no_repo)
    except Unreadable as exc:
        print(f"{label}: cannot read: {exc}", file=sys.stderr)
        return 2
    if not problems:
        print(f"{label}: valid")
        return 0
    for p in problems:
        print(format_problem(label, p))
    return 1
