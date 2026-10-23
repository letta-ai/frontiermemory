"""Memory generators that run outside Harbor, and the job folders they write.

A hosted generator runs on its provider instead of in the Harbor memory-generation task.
It writes ``jobs/<job>/`` with ``meta.json`` (``dream_backend`` names the generator),
``memory/`` and ``memory-report.json``. ``resolve_memory``, ``dream_trial_dir`` and
``bundle`` accept the job folder of any registered generator.

Claude Managed Agents is built in. Another package registers a generator through the
``frontier_memory.hosted_generators`` entry point group; each entry point loads a
``HostedGenerator``.
"""

from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

ENTRY_POINT_GROUP = "frontier_memory.hosted_generators"


@dataclass(frozen=True)
class HostedGenerator:
    harness: str                       # generate-memory --harness name
    backend: str                       # meta.json "dream_backend"
    runner: str                        # "module:function" taking the generate-memory arguments
    evidence_type: str                 # what the job keeps instead of a Harbor trace
    receipt_keys: tuple[str, ...] = ()  # provider IDs from meta.json to copy into a bundle

    def run(self, args: argparse.Namespace) -> int:
        module, _, name = self.runner.partition(":")
        run: Callable[[argparse.Namespace], int] = getattr(importlib.import_module(module), name)
        return run(args)


BUILT_IN = (
    HostedGenerator(
        harness="claude-managed-agents", backend="claude-managed-dream",
        runner="frontier_memory.cli:cmd_cma_generate_memory",
        evidence_type="anthropic-api-receipts",
        receipt_keys=("dream_id", "anchor_session_id", "input_memory_store_id", "output_memory_store_id"),
    ),
)


def generators() -> dict[str, HostedGenerator]:
    """Registered generators by harness name: built-ins, then installed entry points."""
    found = {g.harness: g for g in BUILT_IN}
    for point in entry_points(group=ENTRY_POINT_GROUP):
        generator = point.load()
        if not isinstance(generator, HostedGenerator):
            raise TypeError(f"entry point {point.name} is not a HostedGenerator")
        found.setdefault(generator.harness, generator)
    return found


def hosted_job(path: Path) -> tuple[dict[str, Any], HostedGenerator] | None:
    """The meta.json and generator of a hosted generator's job folder, or None."""
    file = path / "meta.json"
    if not file.is_file():
        return None
    meta = json.loads(file.read_text())
    if not isinstance(meta, dict):
        return None
    by_backend = {g.backend: g for g in generators().values()}
    generator = by_backend.get(meta.get("dream_backend"))
    return (meta, generator) if generator is not None else None
