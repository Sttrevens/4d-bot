"""Deterministic JSON scenario benchmark runner for Hermes harness work."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.harness.trajectory import TrajectoryRecorder, reset_trajectory_file

ReplayFn = Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _scenario_id(scenario: dict[str, Any], index: int) -> str:
    return str(scenario.get("id") or scenario.get("name") or f"scenario-{index + 1}")


def _suite_name(suite_path: Path, payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("suite") or payload.get("name") or suite_path.stem)
    return suite_path.stem


def _load_suite(suite_path: Path) -> tuple[str, list[dict[str, Any]]]:
    if suite_path.is_dir():
        scenarios: list[dict[str, Any]] = []
        for path in sorted(suite_path.glob("*.json")):
            _, loaded = _load_suite(path)
            scenarios.extend(loaded)
        return suite_path.name, scenarios

    payload = _load_json(suite_path)
    if isinstance(payload, list):
        return suite_path.stem, [dict(item) for item in payload]
    if not isinstance(payload, dict):
        raise ValueError(f"Benchmark suite must be a JSON object or list: {suite_path}")
    if "scenarios" in payload:
        scenarios = payload["scenarios"]
        if not isinstance(scenarios, list):
            raise ValueError("Benchmark suite 'scenarios' must be a list")
        return _suite_name(suite_path, payload), [dict(item) for item in scenarios]
    return _suite_name(suite_path, payload), [payload]


def _expected_for(scenario: dict[str, Any]) -> dict[str, Any]:
    expected = scenario.get("expect", scenario.get("expected", {}))
    if isinstance(expected, str):
        return {"final": expected}
    if expected is None:
        return {}
    if not isinstance(expected, dict):
        raise ValueError(f"Scenario expectation must be an object or string: {_scenario_id(scenario, 0)}")
    return expected


def _matches_expected(actual: dict[str, Any], expected: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if actual_value != expected_value:
            failures.append(f"{key}: expected {expected_value!r}, got {actual_value!r}")
    return not failures, failures


def _fallback_replay(scenario: dict[str, Any], *, live: bool = False, recorder: TrajectoryRecorder | None = None) -> dict[str, Any]:
    if live:
        raise NotImplementedError("live mode requires app.harness.scenario_replay support")

    turns = scenario.get("turns") or []
    final: Any = scenario.get("final")
    tool_calls = scenario.get("tool_calls", [])
    if turns:
        for turn in turns:
            if recorder is not None and isinstance(turn, dict) and "user" in turn:
                recorder.record("turn", {"text": turn["user"]})
        last_turn = turns[-1]
        if isinstance(last_turn, dict):
            final = last_turn.get("assistant", last_turn.get("final", final))
    if final is None:
        final = _expected_for(scenario).get("final")
    return {"final": final, "tool_calls": tool_calls}


def _scenario_replay_fn() -> ReplayFn | None:
    try:
        module = importlib.import_module("app.harness.scenario_replay")
    except ModuleNotFoundError:
        return None
    for name in ("replay_scenario", "run_scenario"):
        replay_fn = getattr(module, name, None)
        if callable(replay_fn):
            return replay_fn
    return None


def _uses_scenario_replay_contract(scenario: dict[str, Any]) -> bool:
    return all(
        key in scenario
        for key in (
            "context",
            "user_text",
            "expected_visible_tools",
            "scripted_model_tool_calls",
            "scripted_tool_results",
            "final_text",
            "assertions",
        )
    )


def _normalize_replay_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    final = getattr(result, "final_text", None)
    if final is not None:
        return {
            "final": final,
            "call_sequence": list(getattr(result, "call_sequence", [])),
            "visible_tools": list(getattr(result, "visible_tools", [])),
            "ledger_ids": list(getattr(result, "ledger_ids", [])),
        }
    raise TypeError("Scenario replay must return a dict or ScenarioReplayResult-like object")


async def _invoke_replay(
    replay_fn: ReplayFn,
    scenario: dict[str, Any],
    *,
    live: bool,
    recorder: TrajectoryRecorder,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    signature = inspect.signature(replay_fn)
    if "live" in signature.parameters:
        kwargs["live"] = live
    if "recorder" in signature.parameters:
        kwargs["recorder"] = recorder
    result = replay_fn(scenario, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return _normalize_replay_result(result)


async def run_benchmark_suite(
    suite_path: str | Path,
    output_dir: str | Path,
    *,
    live: bool = False,
    replay_fn: ReplayFn | None = None,
) -> dict[str, Any]:
    """Run a scenario suite and write ``summary.json`` plus ``trajectories.jsonl``."""

    suite_path = Path(suite_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trajectory_path = output_dir / "trajectories.jsonl"
    reset_trajectory_file(trajectory_path)

    suite, scenarios = _load_suite(suite_path)
    scenario_replay = _scenario_replay_fn()
    if live and replay_fn is None:
        raise NotImplementedError("live mode is not implemented by the benchmark runner yet")

    cases: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenarios):
        scenario_id = _scenario_id(scenario, index)
        expected = _expected_for(scenario)
        recorder = TrajectoryRecorder(trajectory_path, scenario_id)
        recorder.record("scenario_start", {"suite": suite, "live": live})
        replay = replay_fn
        if replay is None and scenario_replay is not None and _uses_scenario_replay_contract(scenario):
            replay = scenario_replay
        if replay is None:
            replay = _fallback_replay
        try:
            actual = await _invoke_replay(replay, scenario, live=live, recorder=recorder)
            passed, failures = _matches_expected(actual, expected)
        except AssertionError as exc:
            actual = {}
            passed = False
            failures = [str(exc)]
        case = {
            "id": scenario_id,
            "passed": passed,
            "expected": expected,
            "actual": actual,
            "failures": failures,
        }
        recorder.record("scenario_end", {"passed": passed, "failures": failures})
        cases.append(case)

    passed_count = sum(1 for case in cases if case["passed"])
    total = len(cases)
    summary = {
        "suite": suite,
        "mode": "live" if live else "deterministic",
        "total": total,
        "passed": passed_count,
        "failed": total - passed_count,
        "pass_rate": passed_count / total if total else 0.0,
        "cases": cases,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def run_benchmark_suite_sync(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_benchmark_suite(*args, **kwargs))
