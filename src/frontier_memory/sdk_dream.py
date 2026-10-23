"""Native OpenAI SDK memory generation inside a Harbor-provided Linux sandbox.

This file is uploaded as a standalone driver. SDK imports stay inside the driver
process: Harbor/LiteLLM and the SDK require incompatible OpenAI client versions.
The SDK's UnixLocalSandboxClient uses the existing container, not nested Docker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any

SDK_VERSION = "0.23.1"
PHASE_ONE_CONCURRENCY = 40
NO_PHASE_ONE_TOKEN_LIMIT = 10**15


def build_rollouts(inputs: Path) -> dict[str, dict[str, Any]]:
    """Preserve each released source verbatim in one SDK rollout, in corpus order."""
    rollouts = {}
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index, path in enumerate(sorted(inputs.iterdir())):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"expected a regular corpus file: {path.name}")
        key = path.stem
        if key in rollouts:
            raise ValueError(f"duplicate rollout ID: {key}")
        rollouts[key] = {
            "updated_at": (base + timedelta(minutes=index)).isoformat(),
            "trajectory": key,
            "files": {f"inputs/{path.name}": path.read_bytes().decode("utf-8")},
        }
    if not rollouts:
        raise ValueError("the corpus has no trajectories")
    return rollouts


def export_memory(source: Path, destination: Path) -> list[str]:
    """Only task-agent-visible files, never raw memories or source rollouts."""
    if source.is_symlink():
        raise ValueError("SDK memory root must not be a symlink")
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("memory output must be an empty, non-symlink directory")
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    for name in ("MEMORY.md", "memory_summary.md", "skills", "rollout_summaries"):
        path = source / name
        if path.is_symlink():
            raise ValueError(f"symlink in SDK memory: {name}")
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for item in candidates:
            if item.is_symlink():
                raise ValueError(f"symlink in SDK memory: {item}")
            if item.is_dir() or not item.exists():
                continue
            if not item.is_file():
                raise ValueError(f"non-regular file in SDK memory: {item}")
            relative = item.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
            files.append(relative.as_posix())
    for entry in ("MEMORY.md", "memory_summary.md"):
        if entry not in files or not (destination / entry).read_text(encoding="utf-8").strip():
            raise ValueError(f"SDK produced no nonempty {entry}")
    return sorted(files)


class Evidence:
    """Local-only native spans and model requests/responses, including failed runs."""

    def __init__(self, logs: Path, model: str):
        self.logs, self.model = logs, model
        self.steps: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.usage: dict[str, dict[str, int]] = {}

    def record(self, event: str, value: Any) -> None:
        with self.lock, (self.logs / "sdk-events.jsonl").open("a") as handle:
            handle.write(json.dumps({"event": event, "value": value}, default=str) + "\n")

    def step(self, source: str, value: Any, phase: str, **extra: Any) -> None:
        # Requests include tool results; responses retain reasoning and native
        # tool-call objects. Native SDK span records also retain tool outputs.
        self.steps.append({"step_id": len(self.steps) + 1, "source": source,
                           "message": json.dumps(value, default=str),
                           "extra": {"phase": phase, **extra}})

    def save_trajectory(self) -> None:
        if not self.steps:
            return
        trajectory = {
            "schema_version": "ATIF-v1.6", "session_id": self.logs.name,
            "agent": {"name": "openai-sandbox-memory", "version": SDK_VERSION, "model_name": self.model},
            "steps": self.steps,
            "notes": "SDK model requests/responses serialized verbatim. Phase-one calls may overlap; "
                     "steps record dispatch/completion order, not one linear conversation. "
                     "Tool execution spans are retained in sdk-events.jsonl.",
        }
        (self.logs / "trajectory.json").write_text(json.dumps(trajectory, indent=2))


async def generate_memory(*, inputs: Path, workspace: Path, instruction: str, model: str,
                          evidence: Evidence, concurrency: int = PHASE_ONE_CONCURRENCY,
                          token_limit: int | None = None, effort: str | None = None) -> None:
    """Use the pinned SDK's native phases and tools on the existing container."""
    from agents import ModelSettings, set_trace_processors
    from agents.sandbox import Manifest
    from agents.sandbox.capabilities import Memory
    from agents.sandbox.config import MemoryGenerateConfig
    from agents.sandbox.memory import manager as sdk_manager, phase_one
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
    from agents.models.openai_responses import OpenAIResponsesModel
    from agents.tracing import TracingProcessor
    from openai import AsyncOpenAI
    from openai.types.shared import Reasoning

    if version("openai-agents") != SDK_VERSION:
        raise ValueError(f"this driver requires openai-agents=={SDK_VERSION}")
    if concurrency < 1 or (token_limit is not None and token_limit < 1):
        raise ValueError("concurrency and token limit must be positive")
    if not model.startswith("openai/") or not model.removeprefix("openai/"):
        raise ValueError("openai-sandbox-memory requires an openai/<model> handle")
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("SDK workspace must be fresh")
    rollouts = build_rollouts(inputs)
    phase_one._PHASE_ONE_ROLLOUT_TOKEN_LIMIT = token_limit or NO_PHASE_ONE_TOKEN_LIMIT

    class LocalTraceProcessor(TracingProcessor):
        def on_trace_start(self, trace):
            evidence.record("trace_start", trace.export())

        def on_trace_end(self, trace):
            evidence.record("trace_end", trace.export())

        def on_span_start(self, span):
            pass

        def on_span_end(self, span):
            evidence.record("span", span.export())

        def shutdown(self):
            pass

        def force_flush(self):
            pass

    # Replace the hosted exporter: all traces stay in Harbor's agent logs.
    set_trace_processors([LocalTraceProcessor()])

    class RecordedModel(OpenAIResponsesModel):
        def __init__(self, phase, client):
            super().__init__(model.removeprefix("openai/"), openai_client=client)
            self.phase = phase

        async def get_response(self, *args, **kwargs):
            request = {"instructions": kwargs.get("system_instructions"), "input": kwargs.get("input"),
                       "model_settings": str(kwargs.get("model_settings"))}
            evidence.record("model_request", {"phase": self.phase, **request})
            evidence.step("user", request, self.phase)
            try:
                response = await super().get_response(*args, **kwargs)
            except Exception as exc:
                evidence.record("model_error", {"phase": self.phase, "error": f"{type(exc).__name__}: {exc}"})
                raise
            output = [item.model_dump(mode="json") for item in response.output]
            usage = response.usage
            totals = evidence.usage.setdefault(self.phase, {"input_tokens": 0, "cached_input_tokens": 0,
                                                           "output_tokens": 0})
            totals["input_tokens"] += usage.input_tokens
            totals["cached_input_tokens"] += usage.input_tokens_details.cached_tokens
            totals["output_tokens"] += usage.output_tokens
            evidence.record("model_response", {"phase": self.phase, "response_id": response.response_id,
                                                "output": output, "usage": asdict(usage)})
            evidence.step("agent", output, self.phase, response_id=response.response_id)
            return response

    class ConcurrentManager(sdk_manager.SandboxMemoryGenerationManager):
        async def _worker(self):
            # Same per-rollout extraction as the SDK, with FrontierMemory's bounded
            # worker fan-out. flush() still runs one native consolidation stage.
            async def drain():
                while True:
                    item = await self._queue.get()
                    try:
                        if item is sdk_manager._STOP:
                            self._queue.put_nowait(item)
                            return
                        await self._process_rollout_file(str(item))
                    finally:
                        self._queue.task_done()
            tasks = [asyncio.create_task(drain()) for _ in range(concurrency)]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    errors: list[str] = []

    class ErrorCapture(logging.Handler):
        def emit(self, record):
            detail = self.format(record)
            errors.append(detail)
            evidence.record("sdk_error", detail)

    handler = ErrorCapture(logging.ERROR)
    loggers = [logging.getLogger(name) for name in ("agents", "openai.agents")]
    for logger in loggers:
        logger.addHandler(handler)
    client = AsyncOpenAI(base_url="https://api.openai.com/v1")
    settings = {} if effort is None else {
        "phase_one_model_settings": ModelSettings(reasoning=Reasoning(effort=effort)),
        "phase_two_model_settings": ModelSettings(reasoning=Reasoning(effort=effort)),
    }
    memory = Memory(read=None, generate=MemoryGenerateConfig(
        max_raw_memories_for_consolidation=len(rollouts),
        phase_one_model=RecordedModel("phase_one", client), phase_two_model=RecordedModel("phase_two", client),
        extra_prompt=instruction, **settings))
    sandbox_client = UnixLocalSandboxClient(inherit_host_environment=False)
    session = await sandbox_client.create(manifest=Manifest(root=str(workspace.resolve())))
    try:
        async with session:
            manager = ConcurrentManager(session=session, memory=memory)
            for key, payload in rollouts.items():
                await manager.enqueue_rollout_payload(payload, rollout_id=key)
            await manager.flush()
            if errors:
                raise RuntimeError("SDK memory generation failed: " + "; ".join(errors))
            selection = workspace / "memories" / "phase_two_selection.json"
            if not selection.is_file():
                raise RuntimeError("SDK did not complete phase-two consolidation")
    finally:
        await sandbox_client.delete(session)
        await client.close()
        for logger in loggers:
            logger.removeHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--inputs", type=Path, default=Path("/evidence/inputs"))
    parser.add_argument("--output", type=Path, default=Path("/memory"))
    parser.add_argument("--logs", type=Path, default=Path("/logs/agent"))
    parser.add_argument("--instruction", type=Path, required=True)
    parser.add_argument("--phase-one-concurrency", type=int, default=PHASE_ONE_CONCURRENCY)
    parser.add_argument("--phase-one-token-limit", type=int)
    parser.add_argument("--reasoning-effort")
    args = parser.parse_args(argv)
    args.logs.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(args.logs, args.model)
    start = time.monotonic()
    meta: dict[str, Any] = {"backend": "openai-sandbox-memory", "sdk_version": SDK_VERSION,
                            "model": args.model, "phase_one_concurrency": args.phase_one_concurrency,
                            "phase_one_token_limit": args.phase_one_token_limit,
                            "reasoning_effort": args.reasoning_effort, "ok": False}
    try:
        instruction = args.instruction.read_text()
        (args.logs / "sdk-instruction.md").write_text(instruction)
        asyncio.run(generate_memory(inputs=args.inputs, workspace=args.logs / "sdk-workspace",
            instruction=instruction, model=args.model, evidence=evidence,
            concurrency=args.phase_one_concurrency, token_limit=args.phase_one_token_limit,
            effort=args.reasoning_effort))
        meta["memory_files"] = export_memory(args.logs / "sdk-workspace" / "memories", args.output)
        meta["ok"] = True
    except Exception as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
        evidence.record("driver_error", meta["error"])
    finally:
        evidence.save_trajectory()
        meta["usage"] = evidence.usage
        meta["elapsed_sec"] = round(time.monotonic() - start, 2)
        (args.logs / "sdk-result.json").write_text(json.dumps(meta, indent=2))
    return 0 if meta["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
