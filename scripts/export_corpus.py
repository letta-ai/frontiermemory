"""Export a prepared FrontierMemory corpus into the release layout, scanning for secrets.

The source is a folder from the private repo's ``scripts/prepare_dream_inputs.py``
(``native/``, ``normalized/``, ``input-manifest.json``). Only ``native/`` is
copied, as ``inputs/``: it is the view our memory agents read. ``input-manifest.json`` maps each opaque name to its source task, so it
stays out of the release.

The export fails if any file holds something that looks like a credential.
``--redact`` replaces each match with ``[REDACTED]`` instead and lists the files
it changed. Redaction changes the corpus digest.

    python scripts/export_corpus.py <private dream inputs>/joint-674-2026-10-17 data/corpus/joint-674
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from frontier_memory.corpus import INPUTS_DIR, write_sums  # noqa: E402

SECRET_PATTERNS = {
    "openrouter": r"sk-or-v1-[0-9a-f]{32,}",
    "anthropic": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai": r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}",
    "github": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "huggingface": r"hf_[A-Za-z0-9]{30,}",
    "slack": r"xox[abposr]-[A-Za-z0-9-]{20,}",
    "aws_access_key": r"AKIA[0-9A-Z]{16}",
    "letta": r"sk-let-[A-Za-z0-9_-]{20,}",
}
# A credential never starts mid-word ("risk-with-..." is not an OpenAI key), but
# it can follow an escaped newline or tab inside a JSON string ("\nsk-...").
SECRET_RE = re.compile(
    "|".join(
        rf"(?:(?<![A-Za-z0-9_-])|(?<=\\[nrt]))(?P<{name}>{pattern})"
        for name, pattern in SECRET_PATTERNS.items()
    )
)


def scan(text: str) -> list[str]:
    return sorted({match.lastgroup for match in SECRET_RE.finditer(text)})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--redact", action="store_true", help="replace credentials instead of failing")
    args = parser.parse_args()

    if args.out.exists():
        shutil.rmtree(args.out)
    hits: dict[str, list[str]] = {}
    for path in sorted((args.source / "native").rglob("*")):
        if not path.is_file():
            continue
        rel = f"{INPUTS_DIR}/{path.relative_to(args.source / 'native').as_posix()}"
        text = path.read_text(encoding="utf-8")
        kinds = scan(text)
        if kinds:
            hits[rel] = kinds
            if args.redact:
                text = SECRET_RE.sub("[REDACTED]", text)
        target = args.out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    if hits and not args.redact:
        shutil.rmtree(args.out)
        print(json.dumps({"secrets_found": hits}, indent=2))
        print("refusing to export; rerun with --redact after rotating the keys", file=sys.stderr)
        return 1
    digest = write_sums(args.out)
    print(json.dumps({"out": str(args.out), "corpus_sha256": digest, "redacted": hits}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
