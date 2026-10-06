#!/usr/bin/env python3
"""Isolated Python 3.13 Harness worker; application tools stay in the bot Host.

This worker imports neither ``app`` nor its providers. Its one-run JSONL protocol
is private to ``app.agent_runtime.a13n_adapter``; stdout contains protocol messages only.
The model is selected explicitly by the Host and credentials are reconstructed
from the worker process environment, never from a saved HarnessState.

Approval and review are suspension-only in this spike. Pending call summaries
are display metadata, not authenticated grants or a DeferredToolResume envelope.
The Host commits completed states only; deferred/uncertain work needs manual
reconciliation. ``tool_recovery='never'`` prevents restored call redispatch but
does not forbid a later newly generated call under fresh Host policy.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any

PROTOCOL_VERSION = 1
MAX_PROTOCOL_BYTES = 16 * 1024 * 1024


class WorkerProtocolError(RuntimeError):
    """A safe protocol failure, with no request or provider payload in its text."""


class StdioTransport:
    async def read(self) -> dict[str, Any]:
        line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_PROTOCOL_BYTES + 1)
        if not line or len(line) > MAX_PROTOCOL_BYTES:
            raise WorkerProtocolError("a13n_protocol_invalid")
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise WorkerProtocolError("a13n_protocol_invalid")
        return payload

    async def write(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_PROTOCOL_BYTES:
            raise WorkerProtocolError("a13n_protocol_too_large")
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()


class ApprovalReviewer:
    """Fail closed until the Host supplies an authenticated review lifecycle."""

    async def review(self, request: Any, *, context: Any) -> Any:
        del request, context
        from a13n_harness.capabilities import ToolReviewAssessment, ToolReviewResult

        return ToolReviewResult(
            assessment=ToolReviewAssessment(
                risk="high", reason="Host review approval is required before dispatch."
            )
        )


def _tool_schema(entry: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Accept the existing OpenAI tool definition or its parameter schema."""
    schema = entry.get("schema")
    if not isinstance(schema, dict):
        raise WorkerProtocolError("a13n_tool_schema_invalid")
    function = schema.get("function", schema)
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise WorkerProtocolError("a13n_tool_schema_invalid")
    parameters = function.get("parameters", function)
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise WorkerProtocolError("a13n_tool_schema_invalid")
    return name, str(function.get("description") or name), parameters


def _make_tool(entry: dict[str, Any], transport: Any) -> Any:
    from jsonschema import Draft202012Validator, ValidationError
    from pydantic_ai import Tool
    from pydantic_ai.exceptions import ToolFailed

    name, description, schema = _tool_schema(entry)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    def validate(ctx: Any, **arguments: Any) -> None:
        del ctx
        try:
            validator.validate(arguments)
        except ValidationError as exc:
            # JSONSchema's default error text includes values; keep it private.
            raise ToolFailed("Tool arguments do not match the current schema.") from exc

    async def execute(ctx: Any, **arguments: Any) -> Any:
        if not ctx.tool_call_id:
            raise WorkerProtocolError("a13n_tool_correlation_invalid")
        await transport.write(
            {"type": "tool_call", "call_id": ctx.tool_call_id, "name": name, "args": arguments}
        )
        response = await transport.read()
        if response.get("type") != "tool_result" or response.get("call_id") != ctx.tool_call_id:
            raise WorkerProtocolError("a13n_tool_correlation_invalid")
        result = response.get("result")
        if not isinstance(result, dict):
            raise WorkerProtocolError("a13n_tool_result_invalid")
        return result

    # from_schema does not install schema validation itself in Pydantic AI.
    # sequential RPC avoids concurrently reading results from one stdin channel.
    return Tool.from_schema(
        execute,
        name=name,
        description=description,
        json_schema=schema,
        takes_ctx=True,
        sequential=True,
        args_validator=validate,
    )


def _event_projection(item: Any) -> dict[str, Any]:
    """Keep correlation and bounded structural facts, never model/tool payloads."""
    from a13n_harness import HarnessExtensionEvent

    value = item.event
    data: dict[str, Any] = {}
    if isinstance(value, HarnessExtensionEvent):
        payload = value.payload if isinstance(value.payload, dict) else {}
        kind = str(payload.get("type") or value.kind)
        for key in ("request_index", "message_count", "decision", "status", "error_code"):
            field = payload.get(key)
            if isinstance(field, (str, int, bool)):
                data[key] = field
    else:
        kind = str(getattr(value, "event_kind", type(value).__name__))
    return {
        "type": "event",
        "event": kind,
        "data": data,
        "sdk_thread_id": item.thread_id,
        "sdk_run_id": item.run_id,
        "sequence": item.sequence,
    }


async def run_worker(
    start: dict[str, Any],
    transport: Any,
    *,
    model_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Run the real SDK; model_factory is a Python test seam, never wire input."""
    from a13n_harness import (
        AgentSpec,
        HarnessBuilder,
        HarnessEvent,
        HarnessRunResultEvent,
        HarnessState,
        RunBindings,
    )
    from a13n_harness.capabilities import ToolReviewPolicy
    from a13n_harness.tools import ToolPermissions, ToolPermissionsCapability, source_tool_id
    from pydantic_ai.capabilities import Capability
    from pydantic_ai.toolsets import FunctionToolset
    from pydantic_ai.usage import UsageLimits

    if start.get("type") != "start" or start.get("protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
        raise WorkerProtocolError("a13n_protocol_invalid")
    request = start.get("request")
    entries = start.get("tools", [])
    if not isinstance(request, dict) or not isinstance(entries, list) or not all(isinstance(x, dict) for x in entries):
        raise WorkerProtocolError("a13n_protocol_invalid")
    request_input = request.get("input", {})
    runtime = request.get("runtime", {})
    if not isinstance(request_input, dict) or not isinstance(runtime, dict):
        raise WorkerProtocolError("a13n_protocol_invalid")
    if request_input.get("image_urls") or request_input.get("attachments"):
        raise WorkerProtocolError("a13n_media_unsupported")
    if model_factory is None and not runtime.get("model"):
        raise WorkerProtocolError("a13n_model_required")

    names = [str(entry.get("name", "")) for entry in entries]
    if len(names) != len(set(names)):
        raise WorkerProtocolError("a13n_tool_schema_invalid")
    rules = {}
    for entry in entries:
        permission = entry.get("permission", "deny")
        if permission not in {"allow", "deny", "ask", "review"}:
            raise WorkerProtocolError("a13n_permission_invalid")
        rules[source_tool_id("4d-bot", entry["name"])] = permission

    permissions = ToolPermissionsCapability(
        ToolPermissions(default="deny", rules=rules),
        reviewer=ApprovalReviewer(),
        policy=ToolReviewPolicy(risk_threshold="low", on_flagged="approval_required"),
    )
    tools = [_make_tool(entry, transport) for entry in entries]
    # Tools are existing Host definitions; no Harness memory, file, shell,
    # browser or subagent toolsets are installed by this worker.
    definition_capabilities = (
        Capability(toolsets=[FunctionToolset(tools, id="4d-bot")]),
        permissions,
    )
    model = model_factory(start) if model_factory is not None else None
    instructions = start.get("instructions", "")
    if not isinstance(instructions, str):
        raise WorkerProtocolError("a13n_protocol_invalid")
    spec = AgentSpec(instructions=instructions, model=runtime["model"] if model is None else None)
    executable = HarnessBuilder(instrumentation=None).build(
        spec, output_type=str, model=model, capabilities=definition_capabilities
    )
    serialized = start.get("state")
    previous = HarnessState.model_validate_json(serialized) if serialized else HarnessState.new(
        thread_id=start.get("thread_id")
    )
    if start.get("thread_id") and previous.thread_id != start["thread_id"]:
        raise WorkerProtocolError("a13n_thread_mismatch")
    text = request_input.get("text", "")
    if not isinstance(text, str):
        raise WorkerProtocolError("a13n_protocol_invalid")
    if request_input.get("chat_context"):
        text += "\n\n" + str(request_input["chat_context"])
    terminal = None
    async with executable.stream(
        text,
        previous_state=previous,
        bindings=RunBindings.embedded(),
        # Never repeat an unanswered Host operation after a worker loss.
        tool_recovery="never",
        usage_limits=UsageLimits(request_limit=int(runtime.get("max_rounds", 12))),
    ) as stream:
        async for item in stream:
            if isinstance(item, HarnessRunResultEvent):
                terminal = item.result
            elif isinstance(item, HarnessEvent):
                await transport.write(_event_projection(item))
    if terminal is None:
        raise WorkerProtocolError("a13n_terminal_missing")
    pending = []
    if terminal.deferred is not None:
        for call in terminal.deferred.approvals:
            pending.append(
                {
                    "call_id": call.tool_call_id,
                    "name": call.tool_name,
                    "permission": next(
                        (entry.get("permission", "deny") for entry in entries if entry["name"] == call.tool_name),
                        "deny",
                    ),
                }
            )
    result = {
        "type": "result",
        "protocol_version": PROTOCOL_VERSION,
        "status": "awaiting_approval" if terminal.status == "suspended" else terminal.status,
        "final_text": str(terminal.output or ""),
        "usage": {
            "input_tokens": terminal.usage.input_tokens,
            "output_tokens": terminal.usage.output_tokens,
            "api_calls": terminal.usage.requests,
            "tool_calls": terminal.usage.tool_calls,
        },
        "state": terminal.state.model_dump_json() if terminal.state is not None else None,
        "sdk_run_id": terminal.run_id,
        "sdk_thread_id": terminal.thread_id,
        "pending_approvals": pending,
        "error_code": terminal.failure.code if terminal.failure else "",
    }
    await transport.write(result)
    return result


async def main() -> int:
    transport = StdioTransport()
    try:
        await run_worker(await transport.read(), transport)
        return 0
    except Exception as exc:
        # Provider and schema errors can contain credentials or user data.
        code = str(exc) if isinstance(exc, WorkerProtocolError) else "a13n_worker_failed"
        await transport.write({"type": "result", "status": "failed", "error_code": code, "final_text": ""})
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
