from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.hermes_runtime.checkpoint import build_side_effect_ledger_entry
from app.hermes_runtime.side_effects import classify_side_effect
from app.hermes_runtime.tool_policy import side_effect_class_for_tool, tool_concurrency_key
from app.hermes_runtime.types import RuntimePolicy
from app.tools.tool_result import ToolResult


@dataclass(slots=True)
class RuntimeToolSchema:
    name: str
    schema: dict[str, Any]


@dataclass(slots=True)
class RuntimeToolset:
    tools: list[RuntimeToolSchema] = field(default_factory=list)
    handlers: dict[str, Callable[[dict], Any]] = field(default_factory=dict)
    tenant_id: str = ""


@dataclass(slots=True)
class RuntimeToolResult:
    ok: bool
    content: str
    code: str = ""
    retry_hint: str = ""
    outcome: str = "ok"
    side_effect: bool = False
    structured: dict[str, Any] = field(default_factory=dict)
    artifact_ids: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "content": self.content,
            "code": self.code,
            "retry_hint": self.retry_hint,
            "outcome": self.outcome,
            "side_effect": self.side_effect,
            "structured": self.structured,
            "artifact_ids": self.artifact_ids,
            "duration_ms": self.duration_ms,
        }


def _tool_name_from_schema(schema: dict[str, Any]) -> str:
    if "function" in schema and isinstance(schema["function"], dict):
        return str(schema["function"].get("name") or "")
    return str(schema.get("name") or "")


def build_runtime_toolset(
    tenant,
    *,
    user_text: str = "",
    override_groups: set[str] | None = None,
    suggested_groups: set[str] | None = None,
) -> RuntimeToolset:
    from app.services import base_agent

    tool_defs, tool_map = base_agent._get_tenant_tools(
        tenant,
        user_text=user_text,
        override_groups=override_groups,
        suggested_groups=suggested_groups,
    )
    schemas = [
        RuntimeToolSchema(name=name, schema=schema)
        for schema in tool_defs
        if (name := _tool_name_from_schema(schema))
    ]
    visible_names = {schema.name for schema in schemas}
    handlers = {name: handler for name, handler in tool_map.items() if name in visible_names or not visible_names}
    return RuntimeToolset(
        tools=schemas,
        handlers=handlers,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
    )


_REPO_SKILL_META_TOOL_NAMES = {
    "install_agent_skill_from_github",
    "list_agent_skill_files",
    "read_agent_skill_file",
    "export_agent_skill_template",
}


def _tool_requires_runtime_tenant(tool_name: str) -> bool:
    if tool_name in _REPO_SKILL_META_TOOL_NAMES:
        return True
    try:
        from app.services import base_agent

        return tool_name in base_agent._CUSTOM_TOOL_META_NAMES
    except Exception:
        return False


def _handler_args(toolset: RuntimeToolset, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    if toolset.tenant_id and _tool_requires_runtime_tenant(tool_name):
        normalized["tenant_id"] = toolset.tenant_id
    return normalized


def _append_side_effect_ledger(
    *,
    ledger: list[dict[str, Any]] | None,
    run_id: str,
    tenant_id: str,
    toolset: RuntimeToolset,
    tool_name: str,
    args: dict[str, Any],
) -> dict[str, Any] | None:
    effect = classify_side_effect(tool_name, args)
    if ledger is None or not effect.side_effect:
        return None
    entry = build_side_effect_ledger_entry(
        run_id=run_id,
        tenant_id=tenant_id or toolset.tenant_id,
        tool_name=tool_name,
        args=args,
        status="started",
    )
    ledger.append(entry)
    return entry


def normalize_tool_result(result: Any, *, tool_name: str, duration_ms: int = 0) -> RuntimeToolResult:
    side_effect = classify_side_effect(tool_name).side_effect
    if isinstance(result, ToolResult):
        return RuntimeToolResult(
            ok=result.ok,
            content=str(result),
            code=result.code,
            retry_hint=result.retry_hint,
            outcome=getattr(result, "outcome", "ok" if result.ok else "retryable_error"),
            side_effect=side_effect,
            duration_ms=duration_ms,
        )
    return RuntimeToolResult(
        ok=True,
        content=str(result),
        side_effect=side_effect,
        duration_ms=duration_ms,
    )


def execute_tool_call(
    toolset: RuntimeToolset,
    policy: RuntimePolicy,
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    run_id: str = "",
    tenant_id: str = "",
    ledger: list[dict[str, Any]] | None = None,
) -> RuntimeToolResult:
    args = args or {}
    if policy.allowed_tool_names and tool_name not in set(policy.allowed_tool_names):
        return RuntimeToolResult(
            ok=False,
            content=f"Tool '{tool_name}' is not allowed for this runtime request.",
            code="policy_denied",
            outcome="blocked",
        )
    side_effect = classify_side_effect(tool_name, args).side_effect
    if policy.shadow_mode and side_effect:
        return RuntimeToolResult(
            ok=False,
            content=f"Tool '{tool_name}' is side-effecting and cannot run in Hermes shadow mode.",
            code="shadow_side_effect_denied",
            outcome="blocked",
            side_effect=True,
        )
    handler = toolset.handlers.get(tool_name)
    if handler is None:
        return RuntimeToolResult(
            ok=False,
            content=f"Unknown or unloaded tool: {tool_name}",
            code="tool_unavailable",
            outcome="blocked",
        )
    args = _handler_args(toolset, tool_name, args)
    effect = classify_side_effect(tool_name, args)
    ledger_entry = _append_side_effect_ledger(
        ledger=ledger,
        run_id=run_id,
        tenant_id=tenant_id,
        toolset=toolset,
        tool_name=tool_name,
        args=args,
    )
    start = time.monotonic()
    try:
        result = handler(args)
        if inspect.isawaitable(result):
            if ledger_entry is not None:
                ledger_entry["status"] = "not_executed"
            return RuntimeToolResult(
                ok=False,
                content=f"Async tool '{tool_name}' must be executed by the async runtime worker.",
                code="async_tool_requires_worker",
                outcome="retryable_error",
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        if ledger_entry is not None:
            ledger_entry["status"] = "completed"
            ledger_entry["duration_ms"] = duration_ms
        return normalize_tool_result(result, tool_name=tool_name, duration_ms=duration_ms)
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        if ledger_entry is not None:
            ledger_entry["status"] = "unknown"
            ledger_entry["duration_ms"] = duration_ms
            return RuntimeToolResult(
                ok=False,
                content=f"Runtime lost reliable state for side-effecting tool '{tool_name}': {exc}",
                code="side_effect_unknown",
                outcome="blocked",
                side_effect=effect.side_effect,
                duration_ms=duration_ms,
            )
        return RuntimeToolResult(
            ok=False,
            content=f"Error executing tool '{tool_name}': {exc}",
            code="tool_bridge_error",
            outcome="retryable_error",
            side_effect=effect.side_effect,
            duration_ms=duration_ms,
        )


async def execute_tool_call_async(
    toolset: RuntimeToolset,
    policy: RuntimePolicy,
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    run_id: str = "",
    tenant_id: str = "",
    ledger: list[dict[str, Any]] | None = None,
) -> RuntimeToolResult:
    args = args or {}
    if policy.allowed_tool_names and tool_name not in set(policy.allowed_tool_names):
        return RuntimeToolResult(
            ok=False,
            content=f"Tool '{tool_name}' is not allowed for this runtime request.",
            code="policy_denied",
            outcome="blocked",
        )
    handler = toolset.handlers.get(tool_name)
    if handler is None:
        return RuntimeToolResult(
            ok=False,
            content=f"Unknown or unloaded tool: {tool_name}",
            code="tool_unavailable",
            outcome="blocked",
        )
    args = _handler_args(toolset, tool_name, args)
    effect = classify_side_effect(tool_name, args)
    ledger_entry = _append_side_effect_ledger(
        ledger=ledger,
        run_id=run_id,
        tenant_id=tenant_id,
        toolset=toolset,
        tool_name=tool_name,
        args=args,
    )
    start = time.monotonic()
    try:
        result = handler(args)
        if inspect.isawaitable(result):
            result = await result
        duration_ms = int((time.monotonic() - start) * 1000)
        if ledger_entry is not None:
            ledger_entry["status"] = "completed"
            ledger_entry["duration_ms"] = duration_ms
        return normalize_tool_result(result, tool_name=tool_name, duration_ms=duration_ms)
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        if ledger_entry is not None:
            ledger_entry["status"] = "unknown"
            ledger_entry["duration_ms"] = duration_ms
            return RuntimeToolResult(
                ok=False,
                content=f"Runtime lost reliable state for side-effecting tool '{tool_name}': {exc}",
                code="side_effect_unknown",
                outcome="blocked",
                side_effect=effect.side_effect,
                duration_ms=duration_ms,
            )
        return RuntimeToolResult(
            ok=False,
            content=f"Error executing tool '{tool_name}': {exc}",
            code="tool_bridge_error",
            outcome="retryable_error",
            side_effect=effect.side_effect,
            duration_ms=duration_ms,
        )


def _call_name_and_args(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    tool_name = str(call.get("tool_name") or call.get("name") or "")
    args = call.get("args") or call.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    return tool_name, args


def _execution_lock_key(tool_name: str, args: dict[str, Any]) -> str:
    if side_effect_class_for_tool(tool_name) == "unknown":
        return "__unknown_side_effect__"
    return tool_concurrency_key(tool_name, args)


async def execute_tool_calls(
    toolset: RuntimeToolset,
    policy: RuntimePolicy,
    calls: list[dict[str, Any]],
    *,
    run_id: str = "",
    tenant_id: str = "",
    ledger: list[dict[str, Any]] | None = None,
) -> list[RuntimeToolResult]:
    locks: dict[str, asyncio.Lock] = {}
    results: list[RuntimeToolResult | None] = [None] * len(calls)

    async def run_one(index: int, call: dict[str, Any]) -> None:
        tool_name, args = _call_name_and_args(call)
        lock_key = _execution_lock_key(tool_name, args)
        if lock_key:
            lock = locks.setdefault(lock_key, asyncio.Lock())
            async with lock:
                results[index] = await execute_tool_call_async(
                    toolset,
                    policy,
                    tool_name,
                    args,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    ledger=ledger,
                )
            return
        results[index] = await execute_tool_call_async(
            toolset,
            policy,
            tool_name,
            args,
            run_id=run_id,
            tenant_id=tenant_id,
            ledger=ledger,
        )

    await asyncio.gather(*(run_one(index, call) for index, call in enumerate(calls)))
    return [
        result
        if result is not None
        else RuntimeToolResult(ok=False, content="Tool call did not produce a result.", code="tool_bridge_error")
        for result in results
    ]
