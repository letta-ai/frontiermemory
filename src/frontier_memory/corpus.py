"""The dream corpus: one folder of trajectories, pinned by SHA256SUMS.

Layout::

    <corpus>/
      SHA256SUMS        # "<sha256>  <relative path>" for every file below
      inputs/t0001.jsonl ...   # each trajectory verbatim, as its harness wrote it

The corpus digest is the SHA-256 of SHA256SUMS. Results record it, so every
score points back to the exact corpus its dream read.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

INPUTS_DIR = "inputs"
SUMS_FILE = "SHA256SUMS"
# The released corpus: a gated Hugging Face dataset, pinned by its digest.
# RELEASE_REVISION is the dataset tag whose SHA256SUMS hashes to RELEASE_SHA256.
# Bump the two together: the dataset's main branch can move past the release.
RELEASE_REPO = "letta-ai/frontier-memory-corpus"
RELEASE_REVISION = "v1.0"
RELEASE_SHA256 = "c7d31214ed20af4f14f46c4b9a838ebc9aa35a9491dfbce0fa50f7aa456eedd1"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def corpus_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in (root / INPUTS_DIR).rglob("*")
        if path.is_file()
    )


def write_sums(root: Path) -> str:
    """Write SHA256SUMS for the corpus at ``root``; return the corpus digest."""
    lines = [f"{file_sha256(root / rel)}  {rel}\n" for rel in corpus_files(root)]
    (root / SUMS_FILE).write_text("".join(lines))
    return corpus_digest(root)


def corpus_digest(root: Path) -> str:
    return file_sha256(root / SUMS_FILE)


def verify_corpus(root: Path) -> str:
    """Check every file against SHA256SUMS, with none missing or extra; return the digest."""
    sums_path = root / SUMS_FILE
    if not sums_path.is_file():
        raise ValueError(f"{sums_path} is missing")
    expected = {}
    for line in sums_path.read_text().splitlines():
        sha, rel = line.split("  ", 1)
        expected[rel] = sha
    actual = corpus_files(root)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        raise ValueError(f"corpus files differ from {SUMS_FILE}: missing {missing[:5]}, extra {extra[:5]}")
    changed = [rel for rel in actual if file_sha256(root / rel) != expected[rel]]
    if changed:
        raise ValueError(f"corpus files changed: {changed[:5]}")
    return corpus_digest(root)


def fetch_corpus(out: Path, repo: str = RELEASE_REPO, revision: str | None = RELEASE_REVISION,
                 expected: str = RELEASE_SHA256) -> str:
    """Download the corpus from Hugging Face into ``out`` and check it; return the digest.

    The dataset is gated: accept its terms on Hugging Face, then log in
    (``hf auth login``) or set ``HF_TOKEN``.
    """
    from huggingface_hub import snapshot_download

    snapshot_download(repo, repo_type="dataset", revision=revision, local_dir=out,
                      allow_patterns=[f"{INPUTS_DIR}/*", SUMS_FILE])
    digest = verify_corpus(out)
    if digest != expected:
        raise ValueError(f"corpus digest {digest} is not the release digest {expected}")
    return digest
