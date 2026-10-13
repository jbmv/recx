import json
import subprocess
import sys

import jsonschema
import pytest

pytest.importorskip("mcp")

import anyio  # noqa: E402
from mcp import Client  # noqa: E402

from recx_cli import index_cache  # noqa: E402
from recx_cli.cli import main  # noqa: E402
from recx_cli.mcp_server import build_server  # noqa: E402
from recx_cli.search import NOTICE, search, show  # noqa: E402
from tests.test_match import INDEX  # noqa: E402
from tests.test_search import RESULT_SCHEMA, SRC  # noqa: E402

QUERY = "validate a yaml manifest against a json schema"


@pytest.fixture
def index_file(tmp_path, monkeypatch):
    monkeypatch.setattr(index_cache.time, "time", lambda: 1_790_000_000.0)
    monkeypatch.setenv("RECX_CACHE_DIR", str(tmp_path / "cache"))
    data = {"format": "recx-index/1.0", "generated_at": "2026-10-12T04:21:09Z", "generator": "t",
            "spec_versions": ["0.1"], "counts": {"entries": len(INDEX), "rejected": 0}, "entries": INDEX}
    path = tmp_path / "index.json"
    path.write_text(json.dumps(data))
    return path.as_uri()


def with_client(index_url, fn):
    async def go():
        async with Client(build_server(index_url)) as client:
            return await fn(client)

    return anyio.run(go)


def test_lists_exactly_two_tools_with_schemas_and_hints(index_file):
    tools = {t.name: t for t in with_client(index_file, lambda c: c.list_tools()).tools}
    assert set(tools) == {"recx_search", "recx_show"}
    s = tools["recx_search"].input_schema
    assert s["required"] == ["query"]
    assert s["properties"]["query"]["minLength"] == 1 and s["properties"]["query"]["maxLength"] == 2000
    assert s["properties"]["limit"] == {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}
    assert {"languages", "interfaces", "licenses"} <= set(s["properties"])
    assert "cli" in s["properties"]["interfaces"]["items"]["enum"]
    assert "MIT" in s["properties"]["licenses"]["items"]["enum"]
    h = tools["recx_show"].input_schema
    assert h["required"] == ["repository"]
    assert h["properties"]["repository"]["pattern"] == r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$"
    assert tools["recx_search"].output_schema == RESULT_SCHEMA
    assert json.loads((SRC / "search-result.schema.json").read_text()) == RESULT_SCHEMA
    for t in tools.values():
        a = t.annotations
        assert a.read_only_hint is True and a.idempotent_hint is True and a.open_world_hint is True
        assert "strangers" in t.description and "data" in t.description


def test_search_structured_content_equals_function(index_file):
    args = {"query": QUERY, "limit": 5, "languages": ["python"]}
    res = with_client(index_file, lambda c: c.call_tool("recx_search", args))
    assert not res.is_error
    expected = search(QUERY, limit=5, languages=["python"], index_url=index_file)
    assert res.structured_content == expected
    jsonschema.Draft202012Validator(RESULT_SCHEMA).validate(res.structured_content)
    text = res.content[0].text
    assert text.startswith(NOTICE + "\n\n")
    assert json.loads(text[len(NOTICE):]) == expected


def test_search_default_limit_is_10(index_file):
    res = with_client(index_file, lambda c: c.call_tool("recx_search", {"query": "yaml"}))
    assert res.structured_content == search("yaml", limit=10, index_url=index_file)


def test_show_found_and_missing(index_file):
    target = INDEX[2]["source"]["full_name"]
    res = with_client(index_file, lambda c: c.call_tool("recx_show", {"repository": target}))
    assert not res.is_error
    assert set(res.structured_content) >= {"notice", "untrusted", "index", "entry"}
    assert res.structured_content["untrusted"] == ["/entry/manifest"]
    assert res.structured_content["entry"] == INDEX[2]
    assert res.structured_content["index"] == show(target, index_url=index_file)["index"]
    assert res.content[0].text.startswith(NOTICE + "\n\n")
    missing = with_client(index_file, lambda c: c.call_tool("recx_show", {"repository": "nobody/nothing"}))
    assert missing.structured_content["entry"] is None
    assert missing.content[0].text.startswith(NOTICE + "\n\n")


@pytest.mark.parametrize(
    "tool,args",
    [
        ("recx_search", {}),
        ("recx_search", {"query": ""}),
        ("recx_search", {"query": "x" * 2001}),
        ("recx_search", {"query": "x", "limit": 0}),
        ("recx_search", {"query": "x", "limit": 51}),
        ("recx_search", {"query": "x", "interfaces": ["nonsense"]}),
        ("recx_show", {}),
        ("recx_show", {"repository": "no-slash"}),
        ("recx_show", {"repository": "a/b/c"}),
        ("nonexistent", {}),
    ],
)
def test_invalid_arguments_are_tool_errors(index_file, tool, args):
    res = with_client(index_file, lambda c: c.call_tool(tool, args))
    assert res.is_error


def test_unavailable_index_is_a_tool_error_and_server_survives(tmp_path):
    missing = (tmp_path / "absent.json").as_uri()

    async def fn(client):
        first = await client.call_tool("recx_search", {"query": "yaml"})
        second = await client.call_tool("recx_show", {"repository": "a/b"})
        tools = await client.list_tools()
        return first, second, tools

    first, second, tools = with_client(missing, fn)
    assert first.is_error and second.is_error
    assert "index unavailable" in first.content[0].text and len(first.content[0].text) > len("index unavailable: ")
    assert len(tools.tools) == 2


def test_cli_without_extra_exits_2(monkeypatch, capsys):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "mcp" else real(name, *a))
    assert main(["mcp"]) == 2
    assert "uvx --from 'recx-cli[mcp]' recx mcp" in capsys.readouterr().err


def test_check_and_search_do_not_import_mcp(tmp_path):
    code = (
        "import sys\n"
        "from recx_cli.cli import main\n"
        "import recx_cli.search\n"
        "main(['check', '--no-repo', sys.argv[1]])\n"
        "assert 'mcp' not in sys.modules and 'recx_cli.mcp_server' not in sys.modules\n"
    )
    manifest = tmp_path / "RECX.yaml"
    manifest.write_text("x: 1\n")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(manifest)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_stdio_server_lists_tools():
    script = (
        "import anyio\n"
        "from mcp import Client, StdioServerParameters\n"
        "async def go():\n"
        f"    params = StdioServerParameters(command={sys.executable!r}, args=['-m', 'recx_cli', 'mcp'])\n"
        "    async with Client(params) as c:\n"
        "        print(sorted(t.name for t in (await c.list_tools()).tools))\n"
        "anyio.run(go)\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "['recx_search', 'recx_show']" in out.stdout


def test_server_module_runs_no_commands():
    from tests.test_search import _forbidden

    assert _forbidden(SRC / "mcp_server.py") == []
