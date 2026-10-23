"""Keep released copy and CLI help on public FrontierMemory names."""

import re
from pathlib import Path

import pytest

from frontier_memory.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TEXT = (
    "README.md",
    "pyproject.toml",
    "docs/rules.md",
    "docs/maintainers.md",
    "release/hub-readme.md",
    "release/corpus-card.md",
    "src/frontier_memory/dream_task/instruction.md",
)
BRANDED_COPY = (
    "README.md",
    "pyproject.toml",
    "docs/maintainers.md",
    "release/hub-readme.md",
    "release/corpus-card.md",
    "scripts/export_corpus.py",
    "src/frontier_memory/sdk_dream.py",
)
# The old project name must not leak into released copy. Task instructions and
# source-task transformations are checksum-bearing release inputs; the task exporter
# removes the old name from those separately.
STALE_COPY = re.compile(r"\bdreambench(?:[-_ ]eval)?\b", re.IGNORECASE)


@pytest.mark.parametrize("name", PUBLIC_TEXT)
def test_published_text_uses_release_names(name: str) -> None:
    text = (ROOT / name).read_text()
    match = STALE_COPY.search(text)
    assert match is None, f"{name} still publishes {match.group(0)!r}" if match else name


@pytest.mark.parametrize("name", BRANDED_COPY)
def test_public_brand_is_one_word(name: str) -> None:
    assert not re.search(r"\bfrontier memory\b", (ROOT / name).read_text(), re.IGNORECASE), name


def test_cli_help_uses_release_names(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    for command in ("generate-memory", "build-memory-task", "run-tasks", "score", "bundle"):
        with pytest.raises(SystemExit) as stopped:
            parser.parse_args([command, "--help"])
        assert stopped.value.code == 0
        help_text = capsys.readouterr().out
        match = STALE_COPY.search(help_text)
        assert match is None, f"{command} help still publishes {match.group(0)!r}" if match else command
