"""Native Claude Managed Agents Dreams, ported from DreamBench.

Historical trajectories are uploaded to a memory store, not replayed as native
sessions. A minimal anchor session satisfies the Dreams API's session input.
Only synthesized /memory/ documents are exported for the task agent. Anthropic owns
execution; this backend makes no Harbor network-policy or full-trace claims.

Required configuration (no built-in agent or environment IDs; none are created):
- ANTHROPIC_API_KEY must grant access to the Managed Agents, Memory, and Dreams
  preview APIs. It is read only by the client, never written into run artifacts.
- agent_id selects an existing minimal Managed Agent for the ACK anchor session,
  not the dream's model (which comes from -m). Keep it free of unrelated memory.
- environment_id selects that anchor's existing cloud environment in the same
  workspace; it is not a Docker/Modal backend choice.

CLI ID precedence, independently for each ID:
1. --ak agent_id=... / --ak environment_id=...
2. FRONTIER_MEMORY_ANTHROPIC_AGENT_ID / FRONTIER_MEMORY_ANTHROPIC_ENVIRONMENT_ID
3. DREAMBENCH_ANTHROPIC_AGENT_ID / DREAMBENCH_ANTHROPIC_ENVIRONMENT_ID (legacy)
There is no fallback after (3). An explicitly empty --ak value is an error,
not permission to silently select a different agent. Blank environment values
are treated as unset. Missing IDs fail before any API call. --dry-run still
requires valid IDs and a checksummed corpus but does not require the API key.

Example (IDs must be supplied by the operator, not these placeholders):
    frontier-memory generate-memory --corpus data/corpus/joint-674 \\
        -a claude-managed-agents -m anthropic/claude-opus-5-5 \\
        --ak agent_id=agent_... --ak environment_id=env_...

Shared CLI options: --jobs-dir, --job-name, --dry-run. --ak accepts only those
two IDs for CMA. Harbor's -e/--env, --allow-host, --build-dir, and passthrough
arguments are rejected. The budget is read from dream_task/task.toml, with no
separate CMA timeout override. On expiry, cancellation is requested and its
actual provider status is recorded; failed/unconfirmed cancellation is not
reported as proof the remote job stopped.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from frontier_memory.corpus import corpus_files, verify_corpus
from frontier_memory.memory import check_memory

AGENT = "claude-managed-agents"
BACKEND = "claude-managed-dream"
API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
MEMORY_BETA = "agent-memory-2026-07-22"
DREAM_BETA = f"{MANAGED_AGENTS_BETA},dreaming-2026-04-21"
TERMINAL_DREAM_STATUSES = {"completed", "failed", "canceled"}
MAX_MEMORY_CONTENT_BYTES = 90_000

# Same joint-corpus instruction and anchor message as DreamBench's CMA runner.
CLAUDE_DREAM_INSTRUCTION = """\
This is a Frontier Memory consolidation run. The input memory store contains
historical task evidence under /evidence/. Treat those documents as immutable
source material. Study the task, prior attempts, metadata, and any included
verifier feedback, then write concise durable procedural memory for a fresh
agent under /memory/.

Balance semantic memory curation and procedural skill extraction. Preserve
durable facts, preferences, relationships, constraints, decisions, and reusable
judgment appropriate for the target. Promote a procedure to a skill only when
independent evidence shows it will recur and a reusable procedure is more
valuable than a concise memory. Do not create a skill from routine task
execution or two steps that merely happened in the same batch.

The future agent receives only /memory/, not /evidence/ or this session. Preserve
reusable methods, repository and environment facts, decision rules, validation
checks, useful commands, and failed approaches. Do not copy final answers,
submitted artifacts, literal patches, diff hunks, answer strings, or commands
that reproduce a prior final artifact. Put every synthesized output document
under /memory/; do not use any other output namespace."""
ANCHOR_MESSAGE = (
    "This session anchors a Frontier Memory dream over a multi-task source "
    "corpus. The Dream receives the historical evidence separately in its "
    "input memory store. Do not solve any task or synthesize memory here; "
    "reply only ACK."
)


def dream_timeout_sec() -> float:
    """Use the same configured agent budget as Harbor dream tasks."""
    task = resources.files("frontier_memory.dream_task").joinpath("task.toml")
    return float(tomllib.loads(task.read_text())["agent"]["timeout_sec"])


def resolve_managed_ids(kwargs: list[str]) -> tuple[str, str]:
    """Apply the documented per-ID precedence; never guess or create provider IDs."""
    explicit = {}
    for item in kwargs:
        key, separator, value = item.partition("=")
        if not separator or key not in {"agent_id", "environment_id"}:
            raise ValueError("CMA --ak accepts only agent_id=... and environment_id=...")
        if key in explicit:
            raise ValueError(f"duplicate CMA option: {key}")
        if not value.strip():
            raise ValueError(f"CMA {key} must not be empty")
        explicit[key] = value.strip()
    resolved = []
    for key in ("agent_id", "environment_id"):
        names = (f"FRONTIER_MEMORY_ANTHROPIC_{key.upper()}", f"DREAMBENCH_ANTHROPIC_{key.upper()}")
        value = explicit.get(key)
        if value is None:
            value = next((os.environ[name].strip() for name in names if os.environ.get(name, "").strip()), None)
        if value is None:
            raise ValueError(f"CMA requires {key}: pass --ak {key}=... or set {names[0]} "
                             f"(legacy fallback: {names[1]}); no built-in default")
        resolved.append(value)
    return resolved[0], resolved[1]


class ClaudeAPIError(RuntimeError):
    """A failed or malformed Anthropic API response."""


class ClaudeDreamClient:
    def __init__(self, api_key: str | None = None, api_base: str = API_BASE,
                 timeout_sec: float = 120, deadline: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for claude-managed-agents")
        self.api_base = api_base.rstrip("/")
        self.timeout_sec, self.deadline, self.clock = timeout_sec, deadline, clock

    def request(self, method: str, path: str, *, beta: str,
                body: dict[str, Any] | None = None, cancellation: bool = False) -> dict[str, Any]:
        timeout = self.timeout_sec
        if cancellation:
            timeout = min(timeout, 30)
        elif self.deadline is not None:
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("Claude managed dream deadline exceeded")
            timeout = min(timeout, remaining)
        request = urllib.request.Request(
            self.api_base + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION,
                     "anthropic-beta": beta, "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", {})
            except (ValueError, AttributeError):
                detail = {}
            message = detail.get("message") if isinstance(detail, dict) else None
            raise ClaudeAPIError(f"Anthropic API {method} {path} returned HTTP {exc.code}"
                                 + (f": {message}" if message else "")) from exc
        except urllib.error.URLError as exc:
            raise ClaudeAPIError(f"Anthropic API {method} {path} failed: {exc.reason}") from exc
        try:
            result = json.loads(payload)
        except ValueError as exc:
            raise ClaudeAPIError("Anthropic API returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ClaudeAPIError("Anthropic API returned a non-object")
        return result

    def create_memory_store(self, *, name: str, metadata: dict[str, str]) -> dict[str, Any]:
        return self.request("POST", "/v1/memory_stores", beta=MEMORY_BETA, body={
            "name": name[:255], "metadata": metadata,
            "description": "Frontier Memory source evidence for a Claude managed dream.",
        })

    def create_memory(self, store_id: str, *, path: str, content: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/memory_stores/{urllib.parse.quote(store_id, safe='')}/memories",
                            beta=MEMORY_BETA, body={"path": path, "content": content})

    def create_anchor_session(self, *, agent_id: str, environment_id: str, title: str,
                              message: str) -> dict[str, Any]:
        return self.request("POST", "/v1/sessions", beta=MANAGED_AGENTS_BETA, body={
            "agent": agent_id, "environment_id": environment_id, "title": title,
            "initial_events": [{"type": "user.message", "content": [{"type": "text", "text": message}]}],
        })

    def retrieve_session(self, session_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                            beta=MANAGED_AGENTS_BETA)

    def create_dream(self, *, store_id: str, session_id: str, model: str,
                     instructions: str) -> dict[str, Any]:
        return self.request("POST", "/v1/dreams", beta=DREAM_BETA, body={
            "inputs": [{"type": "memory_store", "memory_store_id": store_id},
                       {"type": "sessions", "session_ids": [session_id]}],
            "model": model, "instructions": instructions, "output_behavior": {"type": "create_new"},
        })

    def retrieve_dream(self, dream_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/dreams/{urllib.parse.quote(dream_id, safe='')}", beta=DREAM_BETA)

    def cancel_dream(self, dream_id: str) -> dict[str, Any]:
        # Provider SDK route: POST /v1/dreams/{dream_id}/cancel?beta=true.
        return self.request("POST", f"/v1/dreams/{urllib.parse.quote(dream_id, safe='')}/cancel?beta=true",
                            beta=DREAM_BETA, cancellation=True)

    def list_memories(self, store_id: str, *, path_prefix: str, view: str = "full") -> list[dict[str, Any]]:
        items, seen = [], set()
        page = None
        while True:
            query = {"path_prefix": path_prefix, "view": view, "limit": "20"}
            if page:
                query["page"] = page
            result = self.request("GET", f"/v1/memory_stores/{urllib.parse.quote(store_id, safe='')}/memories?"
                                  + urllib.parse.urlencode(query), beta=MEMORY_BETA)
            data = result.get("data", [])
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise ClaudeAPIError("memory list response has invalid data")
            items.extend(data)
            page = result.get("next_page")
            if page in (None, ""):
                return items
            if not isinstance(page, str) or not page or page in seen:
                raise ClaudeAPIError("memory list returned an invalid or repeated page cursor")
            seen.add(page)


def _chunks_by_utf8_bytes(text: str, max_bytes: int = MAX_MEMORY_CONTENT_BYTES) -> list[str]:
    """Split without changing content or breaking UTF-8 code points."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    chunks, current, current_bytes = [], [], 0
    for char in text:
        size = len(char.encode("utf-8"))
        if size > max_bytes:
            raise ValueError("max_bytes is too small for one UTF-8 code point")
        if current and current_bytes + size > max_bytes:
            chunks.append("".join(current))
            current, current_bytes = [], 0
        current.append(char)
        current_bytes += size
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


def upload_corpus(client: ClaudeDreamClient, store_id: str, corpus: Path, *,
                  max_bytes: int = MAX_MEMORY_CONTENT_BYTES) -> dict[str, Any]:
    digest = verify_corpus(corpus)
    files = corpus_files(corpus)
    if not files:
        raise ValueError("the corpus has no trajectories")
    manifest: dict[str, Any] = {"schema_version": 1, "transport": "anthropic-memory-store",
                               "corpus_sha256": digest, "count": len(files), "files": []}
    for name in files:
        source = corpus / name
        if source.is_symlink():
            raise ValueError(f"corpus source must not be a symlink: {name}")
        data = source.read_bytes()
        chunks = _chunks_by_utf8_bytes(data.decode("utf-8"), max_bytes)
        paths = []
        for index, chunk in enumerate(chunks):
            path = f"/evidence/{name}" if len(chunks) == 1 else f"/evidence/{name}.parts/{index:05d}.txt"
            client.create_memory(store_id, path=path, content=chunk)
            paths.append(path)
        manifest["files"].append({"path": name, "content_bytes": len(data),
                                  "content_sha256": hashlib.sha256(data).hexdigest(), "chunks": paths})
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True)
    chunks = _chunks_by_utf8_bytes(manifest_text, max_bytes)
    for index, chunk in enumerate(chunks):
        suffix = "manifest.json" if len(chunks) == 1 else f"manifest.parts/{index:05d}.json"
        client.create_memory(store_id, path=f"/evidence/{suffix}", content=chunk)
    manifest["manifest_sha256"] = hashlib.sha256(manifest_text.encode()).hexdigest()
    manifest["memory_documents"] = sum(len(file["chunks"]) for file in manifest["files"]) + len(chunks)
    return manifest


def _safe_output_path(memory_path: str) -> Path:
    prefix = "/memory/"
    if not memory_path.startswith(prefix):
        raise ValueError(f"dream output path is outside {prefix}: {memory_path!r}")
    parts = memory_path[len(prefix):].split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"invalid dream output path: {memory_path!r}")
    return Path(*parts)


def export_synthesized_memory(client: ClaudeDreamClient, output_store_id: str,
                              memory_dir: Path) -> dict[str, Any]:
    """Export transactionally: failed or invalid exports never become actor memory."""
    if memory_dir.exists() or memory_dir.is_symlink():
        raise ValueError("CMA export destination already exists")
    memories = client.list_memories(output_store_id, path_prefix="/memory/", view="full")
    paths = {item.get("path") for item in memories if item.get("type") == "memory"}
    use_index = "/memory/MEMORY.md" not in paths
    with tempfile.TemporaryDirectory(prefix="cma-export-", dir=memory_dir.parent) as directory:
        staging = Path(directory) / "memory"
        staging.mkdir()
        seen = set()
        for item in memories:
            if item.get("type") != "memory":
                continue
            path, content = item.get("path"), item.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                raise ClaudeAPIError("full memory export omitted path or content")
            relative = _safe_output_path(path)
            if use_index and relative.as_posix() == "_index.md":
                relative = Path("MEMORY.md")
            if relative in seen:
                raise ClaudeAPIError(f"duplicate dream output path: {relative}")
            seen.add(relative)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        report = check_memory(staging)
        if not report["ok"]:
            raise ValueError("CMA memory failed validation: " + "; ".join(report["errors"]))
        staging.rename(memory_dir)
    return report


def _wait(current, *, retrieve, terminal, active, deadline, clock, sleep, poll_interval_sec, on_poll):
    while current.get("status") not in terminal:
        if current.get("status") not in active:
            raise ClaudeAPIError(f"unexpected provider status: {current.get('status')!r}")
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("Claude managed dream deadline exceeded")
        sleep(min(poll_interval_sec, remaining))
        if clock() >= deadline:
            raise TimeoutError("Claude managed dream deadline exceeded")
        current = retrieve(str(current["id"]))
        on_poll(current)
    return current


def run_claude_managed_dream_backend(*, corpus: Path, model: str, agent_id: str,
                                    environment_id: str, dest: Path,
                                    client: ClaudeDreamClient | None = None,
                                    timeout_sec: float | None = None,
                                    clock: Callable[[], float] = time.monotonic,
                                    sleep: Callable[[float], None] = time.sleep,
                                    poll_interval_sec: float = 10.0) -> dict[str, Any]:
    """Same native API sequence as DreamBench, with Frontier Memory's corpus and budget."""
    if not agent_id or not environment_id:
        raise ValueError("CMA requires agent_id and environment_id")
    api_model = model.removeprefix("anthropic/")
    if not api_model or "/" in api_model:
        raise ValueError("CMA requires an Anthropic model, optionally prefixed anthropic/")
    budget = dream_timeout_sec() if timeout_sec is None else timeout_sec
    if budget <= 0 or poll_interval_sec <= 0:
        raise ValueError("timeout and polling interval must be positive")
    digest = verify_corpus(corpus)
    if not corpus_files(corpus):
        raise ValueError("the corpus has no trajectories")
    dest.mkdir(parents=True, exist_ok=False)
    start = clock()
    deadline = start + budget
    dream = session = None
    meta: dict[str, Any] = {"dream_backend": BACKEND, "memory_agent": AGENT,
        "dreamer_model": model, "api_model": api_model, "timeout_sec": budget,
        "corpus": {"name": corpus.name, "sha256": digest}, "ok": False,
        "started_at": datetime.now(UTC).isoformat(), "remote_may_be_running": False}

    def log_event(event, value):
        with (dest / "dream.log").open("a") as handle:
            handle.write(json.dumps({"event": event, "value": value}, default=str) + "\n")

    def remaining():
        if clock() >= deadline:
            raise TimeoutError("Claude managed dream deadline exceeded")

    def poll_event(value):
        if value.get("usage") is not None:
            meta["usage"] = value["usage"]
        log_event("remote_status", value)

    try:
        client = client or ClaudeDreamClient(deadline=deadline, clock=clock)
        log_event("instructions", {"dream": CLAUDE_DREAM_INSTRUCTION, "anchor": ANCHOR_MESSAGE})
        store = client.create_memory_store(name=f"frontier-memory-{dest.name}",
                                           metadata={"benchmark": "frontier-memory", "run": dest.name[:512]})
        meta["input_memory_store_id"] = store["id"]
        log_event("input_memory_store_created", store["id"])
        manifest = upload_corpus(client, str(store["id"]), corpus)
        meta["evidence_manifest"], meta["n_trajectories"] = manifest, manifest["count"]
        log_event("evidence_uploaded", manifest)
        remaining()
        meta["remote_may_be_running"] = True
        session = client.create_anchor_session(agent_id=agent_id, environment_id=environment_id,
            title=f"Frontier Memory dream anchor: {dest.name}", message=ANCHOR_MESSAGE)
        meta["anchor_session_id"] = session["id"]
        log_event("anchor_session", session)
        session = _wait(session, retrieve=client.retrieve_session,
            terminal={"idle", "completed", "failed", "canceled", "terminated", "errored"},
            active={"running", "pending", "queued"}, deadline=deadline, clock=clock,
            sleep=sleep, poll_interval_sec=poll_interval_sec, on_poll=poll_event)
        if session.get("status") not in {"idle", "completed"}:
            raise ClaudeAPIError(f"anchor session ended with status {session.get('status')!r}")
        meta["remote_may_be_running"] = False
        remaining()
        meta["remote_may_be_running"] = True
        dream = client.create_dream(store_id=str(store["id"]), session_id=str(session["id"]),
            model=api_model, instructions=CLAUDE_DREAM_INSTRUCTION)
        meta["dream_id"] = dream["id"]
        log_event("dream_created", dream)
        dream = _wait(dream, retrieve=client.retrieve_dream, terminal=TERMINAL_DREAM_STATUSES,
            active={"pending", "running", "queued"}, deadline=deadline, clock=clock,
            sleep=sleep, poll_interval_sec=poll_interval_sec, on_poll=poll_event)
        meta["remote_may_be_running"] = False
        if dream.get("status") != "completed":
            raise ClaudeAPIError(f"Claude dream ended with status {dream.get('status')!r}: {dream.get('error')}")
        output = next((item for item in dream.get("outputs", [])
                       if item.get("type") == "memory_store"), None)
        if not output or not isinstance(output.get("memory_store_id"), str):
            raise ClaudeAPIError("completed Claude dream has no output memory store")
        meta["output_memory_store_id"] = output["memory_store_id"]
        remaining()
        report = export_synthesized_memory(client, output["memory_store_id"], dest / "memory")
        (dest / "memory-report.json").write_text(json.dumps(report, indent=2))
        meta.update(ok=True, memory_files=[item["path"] for item in report["files"]],
                    memory_bytes=report["total_bytes"], memory_sha256=report["memory_sha256"])
    except (Exception, KeyboardInterrupt) as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
        log_event("error", meta["error"])
        if dream and dream.get("status") not in TERMINAL_DREAM_STATUSES:
            meta["remote_may_be_running"] = True
            try:
                cancellation = client.cancel_dream(str(dream["id"]))
                meta["cancellation_status"] = cancellation.get("status", "requested")
                meta["remote_may_be_running"] = cancellation.get("status") not in TERMINAL_DREAM_STATUSES
                log_event("cancellation", cancellation)
                dream = cancellation
            except Exception as cancellation_error:
                meta["cancellation_error"] = str(cancellation_error)
                log_event("cancellation_error", str(cancellation_error))
        elif not dream and session and session.get("status") == "running":
            meta["remote_may_be_running"] = True
    finally:
        meta["dream_status"] = dream.get("status") if dream else None
        meta["usage"] = (dream.get("usage") if dream else None) or meta.get("usage")
        meta["elapsed_sec"] = round(clock() - start, 2)
        meta["finished_at"] = datetime.now(UTC).isoformat()
        (dest / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta
