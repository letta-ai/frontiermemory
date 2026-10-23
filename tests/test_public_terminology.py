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
# Task instructions and source-task transformations are checksum-bearing release
# inputs. Check editable public copy without changing the v1.0 task materialization.
# The task exporter separately matches old source-task text to remove it before
# publication. Do not reject those input-only patterns as release copy.
STALE_COPY = re.compile(
    r"\bdreambench(?:[-_ ]eval)?\b|\bdreamer\b|\bdream[ -]agent\b|"
    r"\bdream model\b|\bactor\b|\bact\b|\bcma\b", re.IGNORECASE
)


@pytest.mark.parametrize("name", PUBLIC_TEXT)
def test_published_text_uses_release_names(name: str) -> None:
    text = (ROOT / name).read_text()
    match = STALE_COPY.search(text)
    assert match is None, f"{name} still publishes {match.group(0)!r}" if match else name


@pytest.mark.parametrize("name", BRANDED_COPY)
def test_public_brand_is_one_word(name: str) -> None:
    assert not re.search(r"\bfrontier memory\b", (ROOT / name).read_text(), re.IGNORECASE), name


@pytest.mark.parametrize(
    "name",
    (
        "README.md",
        "docs/rules.md",
        "docs/submitting-a-harness.md",
        ".github/PULL_REQUEST_TEMPLATE/harness.md",
    ),
)
def test_submission_copy_requires_three_candidate_runs_before_official_verification(name: str) -> None:
    text = (ROOT / name).read_text()
    assert re.search(r"\bthree\s+independent\b", text, re.IGNORECASE), name
    assert "Letta independently verifies" in text, name
    assert not re.search(r"Letta\s+runs\s+three\s+(?:independent\s+)?official", text), name


@pytest.mark.parametrize(
    "name", ("README.md", "docs/submitting-a-harness.md", ".github/PULL_REQUEST_TEMPLATE/harness.md")
)
def test_smoke_is_optional_local_testing_not_submission_evidence(name: str) -> None:
    text = (ROOT / name).read_text()
    assert "smoke" in text.lower() and "optional" in text.lower(), name
    assert not re.search(r"\battach(?:ed)?\s+(?:the\s+)?(?:\[)?smoke", text, re.IGNORECASE), name


def test_cli_help_uses_release_names(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    for command in ("generate-memory", "build-memory-task", "run-tasks", "score", "bundle"):
        with pytest.raises(SystemExit) as stopped:
            parser.parse_args([command, "--help"])
        assert stopped.value.code == 0
        help_text = capsys.readouterr().out
        match = STALE_COPY.search(help_text)
        assert match is None, f"{command} help still publishes {match.group(0)!r}" if match else command
        assert not re.search(r"\bdream\b", help_text, re.IGNORECASE), command


@pytest.mark.parametrize("command", ("act", "dream", "build-dream-task"))
def test_old_stage_commands_are_not_public_options(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args([command])
    assert stopped.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_old_bundle_flag_is_not_public(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(["bundle", "--dream", "jobs/old",
                                   "--task-job", "jobs/task", "--out", "submission.tar.gz"])
    assert stopped.value.code == 2
    assert "--memory-job" in capsys.readouterr().err
