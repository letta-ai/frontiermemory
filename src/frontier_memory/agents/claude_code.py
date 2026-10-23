"""Harbor's Claude Code task agent with a dream's memory seeded as Claude Code auto-memory.

Claude Code loads ``MEMORY.md`` from ``$CLAUDE_CONFIG_DIR/projects/<project>/memory/``
at session start and can open the other files in that folder when it needs
them. ``<project>`` is the working directory with every character other than a
letter, digit or ``-`` replaced by ``-`` (``/app`` becomes ``-app``).

Harbor's own ``memory_dir`` option copies a folder that is already in the
container to ``-app`` only. This agent takes a host folder, uploads it, and
registers it for the task's actual working directory.

WebSearch and WebFetch are off unless the run sets ``disable_web_search=false``.

    harbor run -a frontier_memory.agents.claude_code:MemoryClaudeCode \\
        -m anthropic/claude-opus-5-5 --ak memory_dir=/abs/path/to/memory ...
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, override

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from frontier_memory.agents import DEFAULT_CLI_VERSIONS, memory_metadata
from frontier_memory.memory import check_memory

REMOTE_MEMORY_DIR = PurePosixPath("/tmp/frontier-memory")


def project_key(cwd: str) -> str:
    """Claude Code's project folder name for a working directory."""
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


class MemoryClaudeCode(ClaudeCode):
    """Harbor's Claude Code with an optional dream memory and web tools off."""

    def __init__(self, logs_dir: Path, memory_dir: str | None = None, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("version", DEFAULT_CLI_VERSIONS["claude-code"])
        kwargs.setdefault("disable_web_search", True)
        host_dir = Path(memory_dir).expanduser().resolve() if memory_dir else None
        super().__init__(
            logs_dir, *args, memory_dir=REMOTE_MEMORY_DIR.as_posix() if host_dir else None, **kwargs
        )
        self._memory_host_dir = host_dir
        self._memory_report: dict[str, Any] | None = None
        self._project_key = "-app"
        if host_dir is not None:
            self._memory_report = check_memory(host_dir)
            if not self._memory_report["ok"]:
                raise ValueError(f"invalid memory {host_dir}: {self._memory_report['errors']}")

    @staticmethod
    @override
    def name() -> str:
        return "frontier-memory-claude-code"

    @override
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        if self._memory_host_dir is not None:
            await self.exec_as_root(environment, command=f"rm -rf {shlex.quote(REMOTE_MEMORY_DIR.as_posix())}")
            await environment.upload_dir(self._memory_host_dir, REMOTE_MEMORY_DIR.as_posix())
            # The agent user copies it into its config dir, so it must be able to read it.
            await self.exec_as_root(environment, command=f"chmod -R a+rX {shlex.quote(REMOTE_MEMORY_DIR.as_posix())}")
            cwd = await environment.exec("pwd", timeout_sec=30)
            if cwd.return_code == 0 and (cwd.stdout or "").strip():
                self._project_key = project_key((cwd.stdout or "").strip().splitlines()[-1])
        await super().run(instruction, environment, context)

    @override
    def _build_register_memory_command(self) -> str | None:
        if self._memory_host_dir is None:
            return None
        target = f"$CLAUDE_CONFIG_DIR/projects/{self._project_key}/memory"
        return (
            f"(mkdir -p {target} && "
            f"cp -r {shlex.quote(REMOTE_MEMORY_DIR.as_posix())}/. {target}/)"
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        context.metadata = {
            **(context.metadata or {}),
            **memory_metadata(self._memory_report),
            "frontier_memory_claude_project": self._project_key,
        }
