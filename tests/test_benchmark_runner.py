from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.harness.benchmark import run_benchmark_suite


def _write_suite(path: Path) -> Path:
    suite = {
        "suite": "deterministic-smoke",
        "scenarios": [
            {
                "id": "passes",
                "turns": [{"user": "ping"}],
                "expect": {"final": "pong"},
            },
            {
                "id": "fails",
                "turns": [{"user": "ping"}],
                "expect": {"final": "pong"},
            },
        ],
    }
    suite_path = path / "suite.json"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")
    return suite_path


async def _deterministic_replay(scenario: dict, *, live: bool = False, recorder=None) -> dict:
    if recorder is not None:
        recorder.record("turn", {"text": scenario["turns"][0]["user"]})
    final = "pong" if scenario["id"] == "passes" else "wrong"
    return {"final": final, "tool_calls": [{"name": "echo", "args": {"text": final}}]}


@pytest.mark.asyncio
async def test_run_benchmark_suite_scores_cases_and_writes_outputs(tmp_path: Path) -> None:
    suite_path = _write_suite(tmp_path)
    output_dir = tmp_path / "out"

    summary = await run_benchmark_suite(suite_path, output_dir, replay_fn=_deterministic_replay)

    assert summary["suite"] == "deterministic-smoke"
    assert summary["mode"] == "deterministic"
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["pass_rate"] == 0.5
    assert [case["id"] for case in summary["cases"]] == ["passes", "fails"]
    assert summary["cases"][0]["passed"] is True
    assert summary["cases"][1]["passed"] is False

    written_summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert written_summary == summary

    trajectory_lines = (output_dir / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(trajectory_lines) == 6
    events = [json.loads(line) for line in trajectory_lines]
    assert events[0]["scenario_id"] == "passes"
    assert events[0]["event"] == "scenario_start"
    assert events[1]["event"] == "turn"
    assert events[-1]["event"] == "scenario_end"
    assert events[-1]["data"]["passed"] is False


@pytest.mark.asyncio
async def test_live_mode_requires_explicit_implementation(tmp_path: Path) -> None:
    suite_path = _write_suite(tmp_path)

    with pytest.raises(NotImplementedError, match="live mode"):
        await run_benchmark_suite(suite_path, tmp_path / "out", live=True)


def test_run_benchmarks_cli_writes_summary(tmp_path: Path) -> None:
    suite = {
        "suite": "cli-smoke",
        "scenarios": [
            {
                "id": "inline",
                "turns": [{"user": "hello", "assistant": "hi"}],
                "expect": {"final": "hi"},
            }
        ],
    }
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")
    output_dir = tmp_path / "out"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_benchmarks.py",
            str(suite_path),
            "--output-dir",
            str(output_dir),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "cli-smoke: 1/1 passed" in result.stdout
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] == 1


@pytest.mark.asyncio
async def test_benchmark_runner_executes_scenario_replay_fixture(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "scenarios" / "basic_tool_replay.json"

    summary = await run_benchmark_suite(fixture, tmp_path / "out")

    assert summary["total"] == 1
    assert summary["passed"] == 1
    assert summary["cases"][0]["actual"]["call_sequence"] == [
        "lookup_ticket",
        "send_delivery_receipt",
    ]
