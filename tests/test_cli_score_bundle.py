import io
import json
import shlex
import tarfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

import pytest

from frontier_memory import claude_dream, hosted
from frontier_memory.agents import DEFAULT_CLI_VERSIONS
from frontier_memory.bundle import build_bundle
from frontier_memory.cli import main, resolve_memory
from frontier_memory.corpus import write_sums
from frontier_memory.memory import check_memory
from frontier_memory.score import score_jobs


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def make_corpus(root: Path) -> None:
    (root / "inputs").mkdir(parents=True)
    (root / "inputs" / "t0001.json").write_text("{}")
    write_sums(root)


def test_generate_memory_dry_run_adds_host_and_web_switch(tmp_path, capsys):
    make_corpus(tmp_path / "corpus")
    code = main(["generate-memory", "--corpus", str(tmp_path / "corpus"), "--harness", "claude-code",
                 "-m", "anthropic/claude-opus-5-5", "--build-dir", str(tmp_path / "build"), "--dry-run"])
    assert code == 0
    err = capsys.readouterr().err
    assert "--allow-agent-host api.anthropic.com" in err
    assert "--ak disable_web_search=true" in err
    assert "--job-name memory-" in err
    assert "memory-task-corpus" in err


@pytest.mark.parametrize("agent,model", [
    ("codex", "openai/gpt-6-sol"),
    ("claude-code", "anthropic/claude-opus-5-5"),
    ("frontier_memory.agents.codex:MemoryCodex", "openai/gpt-6-sol"),
    ("frontier_memory.agents.claude_code:MemoryClaudeCode", "anthropic/claude-opus-5-5"),
])
@pytest.mark.parametrize("override", [None, "9.9.9"])
def test_generation_forwards_one_runtime_version(tmp_path, capsys, agent, model, override):
    make_corpus(tmp_path / "corpus")
    args = ["generate-memory", "--corpus", str(tmp_path / "corpus"), "--harness", agent,
            "-m", model, "--build-dir", str(tmp_path / "build"), "--dry-run"]
    if override:
        args += ["--ak", f"version={override}"]
    assert main(args) == 0
    command = next(line for line in capsys.readouterr().err.splitlines() if line.startswith("+ "))
    versions = [part for part in shlex.split(command[2:]) if part.startswith("version=")]
    name = "claude-code" if "claude" in agent else "codex"
    assert versions == [f"version={override or DEFAULT_CLI_VERSIONS[name]}"]


def test_custom_memory_agent_path_is_passed_to_harbor(tmp_path, capsys):
    make_corpus(tmp_path / "corpus")
    code = main(["generate-memory", "--corpus", str(tmp_path / "corpus"),
                 "--harness", "your_package.openclaw:OpenClawMemoryAgent",
                 "-m", "anthropic/claude-opus-5-5", "--build-dir", str(tmp_path / "build"),
                 "--dry-run"])
    assert code == 0
    err = capsys.readouterr().err
    assert "-a your_package.openclaw:OpenClawMemoryAgent" in err
    assert "--allow-agent-host api.anthropic.com" in err
    assert "disable_web_search=true" not in err  # Custom adapters enforce their own web-tool policy.


def test_generate_memory_needs_host_for_unknown_provider(tmp_path):
    make_corpus(tmp_path / "corpus")
    code = main(["generate-memory", "--corpus", str(tmp_path / "corpus"), "--harness", "my.agent:MemoryAgent",
                 "-m", "acme/model-1", "--build-dir", str(tmp_path / "build"), "--dry-run"])
    assert code == 2


def test_build_memory_task_command_writes_the_verified_corpus(tmp_path):
    make_corpus(tmp_path / "corpus")
    out = tmp_path / "memory-task"
    assert main(["build-memory-task", "--corpus", str(tmp_path / "corpus"),
                 "--out", str(out)]) == 0
    assert (out / "task.toml").is_file()


def make_dream_trial(job: Path, memory_text: str = "# index\n") -> Path:
    trial = job / "dream-task__abc"
    (trial / "artifacts" / "memory").mkdir(parents=True)
    (trial / "artifacts" / "memory" / "MEMORY.md").write_text(memory_text)
    report = check_memory(trial / "artifacts" / "memory")
    write_json(trial / "verifier" / "memory-report.json", report)
    write_json(trial / "agent" / "trajectory.json", {"steps": []})
    write_json(trial / "result.json", {
        "task_name": "letta-ai/frontier-memory-dream", "verifier_result": {"rewards": {"reward": 1.0}},
        "config": {"agent": {"name": "claude-code", "model_name": "anthropic/claude-opus-5-5",
                             "extra_allowed_hosts": ["api.anthropic.com"]}, "timeout_multiplier": 1.0,
                   "task": {"path": str(job / "no-task")}},
    })
    return trial


def make_task_job(job: Path, memory_sha: str | None, rewards: list[float | None]) -> None:
    for i, reward in enumerate(rewards):
        write_json(job / f"task-a__{i}" / "result.json", {
            "task_name": "task-a",
            "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
            "agent_result": {"metadata": {"frontier_memory": {"sha256": memory_sha} if memory_sha else None}},
        })


def test_score_counts_invalid_apart(tmp_path):
    make_task_job(tmp_path / "job", "abc", [1.0, 0.5, None])
    score = score_jobs([tmp_path / "job"])
    assert score["tasks"]["task-a"] == {"passed": 1, "valid": 2, "invalid": 1}


def test_bundle_checks_memory_digest(tmp_path):
    trial = make_dream_trial(tmp_path / "dream")
    sha = json.loads((trial / "verifier" / "memory-report.json").read_text())["memory_sha256"]
    make_task_job(tmp_path / "act", sha, [1.0, 0.0])
    out = tmp_path / "out" / "submission.tar.gz"
    submission = build_bundle(tmp_path / "dream", [tmp_path / "act"], out)
    assert submission["problems"] == []
    assert submission["dream"]["allowed_hosts"] == ["api.anthropic.com"]
    with tarfile.open(out) as archive:
        names = archive.getnames()
    assert "SUBMISSION.json" in names and "dream/dream-task__abc/artifacts/memory/MEMORY.md" in names

    make_task_job(tmp_path / "other", "not-the-dream", [1.0])
    submission = build_bundle(tmp_path / "dream", [tmp_path / "other"], tmp_path / "out" / "bad.tar.gz")
    assert any("not the dream's" in p for p in submission["problems"])


def test_bundle_cli_accepts_memory_job_flag(tmp_path, capsys):
    trial = make_dream_trial(tmp_path / "memory-job")
    sha = json.loads((trial / "verifier" / "memory-report.json").read_text())["memory_sha256"]
    make_task_job(tmp_path / "task-job", sha, [1.0])
    archive = tmp_path / "submission.tar.gz"
    assert main(["bundle", "--memory-job", str(tmp_path / "memory-job"),
                 "--task-job", str(tmp_path / "task-job"), "--out", str(archive)]) == 0
    assert archive.is_file()
    assert json.loads(capsys.readouterr().out)["problems"] == []


@pytest.mark.parametrize("attempt_args,expected_attempts", [([], 3), (["-k", "2"], 2)])
def test_run_tasks_dry_run_allows_task_model_api_host_and_memory(tmp_path, capsys, attempt_args, expected_attempts):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("# index\n")
    code = main(["run-tasks", "--harness", "codex", "-m", "openai/gpt-6-sol", "--memory", str(memory),
                 *attempt_args, "--dry-run"])
    assert code == 0
    err = capsys.readouterr().err
    assert "-d letta-ai/frontier-memory@v1.0" in err
    assert f"-k {expected_attempts} " in err
    assert "--job-name tasks-codex-" in err
    assert "--allow-agent-host api.openai.com" in err
    assert "--ve GRADING_MODEL=openai/gpt-5.6-sol" in err
    assert f"--ak memory_dir={memory.resolve()}" in err
    assert "frontier_memory.agents.codex:MemoryCodex" in err


def test_run_tasks_accepts_a_custom_adapter_and_hosts(tmp_path, capsys):
    code = main(["run-tasks", "--harness", "my_pkg.agents:MyTaskAgent", "-m", "anthropic/claude-opus-5-5",
                 "--allow-host", "api.example.com", "--dry-run"])
    assert code == 0
    err = capsys.readouterr().err
    assert "-a my_pkg.agents:MyTaskAgent " in err
    assert "--job-name tasks-mytaskagent-" in err
    assert "--allow-agent-host api.example.com" in err
    assert "api.anthropic.com" not in err
    assert main(["run-tasks", "--harness", "no-such-agent", "-m", "openai/gpt-6-sol", "--dry-run"]) == 2
    assert "unknown harness 'no-such-agent'" in capsys.readouterr().err


def test_cma_id_precedence_and_missing_values(monkeypatch):
    for prefix in ("FRONTIER_MEMORY", "DREAMBENCH"):
        for name in ("AGENT_ID", "ENVIRONMENT_ID"):
            monkeypatch.delenv(f"{prefix}_ANTHROPIC_{name}", raising=False)
    with pytest.raises(ValueError, match="no built-in default"):
        claude_dream.resolve_managed_ids([])
    monkeypatch.setenv("DREAMBENCH_ANTHROPIC_AGENT_ID", "legacy-agent")
    monkeypatch.setenv("DREAMBENCH_ANTHROPIC_ENVIRONMENT_ID", "legacy-env")
    assert claude_dream.resolve_managed_ids([]) == ("legacy-agent", "legacy-env")
    monkeypatch.setenv("FRONTIER_MEMORY_ANTHROPIC_AGENT_ID", "frontier-agent")
    monkeypatch.setenv("FRONTIER_MEMORY_ANTHROPIC_ENVIRONMENT_ID", " ")
    assert claude_dream.resolve_managed_ids([]) == ("frontier-agent", "legacy-env")
    monkeypatch.setenv("FRONTIER_MEMORY_ANTHROPIC_ENVIRONMENT_ID", "frontier-env")
    assert claude_dream.resolve_managed_ids(["agent_id=explicit-agent"]) == ("explicit-agent", "frontier-env")
    for invalid in (["agent_id="], ["unknown=x"], ["agent_id=a", "agent_id=b"]):
        with pytest.raises(ValueError):
            claude_dream.resolve_managed_ids(invalid)


@pytest.mark.parametrize("extra", [[], ["-e", "modal"], ["-e", "docker"], ["--allow-host", "api.anthropic.com"],
                                  ["--build-dir", "build"], ["--", "--timeout-multiplier", "2"]])
def test_cma_dry_run_and_harbor_option_rejection(tmp_path, monkeypatch, capsys, extra):
    make_corpus(tmp_path / "corpus")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch("urllib.request.urlopen", side_effect=AssertionError("no API call expected")), \
         patch("frontier_memory.cli.run_harbor", side_effect=AssertionError("no Harbor run expected")):
        code = main(["generate-memory", "--corpus", str(tmp_path / "corpus"), "--harness", "claude-managed-agents",
                     "-m", "anthropic/claude-test", "--ak", "agent_id=a", "--ak", "environment_id=e",
                     "--jobs-dir", str(tmp_path / "jobs"), "--dry-run", *extra])
    captured = capsys.readouterr()
    assert not (tmp_path / "jobs").exists()
    if extra:
        assert code == 2 and "not Harbor" in captured.err
    else:
        plan = json.loads(captured.out)
        assert code == 0 and plan["api_model"] == "claude-test"
        assert plan["timeout_sec"] == claude_dream.dream_timeout_sec()


def _cma_transport(status, cancel_error):
    def respond(request, timeout):
        path, method = urlsplit(request.full_url).path, request.method
        if path == "/v1/memory_stores":
            result = {"id": "input"}
        elif method == "POST" and path.endswith("/memories"):
            result = {"id": "source"}
        elif path == "/v1/sessions":
            result = {"id": "anchor", "status": "running"}
        elif path == "/v1/sessions/anchor":
            result = {"id": "anchor", "status": "idle"}
        elif path == "/v1/dreams":
            assert json.loads(request.data)["inputs"] == [
                {"type": "memory_store", "memory_store_id": "input"},
                {"type": "sessions", "session_ids": ["anchor"]}]
            result = {"id": "dream", "status": "pending"}
        elif path == "/v1/dreams/dream":
            result = {"id": "dream", "status": status, "usage": {"output_tokens": 17},
                      "outputs": [{"type": "memory_store", "memory_store_id": "output"}]}
        elif path == "/v1/dreams/dream/cancel":
            if cancel_error:
                raise RuntimeError("cancellation unavailable")
            result = {"id": "dream", "status": "canceled"}
        elif path == "/v1/memory_stores/output/memories":
            result = {"data": [{"type": "memory", "path": "/memory/_index.md", "content": "# Native root\n"}],
                      "next_page": None}
        else:
            raise AssertionError(f"unexpected provider request: {method} {path}")
        return io.BytesIO(json.dumps(result).encode())
    return respond


@pytest.mark.parametrize("status,cancel_error", [("completed", False), ("failed", False),
                                                ("running", False), ("running", True)])
def test_cma_native_flow_export_and_timeout(tmp_path, monkeypatch, status, cancel_error):
    make_corpus(tmp_path / "corpus")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-fixture")
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += seconds

    dest = tmp_path / "native"
    with patch("urllib.request.urlopen", side_effect=_cma_transport(status, cancel_error)):
        meta = claude_dream.run_claude_managed_dream_backend(
            corpus=tmp_path / "corpus", model="anthropic/claude-test", agent_id="a", environment_id="e",
            dest=dest, timeout_sec=5, clock=lambda: elapsed[0], sleep=sleep, poll_interval_sec=1)
    assert meta["ok"] == (status == "completed")
    assert (dest / "meta.json").is_file()
    if status != "completed":
        assert not (dest / "memory").exists()
        with pytest.raises(ValueError, match="no successfully exported"):
            resolve_memory(dest)
        if status == "running":
            assert "TimeoutError" in meta["error"]
            assert meta["remote_may_be_running"] == cancel_error
            if cancel_error:
                assert "unavailable" in meta["cancellation_error"]
            else:
                assert meta["cancellation_status"] == "canceled"
        return
    assert (dest / "memory/MEMORY.md").read_text() == "# Native root\n"
    assert resolve_memory(dest) == (dest / "memory").resolve()
    for actor in ("codex", "claude-code"):
        model = "openai/test" if actor == "codex" else "anthropic/claude-test"
        assert main(["run-tasks", "--harness", actor, "-m", model, "--memory", str(dest), "--dry-run"]) == 0
    make_task_job(tmp_path / "actors", meta["memory_sha256"], [1.0])
    bundle = build_bundle(dest, [tmp_path / "actors"], tmp_path / "bundle.tar.gz")
    assert bundle["dream"]["allowed_hosts"] is None and bundle["dream"]["dream_id"] == "dream"
    assert len(bundle["problems"]) == 1 and "explicit review" in bundle["problems"][0]


def test_harness_selects_the_hermes_adapter_for_each_stage(tmp_path, capsys):
    make_corpus(tmp_path / "corpus")
    assert main(["generate-memory", "--corpus", str(tmp_path / "corpus"), "--harness", "hermes",
                 "-m", "anthropic/claude-opus-5-5", "--build-dir", str(tmp_path / "build"),
                 "--dry-run"]) == 0
    assert "-a frontier_memory.agents.hermes:HermesLearn" in capsys.readouterr().err
    assert main(["run-tasks", "--harness", "hermes", "-m", "anthropic/claude-opus-5-5",
                 "--dry-run"]) == 0
    assert "-a frontier_memory.agents.hermes:MemoryHermes" in capsys.readouterr().err


def test_a_registered_hosted_generator_is_dispatched_resolved_and_bundled(tmp_path, monkeypatch, capsys):
    calls = []
    generator = hosted.HostedGenerator(
        harness="example-hosted", backend="example-backend", runner="tests.example:run",
        evidence_type="example-api-receipts", receipt_keys=("run_id",))
    monkeypatch.setattr(hosted, "entry_points", lambda group: [SimpleNamespace(name="example", load=lambda: generator)])
    monkeypatch.setattr(hosted.HostedGenerator, "run", lambda self, args: calls.append(args.agent) or 0)
    make_corpus(tmp_path / "corpus")
    assert main(["generate-memory", "--corpus", str(tmp_path / "corpus"), "--harness", "example-hosted",
                 "-m", "example/model"]) == 0
    assert calls == ["example-hosted"]

    job = tmp_path / "jobs" / "memory-example"
    (job / "memory").mkdir(parents=True)
    (job / "memory" / "MEMORY.md").write_text("# hosted\n")
    report = check_memory(job / "memory")
    write_json(job / "meta.json", {"dream_backend": "example-backend", "ok": True, "run_id": "r-1",
                                   "corpus": {"name": "corpus", "sha256": "abc"}})
    assert resolve_memory(job) == (job / "memory").resolve()
    make_task_job(tmp_path / "tasks", report["memory_sha256"], [1.0])
    bundle = build_bundle(job, [tmp_path / "tasks"], tmp_path / "bundle.tar.gz")
    assert bundle["dream"]["evidence_type"] == "example-api-receipts" and bundle["dream"]["run_id"] == "r-1"
    assert len(bundle["problems"]) == 1 and "example-hosted ran outside Harbor" in bundle["problems"][0]

    write_json(job / "meta.json", {"dream_backend": "example-backend", "ok": False})
    with pytest.raises(ValueError, match="example-hosted job has no successfully exported memory"):
        resolve_memory(job)


def test_a_broken_hosted_generator_is_skipped_not_fatal(monkeypatch, capsys):
    def broken():
        raise ImportError("no module named example")

    monkeypatch.setattr(hosted, "entry_points", lambda group: [
        SimpleNamespace(name="broken", load=broken), SimpleNamespace(name="wrong", load=lambda: object())])
    assert set(hosted.generators()) == {"claude-managed-agents"}
    with pytest.raises(SystemExit) as stopped:
        main(["generate-memory", "--help"])
    assert stopped.value.code == 0
    err = capsys.readouterr().err
    assert "skipping hosted generator 'broken'" in err and "skipping hosted generator 'wrong'" in err
