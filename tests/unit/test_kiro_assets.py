"""Static checks for the Kiro custom agent and power shipped in this repo.

These tests only read files. They do not prove that Kiro loads the assets.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from chimericmesh.mcp_server import mcp

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / ".kiro" / "agents" / "mesh-responder.json"
WORKSPACE_MCP = ROOT / ".kiro" / "settings" / "mcp.json"
POWER = ROOT / "powers" / "chimericmesh-remediation"

READ_ONLY = {
    "list_incidents",
    "get_incident",
    "find_code_for_incident",
    "recent_events",
    "list_proposals",
    "get_proposal",
}
MUTATING = {"announce_to_irc", "propose_remediation", "complete_proposal", "resolve_incident"}

AGENT_FIELDS = {
    "name", "description", "prompt", "mcpServers", "tools", "permissions", "resources",
    "hooks", "includeMcpJson", "includePowers", "model", "welcomeMessage", "excludedTools",
}
PLUGIN_FIELDS = {
    "$schema", "name", "version", "description", "author", "keywords", "homepage",
    "repository", "license",
}


def _tool_names() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _server(config: dict) -> dict:
    return config["mcpServers"]["chimericmesh"]


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    assert match, f"{path} has no frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _allowed_mcp_tools(agent: dict) -> set[str]:
    allowed: set[str] = set()
    for rule in agent["permissions"]["rules"]:
        if rule["capability"] == "mcp" and rule["effect"] == "allow":
            allowed.update(m.split("/", 1)[1] for m in rule["match"])
    return allowed


# --- custom agent -----------------------------------------------------------


def test_agent_is_valid_json_with_documented_fields():
    agent = _load(AGENT)
    assert agent["name"] == "mesh-responder"
    assert set(agent) <= AGENT_FIELDS
    assert agent["prompt"].strip()


def test_agent_prompt_states_approval_protocol():
    prompt = _load(AGENT)["prompt"]
    for phrase in ("pending", "denied", "approved", "untrusted", "complete_proposal", "resolve_incident"):
        assert phrase in prompt


def test_agent_prompt_requires_concrete_proposals_and_evidence():
    prompt = _load(AGENT)["prompt"]
    for phrase in ("reversible", "exact files", "run the relevant tests", "get_incident", "recent_events",
                   "event rate", "Do not extend the change"):
        assert phrase in prompt
def test_agent_prompt_requires_reverifying_proposal_before_acting():
    prompt = _load(AGENT)["prompt"]
    for phrase in ("Immediately before ANY change", "call get_proposal again", "AND the action text and incident_id",
                   "the id is unknown", "ask for a new proposal", "Never rely on an id or an approval remembered",
                   "hub probably restarted", "re-investigate", "process restart", "separate step the human performs"):
        assert phrase in prompt


def test_remediation_skill_mirrors_reverification_rules():
    text = (POWER / "skills" / "remediation-loop" / "SKILL.md").read_text(encoding="utf-8")
    for phrase in ("Immediately before any change", "action text and incident id", "ask for a new proposal",
                   "remembered from earlier in the conversation", "hub probably restarted", "re-investigate",
                   "process restart", "separate step the human performs"):
        assert phrase in text

def test_agent_never_auto_allows_mutating_tools():
    agent = _load(AGENT)
    assert _allowed_mcp_tools(agent).isdisjoint(MUTATING)
    assert _allowed_mcp_tools(agent) == READ_ONLY
    for rule in agent["permissions"]["rules"]:
        if rule["capability"] in {"shell", "fs_write", "all", "builtin", "filesystem"}:
            assert rule["effect"] != "allow"
        assert not (rule["capability"] == "mcp" and rule["effect"] == "allow" and not rule.get("match"))
        assert not any("*" in m for m in rule.get("match", []) if rule["capability"] == "mcp")


def test_agent_auto_allowed_tools_exist_in_mcp_server():
    assert _allowed_mcp_tools(_load(AGENT)) <= _tool_names()


def test_agent_tools_list_is_minimal_and_valid():
    agent = _load(AGENT)
    names = _tool_names()
    for entry in agent["tools"]:
        if entry.startswith("@"):
            server, _, tool = entry[1:].partition("/")
            assert server == "chimericmesh"
            assert tool in names
        else:
            assert entry in {"read", "write", "shell"}
    assert "*" not in agent["tools"]
    assert agent["includeMcpJson"] is False


def test_agent_mcp_server_matches_module_and_workspace_config():
    agent_server = _server(_load(AGENT))
    assert agent_server["command"] == "python"
    assert agent_server["args"] == ["-m", "chimericmesh.mcp_server"]
    assert (ROOT / "src" / "chimericmesh" / "mcp_server.py").is_file()
    assert agent_server == _server(_load(WORKSPACE_MCP))
    assert set(agent_server["env"]) == {"PYTHONPATH", "CHIMERICMESH_URL"}


def test_agent_resources_exist():
    for resource in _load(AGENT)["resources"]:
        assert resource.startswith("file://")
        path = resource.removeprefix("file://")
        assert "*" not in path
        assert (ROOT / path).is_file()


# --- hooks ------------------------------------------------------------------

HOOKS = ROOT / ".kiro" / "hooks"


def _hooks(name: str) -> list[dict]:
    return _load(HOOKS / name)["hooks"]


def test_guard_hook_matcher_is_anchored_and_covers_write_tools():
    for hook in _hooks("guard-innerhtml.json"):
        matcher = hook["matcher"]
        assert matcher.startswith("^") and matcher.endswith("$")
        for tool in ("fs_write", "fs_append", "str_replace"):
            assert re.search(matcher, tool)
        for other in ("fs_write_extra", "my_str_replace", "read"):
            assert not re.search(matcher, other)


def test_hook_timeout_is_on_hook_not_action():
    for name in ("guard-innerhtml.json", "test-on-save.json"):
        for hook in _hooks(name):
            assert "timeout" not in hook["action"]
    assert _hooks("test-on-save.json")[0]["timeout"] == 120


def test_test_on_save_description_says_agent_saves():
    description = _hooks("test-on-save.json")[0]["description"]
    assert "agent" in description


def test_mcp_configs_rely_on_installed_package_prerequisite():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "pip install -e" in readme


# --- power ------------------------------------------------------------------


def test_power_required_files_exist():
    for rel in ("plugin.json", "mcp.json"):
        assert (POWER / rel).is_file()
    skills = sorted((POWER / "skills").glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} >= {"getting-started", "remediation-loop", "triage"}


def test_power_manifest_uses_documented_fields():
    plugin = _load(POWER / "plugin.json")
    assert set(plugin) <= PLUGIN_FIELDS
    assert plugin["name"] == POWER.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", plugin["name"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"])
    assert plugin["description"]
    assert plugin["author"]["name"]
    assert plugin["keywords"] and all(isinstance(k, str) for k in plugin["keywords"])


def test_power_mcp_json_matches_workspace_config():
    power_server = _server(_load(POWER / "mcp.json"))
    power_server = {k: v for k, v in power_server.items() if k != "type"}
    assert _server(_load(POWER / "mcp.json"))["type"] == "stdio"
    assert power_server == _server(_load(WORKSPACE_MCP))


def test_power_skills_have_name_and_description():
    for skill in (POWER / "skills").glob("*/SKILL.md"):
        meta = _frontmatter(skill)
        assert meta["name"] == skill.parent.name
        assert meta["description"]


def test_power_docs_name_only_real_tools_and_cover_all():
    names = _tool_names()
    text = "\n".join(p.read_text(encoding="utf-8") for p in (POWER / "skills").glob("*/SKILL.md"))
    mentioned = set(re.findall(r"`([a-z_]+)`", text)) & names
    assert mentioned == names
    catalogue = (POWER / "skills" / "getting-started" / "SKILL.md").read_text(encoding="utf-8")
    assert {n for n in names if f"- `{n}`:" in catalogue} == names
    # backticked snake_case words that look like tool names must exist
    for word in re.findall(r"`([a-z]+_[a-z_]+)`", text):
        assert word in names or word in {"total_events"}, word
