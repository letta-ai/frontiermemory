"""Harbor task agents that load a dream's memory natively, plus a network probe for tests."""

from __future__ import annotations

from typing import Any

# Runtime recipes at DreamBench PR #413's e64eaee0 revision.
# Codex: src/dreambench/act.py; Claude Code: src/dreambench/apex_runtime.py.
DEFAULT_CLI_VERSIONS = {"codex": "0.160.1", "claude-code": "2.1.284"}


def memory_metadata(report: dict[str, Any] | None) -> dict[str, Any]:
    """What a trial records about its memory: none, or its digest and files."""
    if report is None:
        return {"frontier_memory": None}
    return {
        "frontier_memory": {
            "sha256": report["memory_sha256"],
            "total_bytes": report["total_bytes"],
            "files": [entry["path"] for entry in report["files"]],
        }
    }
