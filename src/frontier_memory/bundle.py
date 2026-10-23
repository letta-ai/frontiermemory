"""Pack a dream run and its task-agent jobs into one submission archive.

The archive holds the full Harbor folders, so a reviewer can read the memory agent's
trace, the memory it wrote, and every task-agent trial. ``SUBMISSION.json`` at the
top sums up what the folders show and the checks this tool ran on them.
"""

from __future__ import annotations

import json
import tarfile
import tomllib
from pathlib import Path
from typing import Any

from frontier_memory.claude_dream import native_dream_meta
from frontier_memory.memory import check_memory
from frontier_memory.score import score_jobs, trial_dirs


def dream_trial_dir(path: Path) -> Path:
    """Accept a native CMA job, a Harbor trial, or a Harbor job with one trial."""
    if native_dream_meta(path) is not None:
        return path
    if (path / "artifacts" / "memory").is_dir():
        return path
    trials = [t for t in trial_dirs(path) if (t / "artifacts" / "memory").is_dir()]
    if len(trials) != 1:
        raise ValueError(f"expected one dream trial with artifacts/memory under {path}, found {len(trials)}")
    return trials[0]


def dream_summary(trial: Path) -> dict[str, Any]:
    native = native_dream_meta(trial)
    if native is not None:
        report = check_memory(trial / "memory")
        return {
            "trial": trial.name, "memory_agent": native.get("memory_agent"),
            "memory_model": native.get("dreamer_model"), "memory_agent_kwargs": {},
            "allowed_hosts": None, "timeout_multiplier": None, "timeout_sec": native.get("timeout_sec"),
            "corpus_name": (native.get("corpus") or {}).get("name"),
            "corpus_sha256": (native.get("corpus") or {}).get("sha256"),
            "memory_ok": bool(native.get("ok") and report["ok"]),
            "memory_sha256": report.get("memory_sha256"), "memory_bytes": report.get("total_bytes"),
            "has_trajectory": False, "exception": native.get("error"),
            "evidence_type": "anthropic-api-receipts", "dream_id": native.get("dream_id"),
            "anchor_session_id": native.get("anchor_session_id"),
            "input_memory_store_id": native.get("input_memory_store_id"),
            "output_memory_store_id": native.get("output_memory_store_id"),
        }
    result = json.loads((trial / "result.json").read_text())
    report_path = trial / "verifier" / "memory-report.json"
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    agent = (result.get("config") or {}).get("agent") or {}
    task_path = Path(((result.get("config") or {}).get("task") or {}).get("path") or "")
    corpus = {}
    if (task_path / "task.toml").is_file():
        corpus = tomllib.loads((task_path / "task.toml").read_text()).get("metadata", {})
    return {
        "trial": trial.name,
        "memory_agent": agent.get("name") or agent.get("import_path"),
        "memory_model": agent.get("model_name"),
        "memory_agent_kwargs": agent.get("kwargs") or {},
        "allowed_hosts": agent.get("extra_allowed_hosts") or [],
        "timeout_multiplier": (result.get("config") or {}).get("timeout_multiplier"),
        "corpus_name": corpus.get("corpus_name"),
        "corpus_sha256": corpus.get("corpus_sha256"),
        "memory_ok": report.get("ok"),
        "memory_sha256": report.get("memory_sha256"),
        "memory_bytes": report.get("total_bytes"),
        "has_trajectory": (trial / "agent" / "trajectory.json").is_file(),
        "exception": (result.get("exception_info") or {}).get("exception_type"),
    }


def build_bundle(dream: Path, task_jobs: list[Path], out: Path, note: str = "") -> dict[str, Any]:
    missing = [str(job) for job in task_jobs if not job.is_dir()]
    if missing:
        raise ValueError(f"task-agent job folders not found: {missing}")
    trial = dream_trial_dir(dream)
    summary_dream = dream_summary(trial)
    score = score_jobs(task_jobs)
    problems = []
    if not summary_dream["memory_ok"]:
        problems.append("the dream's memory failed the memory check")
    if summary_dream.get("evidence_type") == "anthropic-api-receipts":
        problems.append("CMA has native API receipts, not a full Harbor agent trace or Harbor-enforced network policy; "
                        "submission compliance requires explicit review")
    elif not summary_dream["has_trajectory"]:
        problems.append("the dream trial has no agent/trajectory.json; include the memory agent's trace")
    used = set(score["memory_sha256"])
    if used != {summary_dream["memory_sha256"]}:
        problems.append(f"task-agent trials used memory {sorted(used)}, not the dream's {summary_dream['memory_sha256']}")
    if score["invalid"]:
        problems.append(f"{score['invalid']} task-agent trials have no reward; rerun them")

    submission = {
        "note": note,
        "dream": summary_dream,
        "task_runs": {k: score[k] for k in ("jobs", "passed", "valid", "invalid", "tasks")},
        "problems": problems,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    summary_path = out.with_suffix("").with_suffix(".json")
    summary_path.write_text(json.dumps(submission, indent=2) + "\n")
    with tarfile.open(out, "w:gz") as archive:
        archive.add(summary_path, arcname="SUBMISSION.json")
        archive.add(trial, arcname=f"dream/{trial.name}")
        for job in task_jobs:
            archive.add(job, arcname=f"task-runs/{job.name}")
    return submission
