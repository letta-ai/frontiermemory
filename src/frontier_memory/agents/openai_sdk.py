"""Harbor adapter for the native OpenAI Agents SDK memory pipeline."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from pydantic import Field
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.options import InstalledAgentOptions
from harbor.models.agent.context import AgentContext

from frontier_memory import sdk_dream

REMOTE = "/opt/frontier-memory-sdk"


class SDKOptions(InstalledAgentOptions):
    phase_one_concurrency: int = Field(default=sdk_dream.PHASE_ONE_CONCURRENCY, gt=0)
    phase_one_token_limit: int | None = Field(default=None, gt=0)
    reasoning_effort: str | None = None


class OpenAISandboxMemory(BaseInstalledAgent):
    """Both SDK stages run inside Harbor's existing Docker or Modal container."""

    capabilities = AgentCapabilities(atif=True)
    options_model = SDKOptions
    options: SDKOptions

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        if not (self.model_name or "").startswith("openai/"):
            raise ValueError("openai-sandbox-memory requires an openai/<model> handle")
        if self.options.version not in (None, sdk_dream.SDK_VERSION):
            raise ValueError(f"openai-sandbox-memory is pinned to SDK {sdk_dream.SDK_VERSION}")

    @staticmethod
    def name() -> str:
        return "openai-sandbox-memory"

    def version(self) -> str:
        return sdk_dream.SDK_VERSION

    async def install(self, environment) -> None:
        result = await self.exec_as_root(environment, command=(
            f"python3 -m venv {REMOTE}/venv && "
            f"{REMOTE}/venv/bin/pip install --disable-pip-version-check "
            f"openai-agents=={sdk_dream.SDK_VERSION}"
        ), timeout_sec=360)
        (self.logs_dir / "setup" / "sdk-install.log").write_text((result.stdout or "") + (result.stderr or ""))
        source = Path(sdk_dream.__file__)
        await environment.upload_file(source, f"{REMOTE}/driver.py")
        await environment.upload_file(source.with_name("sdk_dream_instruction.md"), f"{REMOTE}/instruction.md")

    async def run(self, instruction: str, environment, context: AgentContext) -> None:
        key = self._get_env("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY is required for openai-sandbox-memory")
        logs = str(self.environment_logs_dir)
        command = [f"{REMOTE}/venv/bin/python", f"{REMOTE}/driver.py", "--model", self.model_name,
                   "--instruction", f"{REMOTE}/instruction.md", "--logs", logs,
                   "--phase-one-concurrency", str(self.options.phase_one_concurrency)]
        if self.options.phase_one_token_limit is not None:
            command += ["--phase-one-token-limit", str(self.options.phase_one_token_limit)]
        if self.options.reasoning_effort is not None:
            command += ["--reasoning-effort", self.options.reasoning_effort]
        failed = False
        try:
            await self.exec_as_agent(environment, command=shlex.join(command), env={"OPENAI_API_KEY": key})
        except BaseException:
            failed = True
            raise
        finally:
            # Persist usage/failure metadata even if the driver exits nonzero.
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            try:
                await environment.download_file(f"{logs}/sdk-result.json", self.logs_dir / "sdk-result.json")
                self.populate_context_post_run(context)
            except Exception:
                if not failed:
                    raise
                self.logger.warning("Could not collect SDK failure metadata", exc_info=True)

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / "sdk-result.json"
        if not path.is_file():
            return
        result = json.loads(path.read_text())
        usage = list((result.get("usage") or {}).values())
        if usage:
            context.n_input_tokens = sum(u["input_tokens"] for u in usage)
            context.n_cache_tokens = sum(u["cached_input_tokens"] for u in usage)
            context.n_output_tokens = sum(u["output_tokens"] for u in usage)
        context.metadata = {**(context.metadata or {}), "frontier_memory_sdk": result}
