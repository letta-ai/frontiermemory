"""The SDK driver keeps a consolidated memory when some phase-one rollouts failed."""

import json

from frontier_memory import sdk_dream


def test_phase_one_errors_keep_the_consolidated_memory(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name in ("t0001.json", "t0002.jsonl"):
        (inputs / name).write_text("{}\n")
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Learn from these trajectories.")

    async def generate_memory(*, workspace, **_):
        memories = workspace / "memories"
        (memories / "raw_memories").mkdir(parents=True)
        (memories / "raw_memories" / "t0001.md").write_text("raw memory")
        (memories / "MEMORY.md").write_text("# Memory\n")
        (memories / "memory_summary.md").write_text("Summary\n")
        return ["Sandbox memory worker failed"]

    monkeypatch.setattr(sdk_dream, "generate_memory", generate_memory)
    logs = tmp_path / "logs"
    code = sdk_dream.main(["--model", "openai/gpt-6.1-sol", "--inputs", str(inputs),
                           "--output", str(tmp_path / "memory"), "--logs", str(logs),
                           "--instruction", str(instruction)])

    result = json.loads((logs / "sdk-result.json").read_text())
    assert code == 0 and result["ok"]
    assert result["memory_files"] == ["MEMORY.md", "memory_summary.md"]
    assert result["logged_errors"] == ["Sandbox memory worker failed"]
    assert result["sources_without_phase_one_memory"] == ["t0002"]
