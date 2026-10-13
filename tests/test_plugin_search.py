import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin-search"


def test_plugin_is_named_recx_search_and_versioned_with_recx_cli():
    # Claude Code updates an installed plugin only when plugin.json's version string changes,
    # so both plugins move in lockstep with recx-cli, as tests/test_hook.py checks for plugin/.
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    package = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert plugin["name"] == "recx-search"
    assert plugin["version"] == package


def test_mcp_json_declares_the_recx_server():
    servers = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]
    assert list(servers) == ["recx"]
    server = servers["recx"]
    assert server.get("type", "stdio") == "stdio"
    assert server["command"] == "uvx"
    assert server["args"] == ["--quiet", "--from", "recx-cli[mcp]>=0.1.2", "recx", "mcp"]
    assert server["env"] == {"RECX_CACHE_DIR": "${CLAUDE_PLUGIN_DATA}/cache"}


def test_marketplace_lists_recx_search():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    sources = {p["name"]: p["source"] for p in marketplace["plugins"]}
    assert sources["recx-search"] == "./plugin-search"
    assert sources["recx"] == "./plugin"


def test_ci_validates_the_search_plugin():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "claude plugin validate --strict plugin-search" in ci


def test_find_skill_has_frontmatter_named_find():
    text = (PLUGIN / "skills" / "find" / "SKILL.md").read_text()
    assert text.startswith("---\nname: find\ndescription: ")
