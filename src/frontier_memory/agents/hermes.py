"""Native Hermes /learn in Harbor's existing Docker or Modal sandbox."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import yaml
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.installed.hermes import Hermes as HarborHermes, HermesOptions as HarborHermesOptions
from harbor.agents.options import InstalledAgentOptions
from harbor.models.agent.context import AgentContext

from frontier_memory import hermes_dream
from frontier_memory.agents import memory_metadata
from frontier_memory.memory import check_memory

REMOTE = "/opt/frontier-memory-hermes"
ACTOR_HOME = "/tmp/hermes"
UV_VERSION = "0.11.21"


async def install_runtime(agent, environment, *, task_agent: bool = False) -> None:
    """Use PR #413's fixed Hermes checkout and private Python for both stages."""
    if task_agent:
        await agent.ensure_system_dependencies(environment, ("git", "python3", "python_venv", "ripgrep", "coreutils"))
    setup = (
        f"mkdir -p {REMOTE}/source && git -C {REMOTE}/source init && "
        f"git -C {REMOTE}/source remote add origin https://github.com/NousResearch/hermes-agent.git && "
        f"git -C {REMOTE}/source fetch --depth 1 origin {hermes_dream.HERMES_REF} && "
        f"git -C {REMOTE}/source checkout --detach FETCH_HEAD && "
        f"test \"$(git -C {REMOTE}/source rev-parse HEAD)\" = {hermes_dream.HERMES_REF} && "
    )
    # v0.22.2 supports Python 3.14 at runtime. Keep it independent of both the
    # generic dream image's Python and each task image's application Python.
    extras = "anthropic,mcp" if task_agent else "anthropic"
    setup += (
        f"python3 -m venv {REMOTE}/bootstrap && "
        f"{REMOTE}/bootstrap/bin/pip install --disable-pip-version-check uv=={UV_VERSION} && "
        f"UV_PYTHON_INSTALL_DIR={REMOTE}/python {REMOTE}/bootstrap/bin/uv venv "
        f"--python {hermes_dream.HERMES_PYTHON} --python-preference only-managed {REMOTE}/venv && "
        f"{REMOTE}/bootstrap/bin/uv pip install --python {REMOTE}/venv/bin/python "
        f"-e '{REMOTE}/source[{extras}]'"
    )
    await agent.exec_as_root(environment, command=setup, timeout_sec=600 if task_agent else 360)


class HermesOptions(InstalledAgentOptions):
    reasoning_effort: str = "high"


class HermesLearn(BaseInstalledAgent):
    capabilities = AgentCapabilities(atif=True)
    options_model = HermesOptions
    options: HermesOptions

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        if (self.model_name or "").partition("/")[0] not in {"anthropic", "openai"}:
            raise ValueError("Hermes dreaming requires anthropic/<model> or openai/<model>")
        if self.options.version not in (None, hermes_dream.HERMES_REF):
            raise ValueError("Hermes dreaming uses a fixed checkout; version overrides are not supported")

    @staticmethod
    def name() -> str:
        return "frontier-memory-hermes-learn"

    def version(self) -> str:
        return hermes_dream.HERMES_REF

    async def install(self, environment) -> None:
        # Hermes intentionally refuses wheel installs; keep its pinned editable checkout.
        await install_runtime(self, environment)
        await environment.upload_file(Path(hermes_dream.__file__), f"{REMOTE}/driver.py")

    async def run(self, instruction: str, environment, context: AgentContext) -> None:
        provider = self.model_name.partition("/")[0]
        key_name = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        key = self._get_env(key_name)
        if not key:
            raise ValueError(f"{key_name} is required for Hermes dreaming")
        logs = str(self.environment_logs_dir)
        command = shlex.join([f"{REMOTE}/venv/bin/python", f"{REMOTE}/driver.py",
            "--model", self.model_name, "--reasoning-effort", self.options.reasoning_effort, "--logs", logs])
        failed = False
        try:
            await self.exec_as_agent(environment, command=command, cwd="/workspace", env={key_name: key})
        except BaseException:
            failed = True
            raise
        finally:
            try:
                await environment.download_file(f"{logs}/hermes-result.json", self.logs_dir / "hermes-result.json")
                self.populate_context_post_run(context)
            except Exception:
                if not failed:
                    raise
                self.logger.warning("Could not collect Hermes failure metadata", exc_info=True)

    def populate_context_post_run(self, context: AgentContext) -> None:
        result = json.loads((self.logs_dir / "hermes-result.json").read_text())
        usage = result.get("usage") or {}
        if usage.get("session_input_tokens") is not None:
            read = usage.get("session_cache_read_tokens") or 0
            write = usage.get("session_cache_write_tokens") or 0
            context.n_input_tokens = usage["session_input_tokens"] + read + write
            context.n_cache_tokens = read
            context.n_output_tokens = usage.get("session_output_tokens")
        context.metadata = {**(context.metadata or {}), "frontier_memory_hermes": result}


class MemoryHermesOptions(HarborHermesOptions):
    memory_dir: str | None = None
    reasoning_effort: str = "high"


class MemoryHermes(HarborHermes):
    """Native Hermes CLI task agent, with notes and skills seeded before startup."""

    capabilities = AgentCapabilities(atif=True, mcp_servers=True)
    options_model = MemoryHermesOptions
    options: MemoryHermesOptions

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        provider, _, model = (self.model_name or "").partition("/")
        if provider not in {"anthropic", "openai"} or not model:
            raise ValueError("Hermes task agent requires anthropic/<model> or openai/<model>")
        if self.options.version not in (None, hermes_dream.HERMES_REF):
            raise ValueError("Hermes task agent uses the same fixed checkout as the dreamer")
        self._memory_dir = Path(self.options.memory_dir).expanduser().resolve() if self.options.memory_dir else None
        self._memory_report = check_memory(self._memory_dir) if self._memory_dir else None
        if self._memory_report is not None:
            if not self._memory_report["ok"]:
                raise ValueError(f"invalid memory: {self._memory_report['errors']}")

    @staticmethod
    def name() -> str:
        return "frontier-memory-hermes"

    def version(self) -> str:
        return hermes_dream.HERMES_REF

    def get_version_command(self) -> str:
        return f"{REMOTE}/venv/bin/hermes --version"

    async def install(self, environment) -> None:
        await install_runtime(self, environment, task_agent=True)
        if self.model_name == "openai/gpt-6.1-sol":
            # CLI delegates run in this interpreter too. Preserve DreamBench's
            # transport correction rather than merely recording requested max.
            patch = (
                "import pathlib,agent.reasoning_effort as r; p=pathlib.Path(r.__file__); t=p.read_text();"
                "old='GPT6_TIER_PREFIXES: tuple[str, ...] = (\"gpt-6-sol\", \"gpt-6-luna\")';"
                "new='GPT6_TIER_PREFIXES: tuple[str, ...] = (\"gpt-6-sol\", \"gpt-6.1-sol\", \"gpt-6-luna\")';"
                "assert t.count(old)==1 or t.count(new)==1, 'unexpected Hermes effort registry';"
                "p.write_text(t.replace(old,new));"
                "import importlib; importlib.reload(r);"
                "assert r.codex_supported_efforts('gpt-6.1-sol')[-1]=='max'"
            )
            await self.exec_as_root(environment, command=f"{REMOTE}/venv/bin/python -c {shlex.quote(patch)}")

    def _actor_config(self, model: str) -> str:
        config = yaml.safe_load(super()._build_config_yaml(model, self.options.max_turns))
        config.setdefault("agent", {}).update(reasoning_effort=self.options.reasoning_effort,
                                               disabled_toolsets=hermes_dream.DISABLED_TOOLSETS)
        # Native Hermes snapshots MEMORY.md/USER.md at startup. Other supplied
        # note files remain beside them so their relative references still work.
        config["memory"]["memory_enabled"] = self._memory_dir is not None
        config["memory"]["user_profile_enabled"] = bool(self._memory_dir and (self._memory_dir / "USER.md").is_file())
        return yaml.safe_dump(config)

    @with_prompt_template
    async def run(self, instruction: str, environment, context: AgentContext) -> None:
        provider, model = self.model_name.split("/", 1)
        key_name = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        key = self._get_env(key_name)
        if not key:
            raise ValueError(f"{key_name} is required for the Hermes task agent")
        logs = str(self.environment_logs_dir)
        env = {key_name: key, "HERMES_HOME": ACTOR_HOME, "TERMINAL_ENV": "local",
               "HARBOR_INSTRUCTION": instruction,
               "ANTHROPIC_BASE_URL" if provider == "anthropic" else "OPENAI_BASE_URL":
                   "https://api.anthropic.com" if provider == "anthropic" else "https://api.openai.com/v1"}
        await self.exec_as_agent(environment, command=f"test ! -e {ACTOR_HOME} && mkdir -p {ACTOR_HOME}", env=env)
        config = self.logs_dir / "hermes-config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(self._actor_config(model))
        await environment.upload_file(config, f"{ACTOR_HOME}/config.yaml")
        if self._memory_dir:
            await environment.upload_dir(self._memory_dir, f"{ACTOR_HOME}/memories")
            if (self._memory_dir / "skills").is_dir():
                await environment.upload_dir(self._memory_dir / "skills", f"{ACTOR_HOME}/skills")
        if environment.default_user is not None:
            await self.exec_as_root(environment, command=
                f"chown -R {shlex.quote(str(environment.default_user))} {ACTOR_HOME}")
        mcp = self._build_register_mcp_servers_command()
        if mcp:
            await self.exec_as_agent(environment, command=mcp, env=env)
        parts = [f"{REMOTE}/venv/bin/hermes", "--yolo", "chat", "-Q", "--model", model,
                 "--provider", "anthropic" if provider == "anthropic" else "openai-api"]
        if self.options.toolsets:
            toolsets = [name.strip() for name in self.options.toolsets.split(",") if name.strip()]
            for server in self.mcp_servers:
                if self.options.toolsets not in ("all", "*") and server.name not in toolsets:
                    toolsets.append(server.name)
            parts += ["--toolsets", ",".join(toolsets)]
        command = shlex.join(parts) + ' -q "$HARBOR_INSTRUCTION"'
        failed = False
        try:
            await self.exec_as_agent(environment, command=f"{command} 2>&1 | tee {shlex.quote(logs + '/hermes.txt')}", env=env)
        except BaseException:
            failed = True
            raise
        finally:
            # Export all sources so delegated sessions are retained alongside
            # the CLI's `oneshot` session, as in the existing DreamBench actor.
            try:
                await self.exec_as_agent(environment, command=(
                    f"{REMOTE}/venv/bin/hermes sessions export {shlex.quote(logs + '/hermes-session.jsonl')}"
                ), env=env, timeout_sec=60)
                await environment.download_file(f"{logs}/hermes-session.jsonl", self.logs_dir / "hermes-session.jsonl")
                self.populate_context_post_run(context)
            except Exception:
                if not failed:
                    raise
                context.metadata = {**(context.metadata or {}), **memory_metadata(self._memory_report)}
                self.logger.warning("Could not collect Hermes actor failure logs", exc_info=True)

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        path = self.logs_dir / "hermes-session.jsonl"
        if path.is_file():
            sessions = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            if sessions:
                context.n_input_tokens = sum((s.get("input_tokens") or 0) + (s.get("cache_read_tokens") or 0)
                    + (s.get("cache_write_tokens") or 0) for s in sessions)
                context.n_cache_tokens = sum(s.get("cache_read_tokens") or 0 for s in sessions)
                context.n_output_tokens = sum(s.get("output_tokens") or 0 for s in sessions)
        context.metadata = {**(context.metadata or {}), **memory_metadata(self._memory_report)}
