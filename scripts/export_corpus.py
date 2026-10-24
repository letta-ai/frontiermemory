"""Export a prepared FrontierMemory corpus into the release layout, scanning for secrets.

The source is a folder from the private repo's ``scripts/prepare_dream_inputs.py``
(``native/``, ``normalized/``, ``input-manifest.json``). Only ``native/`` is
copied, as ``inputs/``: it is the view our memory agents read. ``input-manifest.json`` maps each opaque name to its source task, so it
stays out of the release.

The export fails if any file holds something that looks like a credential.
``--redact`` replaces each match with ``[REDACTED]`` instead and lists the files
it changed. Redaction changes the corpus digest.

The export also removes the labels added while the corpus was built, so a
memory agent cannot read them in place of the trajectories:

- the SWE-Interact preference, stage and curation fields;
- ``trajectory_id``;
- any ``session_id`` that holds a trial name, which becomes the file's opaque
  name because ATIF requires the field.

Only files holding a label change, in the corpus's own ATIF layout. Every other
file is copied unchanged. The export fails if two files are identical.

    python scripts/export_corpus.py <private prepared corpus> data/corpus/joint-674 --redact
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


STEP_LABELS = frozenset({
    "swe_interact_stage",
    "derived_from_mcp_user_server",
    "controlled_preference_continuation",
    "controlled_customer_context",
    "controlled_synthetic_augmentation",
})
FINAL_METRICS_LABELS = frozenset({
    "swe_interact_stage_count",
    "derived_user_turn_count",
    "reward",
    "controlled_synthetic_augmentation",
})
# The note on merged SWE-Interact trajectories; conversion notes stay.
MERGE_NOTE_PREFIX = "Five SWE-Interact Harbor st"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def scan(text: str) -> list[str]:
    return sorted({match.lastgroup for match in SECRET_RE.finditer(text)})


def strip_labels(doc: dict, name: str) -> bool:
    """Remove corpus-building labels from one ATIF document in place; return whether any were found."""
    found = False
    # Only SWE-Interact trajectories have a top-level extra, and all of it is labels.
    for key in ("extra", "trajectory_id"):
        if key in doc:
            del doc[key]
            found = True
    if "session_id" in doc and not UUID_RE.fullmatch(str(doc["session_id"])):
        doc["session_id"] = name
        found = True
    if str(doc.get("notes", "")).startswith(MERGE_NOTE_PREFIX):
        del doc["notes"]
        found = True
    metrics = doc.get("final_metrics") or {}
    for key in FINAL_METRICS_LABELS & metrics.keys():
        del metrics[key]
        found = True
    for step in doc.get("steps", []):
        extra = step.get("extra")
        if not extra:
            continue
        for key in STEP_LABELS & extra.keys():
            del extra[key]
            found = True
        if not extra:
            del step["extra"]
    return found


def unlabelled(text: str, rel: str) -> str | None:
    """The file's text without labels, or None if it holds none."""
    if not rel.endswith(".json"):
        return None
    doc = json.loads(text)
    if not isinstance(doc, dict) or "steps" not in doc or not strip_labels(doc, Path(rel).stem):
        return None
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--redact", action="store_true", help="replace credentials instead of failing")
    args = parser.parse_args()

    if args.out.exists():
        shutil.rmtree(args.out)
    hits: dict[str, list[str]] = {}
    stripped: list[str] = []
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
        clean = unlabelled(text, rel)
        if clean is not None:
            text = clean
            stripped.append(rel)
        target = args.out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    if hits and not args.redact:
        shutil.rmtree(args.out)
        print(json.dumps({"secrets_found": hits}, indent=2))
        print("refusing to export; rerun with --redact after rotating the keys", file=sys.stderr)
        return 1
    digest = write_sums(args.out)
    by_sha: dict[str, list[str]] = {}
    for line in (args.out / "SHA256SUMS").read_text().splitlines():
        sha, rel = line.split("  ", 1)
        by_sha.setdefault(sha, []).append(rel)
    copies = [rels for rels in by_sha.values() if len(rels) > 1]
    if copies:
        shutil.rmtree(args.out)
        print(json.dumps({"duplicate_files": copies}, indent=2))
        print("refusing to export; remove the extra copies from the source corpus", file=sys.stderr)
        return 1
    print(json.dumps({"out": str(args.out), "corpus_sha256": digest, "redacted": hits,
                      "stripped": len(stripped)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
