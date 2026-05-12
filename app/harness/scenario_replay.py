"""Deterministic JSON scenario replay harness.

This module is intentionally offline-only: a scenario supplies the visible tool
set, scripted model tool calls, scripted tool results, and final assistant text.
Replay verifies the transcript contract without invoking providers or tools.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Sequence


class ScenarioReplayError(AssertionError):
    """Raised when a deterministic scenario transcript violates expectations."""


@dataclass(frozen=True)
class ScenarioToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ScenarioToolOutput:
    name: str
    text: str


@dataclass(frozen=True)
class ScenarioReplayResult:
    scenario_id: str
    tenant_id: str
    platform: str
    user_text: str
    visible_tools: list[str]
    tool_calls: list[ScenarioToolCall]
    call_sequence: list[str]
    tool_outputs: list[ScenarioToolOutput]
    final_text: str
    ledger_ids: list[str]


def replay_scenario_file(
    path: str | Path,
    *,
    visible_tools: Sequence[str] | None = None,
    recorder: Any | None = None,
) -> ScenarioReplayResult:
    """Load and replay a JSON scenario file.

    ``visible_tools`` can be supplied by an integration test that computes a
    tenant/platform tool list elsewhere. When omitted, the fixture's expected
    visible tools become the scripted visible set for fully offline replay.
    """
    scenario_path = Path(path)
    try:
        data = json.loads(scenario_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScenarioReplayError(f"invalid scenario JSON: {scenario_path}: {exc}") from exc

    return replay_scenario(data, visible_tools=visible_tools, recorder=recorder)


def replay_scenario(
    data: dict[str, Any],
    *,
    visible_tools: Sequence[str] | None = None,
    recorder: Any | None = None,
) -> ScenarioReplayResult:
    scenario_id = _required_str(data, "id")
    context = _required_dict(data, "context")
    tenant_id = _required_str(context, "tenant_id")
    platform = _required_str(context, "platform")
    user_text = _required_str(data, "user_text")

    expected_visible_tools = _required_str_list(data, "expected_visible_tools")
    actual_visible_tools = list(visible_tools) if visible_tools is not None else expected_visible_tools
    if actual_visible_tools != expected_visible_tools:
        raise ScenarioReplayError(
            f"visible tools mismatch for {scenario_id}: "
            f"expected {expected_visible_tools}, got {actual_visible_tools}"
        )

    tool_calls = _parse_tool_calls(data.get("scripted_model_tool_calls"), scenario_id)
    tool_outputs = _parse_tool_outputs(data.get("scripted_tool_results"), scenario_id)
    if len(tool_calls) != len(tool_outputs):
        raise ScenarioReplayError(
            f"tool call/result count mismatch for {scenario_id}: "
            f"{len(tool_calls)} calls, {len(tool_outputs)} results"
        )

    for index, (call, output) in enumerate(zip(tool_calls, tool_outputs), start=1):
        _record(recorder, "tool_call", {"name": call.name, "arguments": call.arguments})
        _record(recorder, "tool_result", {"name": output.name, "text": output.text})
        if call.name != output.name:
            raise ScenarioReplayError(
                f"tool result #{index} mismatch for {scenario_id}: "
                f"call {call.name!r}, result {output.name!r}"
            )

    final_text = _required_str(data, "final_text")
    assertions = _required_dict(data, "assertions")
    call_sequence = [call.name for call in tool_calls]
    expected_call_sequence = _required_str_list(assertions, "expected_call_sequence")
    if call_sequence != expected_call_sequence:
        raise ScenarioReplayError(
            f"call sequence mismatch for {scenario_id}: "
            f"expected {expected_call_sequence}, got {call_sequence}"
        )

    for fragment in _required_str_list(assertions, "final_text_contains"):
        if fragment not in final_text:
            raise ScenarioReplayError(
                f"final text for {scenario_id} is missing expected fragment {fragment!r}"
            )
    _record(recorder, "final", {"text": final_text})

    combined_tool_output = "\n".join(output.text for output in tool_outputs)
    expected_ledger_ids = _required_str_list(assertions, "tool_output_ledger_ids")
    for ledger_id in expected_ledger_ids:
        if ledger_id not in combined_tool_output:
            raise ScenarioReplayError(
                f"tool output for {scenario_id} is missing ledger id {ledger_id!r}"
            )

    return ScenarioReplayResult(
        scenario_id=scenario_id,
        tenant_id=tenant_id,
        platform=platform,
        user_text=user_text,
        visible_tools=actual_visible_tools,
        tool_calls=tool_calls,
        call_sequence=call_sequence,
        tool_outputs=tool_outputs,
        final_text=final_text,
        ledger_ids=expected_ledger_ids,
    )


def _parse_tool_calls(value: Any, scenario_id: str) -> list[ScenarioToolCall]:
    if not isinstance(value, list):
        raise ScenarioReplayError(f"scripted_model_tool_calls must be a list for {scenario_id}")

    calls: list[ScenarioToolCall] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ScenarioReplayError(f"tool call #{index} must be an object for {scenario_id}")
        name = _required_str(item, "name")
        arguments = item.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ScenarioReplayError(
                f"tool call #{index} arguments must be an object for {scenario_id}"
            )
        calls.append(ScenarioToolCall(name=name, arguments=dict(arguments)))
    return calls


def _parse_tool_outputs(value: Any, scenario_id: str) -> list[ScenarioToolOutput]:
    if not isinstance(value, list):
        raise ScenarioReplayError(f"scripted_tool_results must be a list for {scenario_id}")

    outputs: list[ScenarioToolOutput] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ScenarioReplayError(f"tool result #{index} must be an object for {scenario_id}")
        outputs.append(
            ScenarioToolOutput(
                name=_required_str(item, "name"),
                text=_required_str(item, "text"),
            )
        )
    return outputs


def _required_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ScenarioReplayError(f"{key} must be an object")
    return value


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ScenarioReplayError(f"{key} must be a non-empty string")
    return value


def _required_str_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ScenarioReplayError(f"{key} must be a list of strings")
    return list(value)


def _record(recorder: Any | None, event: str, data: dict[str, Any]) -> None:
    if recorder is None:
        return
    record = getattr(recorder, "record", None)
    if callable(record):
        record(event, data)
