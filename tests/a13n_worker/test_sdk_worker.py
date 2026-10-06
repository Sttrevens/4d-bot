"""Offline contract tests against the installed, pinned real Harness SDK.

Run in the isolated Python 3.13 environment, not the bot's Python 3.12 venv.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

if sys.version_info < (3, 13):
    pytest.skip("a13n worker needs isolated Python 3.13", allow_module_level=True)

pytest.importorskip("a13n_harness")

from a13n_harness import HarnessState
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from scripts.a13n_worker import WorkerProtocolError, run_worker


class FakeTransport:
    def __init__(self, *, reply_call_id="call-1"):
        self.messages = []
        self.reply_call_id = reply_call_id

    async def read(self):
        return {
            "type": "tool_result",
            "call_id": self.reply_call_id,
            "result": {"ok": True, "content": "tool-result-secret", "code": ""},
        }

    async def write(self, message):
        self.messages.append(deepcopy(message))

    @property
    def calls(self):
        return [message for message in self.messages if message["type"] == "tool_call"]


def start(mode="allow"):
    return {
        "type": "start",
        "protocol_version": 1,
        "thread_id": "thr_worker1234",
        "request": {
            "run_id": "host-run-1",
            "input": {"text": "input-secret", "image_urls": [], "attachments": []},
            "runtime": {"model": "unused-in-test", "max_rounds": 4},
        },
        "tools": [
            {
                "name": "lookup",
                "schema": {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Read a record.",
                        "parameters": {
                            "type": "object",
                            "properties": {"key": {"type": "string"}},
                            "required": ["key"],
                            "additionalProperties": False,
                        },
                    },
                },
                "permission": mode,
            }
        ],
    }


def tool_model(arguments=None, *, seen_tools=None):
    async def respond(messages, info):
        if seen_tools is not None:
            seen_tools.append([tool.name for tool in info.function_tools])
        # Only the most recent request determines whether this turn has a result.
        # A resumed turn must still obey its newly reconstructed permissions.
        last = next(message for message in reversed(messages) if isinstance(message, ModelRequest))
        if any(isinstance(part, ToolReturnPart) for part in last.parts):
            yield "final-secret"
        else:
            yield {
                0: DeltaToolCall(
                    name="lookup",
                    json_args=json.dumps(arguments if arguments is not None else {"key": "argument-secret"}),
                    tool_call_id="call-1",
                )
            }

    return FunctionModel(stream_function=respond)


@pytest.mark.parametrize(
    ("mode", "status", "dispatches"),
    [("allow", "completed", 1), ("deny", "completed", 0), ("ask", "awaiting_approval", 0), ("review", "awaiting_approval", 0)],
)
def test_real_permission_gate_before_host_dispatch(mode, status, dispatches):
    transport = FakeTransport()
    response = asyncio.run(run_worker(start(mode), transport, model_factory=lambda _: tool_model()))

    assert response["status"] == status
    assert len(transport.calls) == dispatches
    assert response["sdk_thread_id"] == "thr_worker1234"
    if status == "awaiting_approval":
        assert response["pending_approvals"] == [{"call_id": "call-1", "name": "lookup", "permission": mode}]
        assert response["final_text"] == ""
    else:
        assert response["pending_approvals"] == []
        assert response["final_text"] == "final-secret"


def test_serialized_state_continues_thread_with_fresh_run_and_permission():
    first = asyncio.run(run_worker(start(), FakeTransport(), model_factory=lambda _: tool_model()))
    restored = HarnessState.model_validate_json(first["state"])
    assert restored.thread_id == "thr_worker1234"
    next_start = start("deny")
    next_start["state"] = restored.model_dump_json()
    transport = FakeTransport()
    second = asyncio.run(run_worker(next_start, transport, model_factory=lambda _: tool_model()))

    assert second["status"] == "completed"
    assert second["sdk_thread_id"] == first["sdk_thread_id"]
    assert second["sdk_run_id"] != first["sdk_run_id"]
    assert transport.calls == []
    assert len(HarnessState.model_validate_json(second["state"]).message_history) > len(restored.message_history)


def test_restored_pending_call_is_never_redispatched_without_fresh_authority():
    suspended = asyncio.run(run_worker(start("ask"), FakeTransport(), model_factory=lambda _: tool_model()))
    next_start = start("allow")
    next_start["state"] = suspended["state"]
    transport = FakeTransport()

    async def respond(messages, info):
        del messages, info
        yield "Pending operation has unknown outcome."

    response = asyncio.run(
        run_worker(next_start, transport, model_factory=lambda _: FunctionModel(stream_function=respond))
    )
    assert response["status"] == "completed"
    assert transport.calls == []


def test_preserves_existing_schema_and_no_duplicate_tool_system():
    visible = []
    transport = FakeTransport()
    asyncio.run(run_worker(start(), transport, model_factory=lambda _: tool_model(seen_tools=visible)))
    assert visible and all(names == ["lookup"] for names in visible)
    assert transport.calls[0]["args"] == {"key": "argument-secret"}


@pytest.mark.parametrize("arguments", [{"key": 3}, {"unknown": "x"}, {"key": "x", "extra": "y"}])
def test_actual_json_schema_validation_before_host_dispatch(arguments):
    transport = FakeTransport()
    response = asyncio.run(run_worker(start(), transport, model_factory=lambda _: tool_model(arguments)))
    assert response["status"] == "completed"
    assert transport.calls == []


def test_observation_has_safe_correlation_and_usage_without_raw_content():
    transport = FakeTransport()
    response = asyncio.run(run_worker(start(), transport, model_factory=lambda _: tool_model()))
    events = [message for message in transport.messages if message["type"] == "event"]
    assert events
    serialized = json.dumps(events)
    for secret in ("input-secret", "argument-secret", "tool-result-secret", "final-secret"):
        assert secret not in serialized
    assert {event["sdk_run_id"] for event in events} == {response["sdk_run_id"]}
    assert [event["sequence"] for event in events] == sorted({event["sequence"] for event in events})
    assert response["usage"]["api_calls"] == 2
    assert response["usage"]["tool_calls"] == 1


def test_schema_error_and_media_error_never_start_tool_dispatch():
    request = start()
    request["request"]["input"]["image_urls"] = ["https://example.com/image"]
    transport = FakeTransport()
    with pytest.raises(WorkerProtocolError, match="a13n_media_unsupported"):
        asyncio.run(run_worker(request, transport, model_factory=lambda _: tool_model()))
    assert transport.messages == []


def test_mismatched_thread_state_is_rejected():
    request = start()
    request["state"] = HarnessState.new(thread_id="thr_other1234").model_dump_json()
    transport = FakeTransport()
    with pytest.raises(WorkerProtocolError, match="a13n_thread_mismatch"):
        asyncio.run(run_worker(request, transport, model_factory=lambda _: tool_model()))
    assert transport.messages == []


def test_model_failure_does_not_return_completed_checkpoint():
    async def fail(messages, info):
        del messages, info
        raise RuntimeError("provider-secret")
        yield "unreachable"

    transport = FakeTransport()
    response = asyncio.run(
        run_worker(start(), transport, model_factory=lambda _: FunctionModel(stream_function=fail))
    )
    assert response["status"] == "failed"
    assert response["final_text"] == ""
    assert response["error_code"]
    assert "provider-secret" not in json.dumps(transport.messages)


def test_invalid_tool_result_correlation_cannot_complete_successfully():
    transport = FakeTransport(reply_call_id="wrong-call")
    with pytest.raises(WorkerProtocolError, match="a13n_tool_correlation_invalid"):
        asyncio.run(run_worker(start(), transport, model_factory=lambda _: tool_model()))
    assert not [message for message in transport.messages if message["type"] == "result"]


def test_cli_failure_is_safe_json_and_no_scripted_provider_wire_switch():
    request = start()
    request["request"]["runtime"]["model"] = ""
    request["model_factory"] = "FunctionModel"
    worker = Path(__file__).resolve().parents[2] / "scripts" / "a13n_worker.py"
    completed = subprocess.run(
        [sys.executable, str(worker)], input=json.dumps(request) + "\n", text=True, capture_output=True, check=False
    )
    assert completed.returncode == 1
    response = json.loads(completed.stdout)
    assert response == {
        "type": "result", "status": "failed", "error_code": "a13n_model_required", "final_text": ""
    }
    assert "input-secret" not in completed.stdout + completed.stderr
