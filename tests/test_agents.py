import asyncio
import json
from types import SimpleNamespace

import pytest
from harbor.models.trajectories import Trajectory

from frontier_memory.agents import DEFAULT_CLI_VERSIONS
from frontier_memory.agents.claude_code import MemoryClaudeCode
from frontier_memory.agents.codex import MemoryCodex, codex_events_atif


@pytest.mark.parametrize("agent,model", [
    (MemoryCodex, "openai/gpt-6-sol"),
    (MemoryClaudeCode, "anthropic/claude-opus-5-5"),
])
def test_invalid_memory_fails_at_start(tmp_path, agent, model):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "notes.md").write_text("no index")
    with pytest.raises(ValueError, match="MEMORY.md"):
        agent(logs_dir=tmp_path, model_name=model, memory_dir=str(bad))


def test_codex_events_atif_keeps_web_search_and_answer_last():
    events = [
        {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "thinking"}},
        {"type": "item.completed", "item": {"id": "2", "type": "web_search", "query": "answer key"}},
        {"type": "item.completed", "item": {"id": "3", "type": "command_execution", "command": "ls",
                                            "aggregated_output": "a", "exit_code": 0}},
        {"type": "item.completed", "item": {"id": "4", "type": "mcp_tool_call", "server": "company",
            "tool": "lookup", "arguments": {}, "result": {"content": [{"type": "text", "text": "found"}]}}},
        {"type": "item.completed", "item": {"id": "5", "type": "agent_message", "text": "final answer"}},
    ]
    trajectory = codex_events_atif("do it", "\n".join(json.dumps(e) for e in events), "gpt-6-sol")
    Trajectory.model_validate(trajectory)
    steps = trajectory["steps"]
    assert steps[0]["message"] == "do it"
    assert steps[-1]["message"] == "final answer"
    assert any(call["function_name"] == "web_search" for s in steps for call in s.get("tool_calls", []))


@pytest.mark.parametrize("exit_code", [0, 2])
def test_codex_tool_results_preserve_failure_status_in_valid_atif(exit_code):
    events = [
        {"type": "item.completed", "item": {"id": "command", "type": "command_execution",
            "command": "run-tests", "aggregated_output": "test output", "exit_code": exit_code}},
        {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "Done"}},
    ]
    result = codex_events_atif("Run tests", "\n".join(json.dumps(event) for event in events), "gpt-6-sol")
    trajectory = Trajectory.model_validate(result)
    observation = trajectory.steps[1].observation.results[0]
    assert observation.source_call_id == "command"
    assert observation.content == "test output"
    assert observation.extra["is_error"] is (exit_code != 0)


@pytest.mark.parametrize("agent,model,name", [
    (MemoryCodex, "openai/gpt-6-sol", "codex"),
    (MemoryClaudeCode, "anthropic/claude-opus-5-5", "claude-code"),
])
@pytest.mark.parametrize("override", [None, "9.9.9"])
def test_runtime_pin_rejects_a_different_preinstalled_cli(tmp_path, agent, model, name, override):
    adapter = agent(logs_dir=tmp_path, model_name=model, **({"version": override} if override else {}))
    expected = override or DEFAULT_CLI_VERSIONS[name]

    class Environment:
        installed = "0.0.0"

        async def exec(self, command):
            return SimpleNamespace(return_code=0, stdout=self.installed)

    environment = Environment()
    check = (adapter._installed_codex_satisfies_version if name == "codex"
             else adapter._installed_claude_satisfies_version)
    assert not asyncio.run(check(environment))
    environment.installed = expected
    assert asyncio.run(check(environment))
