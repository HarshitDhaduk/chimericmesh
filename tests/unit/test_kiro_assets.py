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


# Parameter, field and file words (snake_case, in backticks) that the three skills use besides tool names.
# Computed once from the skills and written out; a new word in a skill needs a deliberate edit here.
SKILL_WORDS = {
    "after_id", "code_folder", "estimated_lines", "exclude_ids", "incident_id", "min_severity", "next_after_id",
}
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
    # a backticked snake_case word must be a tool or an entry of the explicit allow-list SKILL_WORDS
    for word in re.findall(r"`([a-z]+_[a-z_]+)`", text):
        assert word in names or word in SKILL_WORDS, word


# --- mesh-watcher profile (agent-loop task 9.1) -----------------------------
WATCHER = ROOT / ".kiro" / "agents" / "mesh-watcher.json"
# SHA-256 of both files as committed at 95922aa (the main tree); R5.3, B8: they never change.
BASELINE_SHA256 = {
    ".kiro/agents/mesh-responder.json": "bd67aeb787e036b6d0fc829d7c5c71594d6cc9dc786d36d6155a6e75c61d9be6",
    ".kiro/settings/mcp.json": "55fb84a5d0d7eac79f86a70499b46fba5f847e63985f0fb224988cc309214824",
}
WATCHER_READ_ONLY = READ_ONLY | {
    "list_services", "get_service", "service_events", "explain_log_window", "find_code_for_service",
    "verify_proposal", "wait_for_work",
}
WATCHER_FS_WRITE_DENY = [
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/admin.token", "**/chimericmesh.db*",
    "~/.chimericmesh/**",
]
WATCHER_FS_READ_DENY = ["**/admin.token", "**/chimericmesh.db*", "~/.chimericmesh/**"]
def _sha256(rel: str) -> str:
    import hashlib
    return hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
def test_watcher_is_valid_json_with_documented_fields():
    agent = _load(WATCHER)
    assert agent["name"] == "mesh-watcher"
    assert set(agent) <= AGENT_FIELDS
    assert agent["prompt"].strip() and agent["description"].strip()
    assert agent["includeMcpJson"] is False and agent["includePowers"] is False
def test_watcher_tools_are_the_three_builtins_and_all_17_mcp_tools():
    agent = _load(WATCHER)
    names = _tool_names()
    assert len(names) == 17
    assert "*" not in agent["tools"]
    assert {t for t in agent["tools"] if not t.startswith("@")} == {"read", "write", "shell"}
    assert {t for t in agent["tools"] if t.startswith("@")} == {f"@chimericmesh/{n}" for n in names}
    assert len(agent["tools"]) == len(set(agent["tools"])) == 20
def test_watcher_mcp_server_is_the_workspace_one_plus_request_timeout():
    server = dict(_server(_load(WATCHER)))
    assert server.pop("requestTimeout") >= 60000
    assert server == _server(_load(WORKSPACE_MCP))
def test_watcher_auto_allows_only_read_only_tools_that_exist():
    agent = _load(WATCHER)
    allowed = _allowed_mcp_tools(agent)
    assert allowed == WATCHER_READ_ONLY
    assert allowed <= _tool_names()
    assert allowed.isdisjoint(MUTATING)
    for rule in agent["permissions"]["rules"]:
        if rule["effect"] == "allow":
            assert rule["capability"] == "mcp"
            assert rule["match"] and not any("*" in m for m in rule["match"])
            assert all(m.startswith("chimericmesh/") for m in rule["match"])
def test_watcher_state_changing_tools_ask():
    asked = set()
    for rule in _load(WATCHER)["permissions"]["rules"]:
        if rule["capability"] == "mcp" and rule["effect"] == "ask":
            asked.update(m.split("/", 1)[1] for m in rule["match"])
    assert asked == MUTATING
    assert asked.isdisjoint(_allowed_mcp_tools(_load(WATCHER)))
def test_watcher_has_no_shell_or_fs_write_allow_or_ask_and_denies_are_exact():
    rules = _load(WATCHER)["permissions"]["rules"]
    assert {r["capability"] for r in rules} <= {"mcp", "fs_write", "fs_read"}
    assert {r["effect"] for r in rules} <= {"allow", "ask", "deny"}
    assert not [r for r in rules if r["capability"] == "shell"]
    for rule in rules:
        if rule["capability"] in {"fs_write", "fs_read"}:
            assert rule["effect"] == "deny"
    assert [m for r in rules if r["capability"] == "fs_write" for m in r["match"]] == WATCHER_FS_WRITE_DENY
    assert [m for r in rules if r["capability"] == "fs_read" for m in r["match"]] == WATCHER_FS_READ_DENY
def test_watcher_resources_exist():
    for resource in _load(WATCHER)["resources"]:
        assert resource.startswith("file://") and "*" not in resource
        assert (ROOT / resource.removeprefix("file://")).is_file()
def test_watcher_description_calls_the_home_globs_check_live():
    description = _load(WATCHER)["description"]
    assert "~/.chimericmesh/**" in description and "check live" in description
def test_responder_and_workspace_mcp_are_byte_identical_to_main():
    for rel, digest in BASELINE_SHA256.items():
        assert _sha256(rel) == digest, rel
def test_power_manifest_and_mcp_json_keep_their_behaviour():
    assert set(_load(POWER / "plugin.json")) <= PLUGIN_FIELDS
    assert _server(_load(POWER / "mcp.json"))["env"] == _server(_load(WORKSPACE_MCP))["env"]


# --- power skills (agent-loop task 9.2) -------------------------------------
from skill_baseline import ORIGINAL_SKILL_LINES  # noqa: E402
SKILL_LINES_REPLACED: set[str] = set()  # original lines the update replaced; none: the change is additive (R6.1)
def _skill_text(name: str) -> str:
    return (POWER / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
def test_skills_mention_all_17_tools_and_getting_started_lists_each():
    names = _tool_names()
    assert len(names) == 17
    text = "\n".join(p.read_text(encoding="utf-8") for p in (POWER / "skills").glob("*/SKILL.md"))
    assert {n for n in names if f"`{n}`" in text} == names
    catalogue = _skill_text("getting-started")
    assert [n for n in names if f"- `{n}`:" not in catalogue] == []
def test_watch_mode_skill_has_matching_name_and_description():
    skill = POWER / "skills" / "watch-mode" / "SKILL.md"
    assert skill.is_file()
    meta = _frontmatter(skill)
    assert meta["name"] == "watch-mode" == skill.parent.name
    assert meta["description"]
def test_original_skill_lines_survive_the_update():
    for name, lines in ORIGINAL_SKILL_LINES.items():
        text = _skill_text(name)
        for line in lines:
            assert line in SKILL_LINES_REPLACED or line in text, (name, line)
def test_replaced_lines_are_real_original_lines():
    originals = {line for lines in ORIGINAL_SKILL_LINES.values() for line in lines}
    assert SKILL_LINES_REPLACED <= originals
def test_getting_started_explains_services_proposals_and_auto_approval():
    text = _skill_text("getting-started")
    section = text.split("## Services, proposals and auto-approval", 1)
    assert len(section) == 2, "section missing"
    body = section[1].split("\n## ", 1)[0].lower()
    for phrase in ("service", "code folder", "files", "estimated_lines", "verification", "auto-approval",
                   "the hub decides", "no tool", "create, edit, widen or resume a rule", "watch-mode"):
        assert phrase in body, phrase
    assert len(body.splitlines()) <= 14  # one short section
def test_skill_words_has_no_stale_entry():
    text = "\n".join(p.read_text(encoding="utf-8") for p in (POWER / "skills").glob("*/SKILL.md"))
    used = set(re.findall(r"`([a-z]+_[a-z_]+)`", text))
    assert SKILL_WORDS <= used
    assert SKILL_WORDS.isdisjoint(_tool_names())
def test_skills_never_offer_a_tool_that_decides_or_changes_rules():
    for name in ("getting-started", "watch-mode", "remediation-loop"):
        lowered = _skill_text(name).lower()
        assert "decide_proposal" not in lowered and "create_rule" not in lowered
        assert "approve_proposal" not in lowered
def test_watch_mode_names_the_profile_and_the_wait():
    text = _skill_text("watch-mode")
    for phrase in ("mesh-watcher", "wait_for_work", "25 seconds", "timeout"):
        assert phrase in text
