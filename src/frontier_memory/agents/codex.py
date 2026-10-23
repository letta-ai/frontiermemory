"""Harbor's Codex task agent with a dream's memory seeded as native Codex memories.

Codex CLI puts ``$CODEX_HOME/memories/memory_summary.md`` into its developer
instructions when the ``memories`` feature is on, and can open the other files
in that folder when it needs them. This agent copies the memory folder there
before Codex starts. A memory without ``memory_summary.md`` gets a copy of its
``MEMORY.md`` under that name. Every run turns the feature on, the no-memory
control too, so the memory is the only difference.

Codex runs as ``codex exec --ephemeral``. Otherwise its startup memory sync
deletes every rollout summary its own state database does not know, which is
every seeded one. Ephemeral runs save no session log, so this agent builds the
trajectory and token counts from the ``--json`` event stream.

Web search is off unless the run sets ``web_search``.

    harbor run -a frontier_memory.agents.codex:MemoryCodex \\
        -m openai/gpt-6-sol --ak memory_dir=/abs/path/to/memory ...
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, override

from harbor.agents.installed.codex import Codex, CodexOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field

from frontier_memory.agents import DEFAULT_CLI_VERSIONS, memory_metadata
from frontier_memory.memory import ENTRY_FILE, check_memory

CODEX_ENTRY_FILE = "memory_summary.md"


def codex_event_usage(events_path: Path) -> dict[str, int] | None:
    """Token usage from the last ``turn.completed`` event of ``codex exec --json``."""
    try:
        lines = events_path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    usage = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # Harbor tees Codex's stderr into the same file.
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    return usage


def codex_events_atif(instruction: str, events_text: str, model: str | None) -> dict[str, Any] | None:
    """ATIF trajectory from the ``codex exec --json`` stream.

    Tool calls become steps with their observations, and the last agent message
    becomes the final step (APEX graders read it as the answer).
    """
    events: list[dict[str, Any]] = []
    for line in events_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if event.get("type") != "item.completed":
            continue
        kind = item.get("type")
        if kind == "agent_message":
            events.append({"source": "agent", "model_name": model, "message": item.get("text") or ""})
        elif kind in ("mcp_tool_call", "command_execution", "web_search"):
            call_id = str(item.get("id"))
            if kind == "mcp_tool_call":
                name = f"mcp__{item.get('server')}__{item.get('tool')}"
                arguments = item.get("arguments") or {}
                result = item.get("result") or {}
                content = "\n".join(
                    c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)
                ) or json.dumps(item.get("error"))
                failed = bool(item.get("error"))
            elif kind == "command_execution":
                name, arguments = "exec_command", {"cmd": item.get("command")}
                content = item.get("aggregated_output") or ""
                failed = item.get("exit_code") not in (0, None)
            else:  # kept so a trace review can see any web search
                name, arguments = "web_search", {"query": item.get("query")}
                content, failed = "", False
            events.append({
                "source": "agent", "model_name": model, "message": f"Executed {name} {call_id}",
                "tool_calls": [{"tool_call_id": call_id, "function_name": name,
                                "arguments": arguments if isinstance(arguments, dict) else {"value": arguments}}],
                "observation": {"results": [{"source_call_id": call_id, "content": content,
                                             "extra": {"is_error": failed}}]},
            })
    messages = [i for i, e in enumerate(events) if "tool_calls" not in e]
    if not messages:
        return None
    final = events.pop(messages[-1])  # the answer goes last
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    steps = [{"source": "user", "message": instruction}, *events, final]
    return {
        "schema_version": "ATIF-v1.8",
        "session_id": "codex-ephemeral",
        "agent": {"name": "frontier-memory-codex", "version": "unknown", "model_name": model},
        "steps": [{"step_id": i, "timestamp": timestamp, **step} for i, step in enumerate(steps, start=1)],
        "extra": {"source": "codex exec --json", "native_status": "completed"},
    }


class MemoryCodexOptions(CodexOptions):
    memory_dir: str | None = Field(default=None, description="Host folder seeded as native Codex memories.")


class MemoryCodex(Codex):
    """Harbor's Codex with an optional dream memory and web search off."""

    options_model = MemoryCodexOptions
    options: MemoryCodexOptions

    def __init__(self, logs_dir: Path, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("version", DEFAULT_CLI_VERSIONS["codex"])
        if "web_search" not in kwargs:
            kwargs.setdefault("disable_web_search", True)
        super().__init__(logs_dir, *args, **kwargs)
        memory_dir = self.options.memory_dir
        self._memory_dir = Path(memory_dir).expanduser().resolve() if memory_dir else None
        self._memory_report: dict[str, Any] | None = None
        if self._memory_dir is not None:
            self._memory_report = check_memory(self._memory_dir)
            if not self._memory_report["ok"]:
                raise ValueError(f"invalid memory {self._memory_dir}: {self._memory_report['errors']}")

    @staticmethod
    @override
    def name() -> str:
        return "frontier-memory-codex"

    @override
    def _build_effective_config(self, openai_base_url: str | None = None) -> dict[str, Any]:
        config = super()._build_effective_config(openai_base_url)
        features = config.setdefault("features", {})
        if not isinstance(features, dict):
            raise ValueError("Codex config features must be a TOML table")
        features["memories"] = True
        return config

    @override
    def build_cli_flags(self) -> str:
        return f"{super().build_cli_flags()} --ephemeral".strip()

    @override
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        # Harbor fills the context from the logs only when run() leaves it empty.
        remote_root = PurePosixPath(self._REMOTE_CODEX_HOME) / "memories"
        if self._memory_dir is not None and self._memory_report is not None:
            files = [entry["path"] for entry in self._memory_report["files"]]
            uploads = [(rel, rel) for rel in files]
            if CODEX_ENTRY_FILE not in files:
                uploads.append((ENTRY_FILE, CODEX_ENTRY_FILE))
            remote_dirs = sorted({(remote_root / target).parent.as_posix() for _, target in uploads})
            await self.exec_as_root(
                environment, command="mkdir -p " + " ".join(shlex.quote(d) for d in remote_dirs)
            )
            for source, target in uploads:
                await environment.upload_file(self._memory_dir / source, (remote_root / target).as_posix())
            if environment.default_user is not None:
                # Codex writes its config, auth and memory updates as the agent user.
                await self.exec_as_root(
                    environment,
                    command=f"chown -R {shlex.quote(str(environment.default_user))} "
                    f"{shlex.quote(PurePosixPath(self._REMOTE_CODEX_HOME).as_posix())}",
                )
        try:
            await super().run(instruction, environment, context)
            await self._upload_event_trajectory(instruction, environment)
        finally:
            if self._memory_dir is not None:
                await self.exec_as_root(environment, command=f"rm -rf {shlex.quote(remote_root.as_posix())}")

    async def _upload_event_trajectory(self, instruction: str, environment: BaseEnvironment) -> None:
        """Give graders and reviewers the ``--json`` stream as an ATIF trajectory."""
        events = await environment.exec("cat /logs/agent/codex.txt 2>/dev/null || true", timeout_sec=60)
        trajectory = codex_events_atif(instruction, events.stdout or "", self.model_name)
        if trajectory is None:
            return
        local = Path(self.logs_dir) / "trajectory.json"
        local.write_text(json.dumps(trajectory, indent=2))
        await environment.upload_file(local, "/logs/agent/trajectory.json")

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        if context.n_input_tokens is None:
            usage = codex_event_usage(Path(self.logs_dir) / self._OUTPUT_FILENAME)
            if usage:
                context.n_input_tokens = usage.get("input_tokens")
                context.n_cache_tokens = usage.get("cached_input_tokens")
                context.n_output_tokens = usage.get("output_tokens")
        context.metadata = {**(context.metadata or {}), **memory_metadata(self._memory_report)}
