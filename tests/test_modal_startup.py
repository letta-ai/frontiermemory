import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.environments.modal import ModalEnvironment
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from frontier_memory.modal import RetryingModalEnvironment
from frontier_memory.cli import main
from frontier_memory.corpus import write_sums


PULL_ERROR = "docker compose up failed: main Error toomanyrequests: Data limit exceeded"
BAKE_ERROR = "docker compose up failed: failed to execute bake: read |0: file already closed"


class ScriptedStrategy:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = []

    async def start(self, force_build):
        self.calls.append(("start", force_build))
        if self.failures:
            raise self.failures.pop(0)

    async def stop(self, delete):
        self.calls.append(("stop", delete))


def environment(tmp_path, failures, cls=RetryingModalEnvironment):
    env = cls(
        environment_dir=tmp_path, environment_name="startup-test", session_id="startup-test__env",
        trial_paths=TrialPaths(trial_dir=tmp_path / "trial"),
        task_env_config=EnvironmentConfig(docker_image="alpine:3.22"),
    )
    env._strategy = ScriptedStrategy(failures)
    return env


@pytest.mark.parametrize("message", [PULL_ERROR, BAKE_ERROR])
def test_transient_startup_recovers_before_agent_execution(tmp_path, monkeypatch, message):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    env = environment(tmp_path, [RuntimeError(message)])
    asyncio.run(env.start(force_build=True))
    assert env._strategy.calls == [("start", True), ("stop", True), ("start", True)]
    assert waits == [5]


def test_persistent_startup_failure_is_bounded(tmp_path, monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    final = RuntimeError(BAKE_ERROR)
    env = environment(tmp_path, [RuntimeError(PULL_ERROR), RuntimeError(BAKE_ERROR), final])
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(env.start(force_build=False))
    assert exc.value is final
    assert env._strategy.calls == [("start", False), ("stop", True), ("start", False), ("stop", True), ("start", False)]
    assert waits == [5, 15]


@pytest.mark.parametrize("error", [
    RuntimeError("docker compose up failed: manifest unknown"),
    RuntimeError("docker compose build failed: Dockerfile syntax error"),
    RuntimeError("docker compose up failed: unauthorized: authentication required"),
    RuntimeError("model execution failed: toomanyrequests"),
    RuntimeError("Unexpected stop reason: max_tokens_exceeded"),
    asyncio.CancelledError(),
])
def test_nontransient_errors_and_cancellation_are_not_retried(tmp_path, error):
    env = environment(tmp_path, [error])
    with pytest.raises(type(error)) as exc:
        asyncio.run(env.start(force_build=False))
    assert exc.value is error
    assert env._strategy.calls == [("start", False)]


def test_cleanup_failure_does_not_start_another_sandbox(tmp_path):
    env = environment(tmp_path, [RuntimeError(PULL_ERROR)])

    async def stop(delete):
        raise RuntimeError("sandbox termination failed")

    env._strategy.stop = stop
    with pytest.raises(RuntimeError, match="sandbox termination failed"):
        asyncio.run(env.start(force_build=False))
    assert env._strategy.calls == [("start", False)]


def test_swallowed_teardown_error_cannot_leak_a_live_sandbox(tmp_path):
    env = environment(tmp_path, [RuntimeError(PULL_ERROR)])
    env._sandbox = SimpleNamespace(poll=SimpleNamespace(aio=AsyncMock(return_value=None)))
    with pytest.raises(RuntimeError, match="still running; refusing startup retry"):
        asyncio.run(env.start(force_build=False))
    assert env._strategy.calls == [("start", False), ("stop", True)]


def test_execution_after_start_is_never_retried(tmp_path):
    env = environment(tmp_path, [])

    async def exec(*args, **kwargs):
        env._strategy.calls.append(("exec",))
        raise RuntimeError(PULL_ERROR)

    env._strategy.exec = exec

    async def run():
        await env.start(force_build=False)
        await env.exec("run-the-agent")

    with pytest.raises(RuntimeError, match="toomanyrequests"):
        asyncio.run(run())
    assert env._strategy.calls == [("start", False), ("exec",)]


@pytest.mark.parametrize("phase", ["generate-memory", "run-tasks"])
def test_cli_modal_path_recovers_startup_before_running_agent(tmp_path, monkeypatch, phase):
    async def sleep(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", sleep)
    started = []

    def harbor(command, dry_run):
        name = command[command.index("-e") + 1]
        if ":" in name:
            module, cls = name.split(":")
            selected = getattr(importlib.import_module(module), cls)
        else:
            assert name == "modal"
            selected = ModalEnvironment
        env = environment(tmp_path, [RuntimeError(BAKE_ERROR)], selected)
        asyncio.run(env.start(force_build=False))
        started.append(env)
        return 0

    monkeypatch.setattr("frontier_memory.cli.run_harbor", harbor)
    args = [phase, "--harness", "claude-code", "-m", "anthropic/claude-opus-5-5", "-e", "modal", "--dry-run"]
    if phase == "generate-memory":
        corpus = tmp_path / "corpus"
        (corpus / "inputs").mkdir(parents=True)
        (corpus / "inputs/t0001.json").write_text("{}")
        write_sums(corpus)
        args += ["--corpus", str(corpus), "--build-dir", str(tmp_path / "build")]
    assert main(args) == 0
    assert len(started) == 1
    assert started[0]._strategy.calls == [("start", False), ("stop", True), ("start", False)]
