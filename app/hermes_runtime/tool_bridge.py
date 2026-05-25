from __future__ import annotations

import asyncio
import inspect
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.hermes_runtime.checkpoint import build_side_effect_ledger_entry
from app.hermes_runtime.execution_policy import ExecutionPolicy, evaluate_execution_request
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


def _confirmation_tokens_for(effect_class: str) -> set[str]:
    tokens = {effect_class}
    if effect_class == "infrastructure":
        tokens.update({"deploy", "infrastructure"})
    elif effect_class == "message_send":
        tokens.update({"send_external_message", "message_send"})
    elif effect_class == "code_mutation":
        tokens.add("code_mutation")
    elif effect_class == "platform_write":
        tokens.add("platform_write")
    return tokens


def _requires_confirmation(policy: RuntimePolicy, tool_name: str, effect) -> bool:
    required = {
        str(item).strip()
        for item in policy.requires_confirmation_for
        if str(item).strip()
    }
    if not required:
        return False
    if tool_name in required:
        return True
    return bool(required & _confirmation_tokens_for(effect.side_effect_class))


def _confirmation_required_result(
    *,
    tool_name: str,
    args: dict[str, Any],
    effect,
) -> RuntimeToolResult:
    approval_request = {
        "tool_name": tool_name,
        "args": args,
        "side_effect_class": effect.side_effect_class,
        "rollback_available": effect.rollback_available,
        "user_visible": effect.user_visible,
    }
    return RuntimeToolResult(
        ok=False,
        content=f"Tool '{tool_name}' requires explicit approval before execution.",
        code="confirmation_required",
        outcome="needs_confirmation",
        side_effect=effect.side_effect,
        structured={"approval_request": approval_request},
    )


_EXECUTION_GATED_TOOLS = {
    "browser_do",
    "browser_open",
    "create_custom_tool",
    "install_package",
    "lark_cli_run",
    "local_agent_request",
    "test_custom_tool",
}


def _execution_request(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if tool_name not in _EXECUTION_GATED_TOOLS:
        return None
    if tool_name == "lark_cli_run":
        argv = args.get("argv") if isinstance(args.get("argv"), list) else []
        command = "lark-cli"
        if argv:
            command = f"{command} {shlex.join(str(item) for item in argv)}"
        return {"command": command, "cwd": "/workspace", "write_paths": []}
    if tool_name == "install_package":
        package = str(args.get("package_name") or "").strip()
        command = "pip install" if not package else f"pip install {shlex.quote(package)}"
        return {"command": command, "cwd": "/workspace", "write_paths": []}
    if tool_name == "local_agent_request":
        local_tool = str(args.get("tool") or "")
        tool_args = args.get("tool_args") if isinstance(args.get("tool_args"), dict) else {}
        command = str(tool_args.get("command") or "") if local_tool == "bash.run" else ""
        cwd = str(tool_args.get("cwd") or args.get("cwd") or "/workspace")
        write_paths: list[str] = []
        if local_tool in {"file.write", "file.patch", "file.delete"}:
            target_path = str(tool_args.get("path") or "").strip()
            if target_path:
                write_paths.append(target_path)
        skill_script_path = ""
        if local_tool in {"skill.script.run", "skill_script.run"}:
            skill_script_path = str(tool_args.get("path") or tool_args.get("script_path") or "")
        return {
            "command": command,
            "cwd": cwd,
            "write_paths": write_paths,
            "skill_script_path": skill_script_path,
        }
    return {"command": "", "cwd": "/workspace", "write_paths": []}


def _execution_policy_result(
    *,
    tool_name: str,
    args: dict[str, Any],
    execution_policy: ExecutionPolicy | None,
) -> RuntimeToolResult | None:
    request = _execution_request(tool_name, args)
    if request is None:
        return None
    policy = execution_policy or ExecutionPolicy()
    decision = evaluate_execution_request(
        policy,
        command=str(request.get("command") or ""),
        cwd=str(request.get("cwd") or "/workspace"),
        write_paths=list(request.get("write_paths") or []),
        skill_script_path=str(request.get("skill_script_path") or ""),
    )
    if decision.allowed:
        return None
    code = decision.code
    if code in {"file_write_denied", "path_denied"}:
        code = "policy_denied"
    structured = {"tool_name": tool_name}
    if decision.approval_request:
        structured["approval_request"] = decision.approval_request
    return RuntimeToolResult(
        ok=False,
        content=decision.message,
        code=code,
        outcome=decision.status,
        side_effect=True,
        structured=structured,
    )


def _autofix_boundary_result(
    *,
    tool_name: str,
    args: dict[str, Any],
    effect,
) -> RuntimeToolResult | None:
    if tool_name not in {"self_write_file", "self_edit_file"}:
        return None
    path = str(args.get("path") or "")
    try:
        from app.services.auto_fix import (
            _autofix_write_denial_message,
            _is_autofix_write_path_allowed,
        )
    except ModuleNotFoundError:
        default_allowed = ("app/tools/", "app/knowledge/")

        def _is_autofix_write_path_allowed(path: str) -> bool:
            return bool(path) and any(path.startswith(prefix) for prefix in default_allowed)

        def _autofix_write_denial_message(path: str) -> str:
            return (
                f"不允许修改 {path}（超出 auto-fix 修复范围）。\n"
                f"auto-fix 只能修改应用层代码：{', '.join(default_allowed)}\n"
                "如果 bug 在基础设施层，请在修复报告中描述问题和建议方案，管理员会人工处理。"
            )

    if _is_autofix_write_path_allowed(path):
        return None
    return RuntimeToolResult(
        ok=False,
        content=_autofix_write_denial_message(path),
        code="policy_denied",
        outcome="blocked",
        side_effect=effect.side_effect,
        structured={
            "path": path,
            "side_effect_class": effect.side_effect_class,
        },
    )


def _shadow_side_effect_result(*, tool_name: str, effect) -> RuntimeToolResult | None:
    if not effect.side_effect:
        return None
    return RuntimeToolResult(
        ok=False,
        content=f"Tool '{tool_name}' has side effects and is blocked in Hermes shadow mode.",
        code="shadow_side_effect_denied",
        outcome="blocked",
        side_effect=True,
        structured={
            "side_effect_class": effect.side_effect_class,
            "rollback_available": effect.rollback_available,
            "user_visible": effect.user_visible,
        },
    )


def normalize_tool_result(result: Any, *, tool_name: str, duration_ms: int = 0) -> RuntimeToolResult:
    side_effect = classify_side_effect(tool_name).side_effect
    if isinstance(result, RuntimeToolResult):
        result.side_effect = result.side_effect or side_effect
        result.duration_ms = result.duration_ms or duration_ms
        return result
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
    execution_policy: ExecutionPolicy | None = None,
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
    if policy.shadow_mode:
        shadow_result = _shadow_side_effect_result(tool_name=tool_name, effect=effect)
        if shadow_result is not None:
            return shadow_result
    execution_result = _execution_policy_result(
        tool_name=tool_name,
        args=args,
        execution_policy=execution_policy,
    )
    if execution_result is not None:
        return execution_result
    boundary_result = _autofix_boundary_result(tool_name=tool_name, args=args, effect=effect)
    if boundary_result is not None:
        return boundary_result
    if _requires_confirmation(policy, tool_name, effect):
        return _confirmation_required_result(
            tool_name=tool_name,
            args=args,
            effect=effect,
        )
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
    execution_policy: ExecutionPolicy | None = None,
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
    if policy.shadow_mode:
        shadow_result = _shadow_side_effect_result(tool_name=tool_name, effect=effect)
        if shadow_result is not None:
            return shadow_result
    execution_result = _execution_policy_result(
        tool_name=tool_name,
        args=args,
        execution_policy=execution_policy,
    )
    if execution_result is not None:
        return execution_result
    boundary_result = _autofix_boundary_result(tool_name=tool_name, args=args, effect=effect)
    if boundary_result is not None:
        return boundary_result
    if _requires_confirmation(policy, tool_name, effect):
        return _confirmation_required_result(
            tool_name=tool_name,
            args=args,
            effect=effect,
        )
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
    execution_policy: ExecutionPolicy | None = None,
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
                    execution_policy=execution_policy,
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
            execution_policy=execution_policy,
        )

    await asyncio.gather(*(run_one(index, call) for index, call in enumerate(calls)))
    return [
        result
        if result is not None
        else RuntimeToolResult(ok=False, content="Tool call did not produce a result.", code="tool_bridge_error")
        for result in results
    ]
