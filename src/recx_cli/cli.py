"""Command line: `recx check`, `recx evidence`, `recx search`, `recx show`, `recx mcp`, `recx hook stop`."""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata
from pathlib import Path

from recx_cli.check import Unreadable, check_file, format_problem
from recx_cli.evidence import HELP as EVIDENCE_HELP
from recx_cli.evidence import evidence
from recx_cli.index import _normalise_str


def _version() -> str:
    try:
        return metadata.version("recx-cli")
    except metadata.PackageNotFoundError:
        return "unknown"


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse exits 2 already; keep it explicit
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def _index_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--refresh", action="store_true", help="fetch the index even if the cached copy is fresh")
    p.add_argument("--offline", action="store_true", help="use the cached index and do not touch the network")
    p.add_argument("--index-url", metavar="URL", help="index location (default: RECX_INDEX_URL, else the published index)")


_FULL_NAME = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def _text(value, indent: str = "") -> str:
    """One line of author or index text, with control and format characters escaped."""
    line = " ".join(_normalise_str(str(value)).split())
    return indent + line


def _print_search(result: dict) -> None:
    print(result["notice"])
    print()
    results = result["results"]
    if not results:
        print("no matches")
        return
    for r in results:
        print(f"{r['rank']}. {_text(r['source'].get('full_name', ''))}  score {r['score']:.2f}")
        summary = r["manifest"].get("summary")
        if summary:
            print(_text(summary, "     "))
        for cap in r["matches"]["capabilities"][:3]:
            print(_text("matches: " + cap, "     "))
        if r["name_shared_with"]:
            print(f"     name shared with {r['name_shared_with']} other indexed repositories")
    print()
    print("To see one in full: recx show OWNER/NAME")


def _print_show(result: dict, full_name: str) -> None:
    print(result["notice"])
    print()
    entry = result["entry"]
    if entry is None:
        print(f"{_text(full_name)} is not in the index")
        return
    print(_text(entry["source"].get("full_name", "")))
    for line in _normalise_str(json.dumps(entry["manifest"], indent=2, ensure_ascii=False)).splitlines():
        print("     " + line)


def _run_index_command(args) -> int:
    from recx_cli.search import IndexUnavailable, search, show

    try:
        if args.command == "search":
            result = search(
                " ".join(args.query), limit=args.limit, languages=args.language,
                interfaces=args.interface, licenses=args.license,
                refresh=args.refresh, offline=args.offline, index_url=args.index_url,
            )
        else:
            result = show(args.full_name, refresh=args.refresh, offline=args.offline, index_url=args.index_url)
    except IndexUnavailable as exc:
        print(f"recx {args.command}: {_text(exc)}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.command == "search":
        _print_search(result)
    else:
        _print_show(result, args.full_name)
    return 0


def _run_mcp() -> int:
    from importlib.util import find_spec

    if find_spec("mcp") is None:
        print(
            "recx mcp: the MCP server needs the optional 'mcp' extra, which is not installed.\n"
            "Run it with: uvx --from 'recx-cli[mcp]' recx mcp",
            file=sys.stderr,
        )
        return 2
    from recx_cli.mcp_server import main as serve

    return serve()


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
    se = sub.add_parser(
        "search",
        help="search the published index of RECX.yaml manifests",
        description="Search the published index for repositories whose manifests match QUERY. "
        "Reads the index only; runs nothing from any manifest. "
        "Exit status: 0 success (including no matches), 1 no index available, 2 usage error.",
    )
    se.add_argument("query", nargs="+", metavar="QUERY", help="what you are looking for; words are joined with spaces")
    se.add_argument("--json", action="store_true", help="print the JSON object (see search-result.schema.json)")
    se.add_argument("--limit", type=int, default=20, metavar="N", help="at most N results, 1 to 100 (default 20)")
    se.add_argument("--language", action="append", default=[], metavar="TAG", help="keep entries using this language (repeatable)")
    se.add_argument("--interface", action="append", default=[], metavar="TYPE", help="keep entries with a module of this interface type (repeatable)")
    se.add_argument("--license", action="append", default=[], metavar="ID", help="keep entries with this licence (repeatable)")
    _index_options(se)
    sh = sub.add_parser(
        "show",
        help="print one full indexed entry",
        description="Print the full indexed entry for OWNER/NAME. Reads the index only. "
        "Exit status: 0 success (including not indexed), 1 no index available, 2 usage error.",
    )
    sh.add_argument("full_name", metavar="OWNER/NAME")
    sh.add_argument("--json", action="store_true", help="print the JSON object")
    _index_options(sh)
    sub.add_parser(
        "mcp",
        help="start the stdio MCP server (needs the recx-cli[mcp] extra)",
        description="Serve recx_search and recx_show over stdio for MCP clients. Reads the index only. "
        "Needs the optional extra: uvx --from 'recx-cli[mcp]' recx mcp. "
        "Exit status: 0 on a clean shutdown, 2 when the extra is not installed.",
    )
    hook = sub.add_parser(
        "hook",
        help="entry points for Claude Code hooks",
        description="Commands that Claude Code hooks run. They read the hook's JSON on stdin.",
    )
    hook_sub = hook.add_subparsers(dest="hook_command", required=True, parser_class=_Parser)
    hook_sub.add_parser(
        "stop",
        help="Stop and SubagentStop hook: keep the repository's RECX.yaml valid",
        description="Read Claude Code's Stop or SubagentStop hook JSON on stdin. Always exits 0. "
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
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "search" and not 1 <= args.limit <= 100:
        parser.error("search: --limit must be between 1 and 100")
    if args.command == "show" and not _FULL_NAME.fullmatch(args.full_name):
        parser.error("show: expected OWNER/NAME, for example jbmv/recx")
    if args.command in ("search", "show"):
        return _run_index_command(args)
    if args.command == "mcp":
        return _run_mcp()
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
