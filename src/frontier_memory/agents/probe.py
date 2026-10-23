"""A test-only memory agent that checks the dream task's network rules without a model.

It runs ``curl`` inside the task container during the agent phase, where the
task's allowlist applies, and writes what it could reach to ``/memory/MEMORY.md``
and ``<trial>/agent/network-probe.json``.

    harbor run -p <dream task> -a frontier_memory.agents.probe:NetworkProbe \\
        --allow-agent-host api.anthropic.com \\
        --ak 'urls=["https://api.anthropic.com","https://example.com"]'
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, override

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

DEFAULT_URLS = ["https://api.anthropic.com", "https://api.openai.com", "https://example.com", "https://github.com"]


class NetworkProbe(BaseAgent):
    def __init__(self, logs_dir: Path, urls: list[str] | None = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(logs_dir, *args, **kwargs)
        self._urls = list(urls or DEFAULT_URLS)

    @staticmethod
    @override
    def name() -> str:
        return "frontier-memory-network-probe"

    @override
    def version(self) -> str:
        return "1.0.0"

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    @override
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        results = {}
        for url in self._urls:
            # Any HTTP status, even 404, means the connection got through; 000 means it did not.
            probe = await environment.exec(
                f"curl -sS -o /dev/null -w '%{{http_code}}' --max-time 15 {shlex.quote(url)}", timeout_sec=60
            )
            output = ((probe.stdout or "") + (probe.stderr or "")).strip()
            code = output[-3:]
            results[url] = {"reachable": code.isdigit() and code != "000", "http_code": code,
                            "detail": output[:300]}
        (Path(self.logs_dir) / "network-probe.json").write_text(json.dumps(results, indent=2) + "\n")
        lines = ["# Network probe", ""] + [
            f"- {url}: {'reachable' if r['reachable'] else 'blocked'} (HTTP {r['http_code'] or 'none'})"
            for url, r in results.items()
        ]
        body = shlex.quote("\n".join(lines) + "\n")
        await environment.exec(f"printf '%s' {body} > /memory/MEMORY.md", timeout_sec=30)
