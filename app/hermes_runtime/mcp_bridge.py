from __future__ import annotations

import inspect
from typing import Any, Callable

from app.hermes_runtime.mcp_registry import McpServerConfig, visible_servers_for_tenant
from app.hermes_runtime.tool_bridge import RuntimeToolResult, RuntimeToolSchema, RuntimeToolset, normalize_tool_result
from app.tools.tool_result import ToolResult


McpHandler = Callable[[str, dict[str, Any], dict[str, str]], Any]


def prefix_tool_name(server_id: str, tool_name: str) -> str:
    clean_server = "".join(ch if ch.isalnum() else "_" for ch in server_id.strip().lower()).strip("_")
    clean_tool = "".join(ch if ch.isalnum() else "_" for ch in tool_name.strip()).strip("_")
    return f"mcp_{clean_server}_{clean_tool}"


def _server_prefix(server_id: str) -> str:
    return prefix_tool_name(server_id, "")


def _tool_allowlist(tenant) -> set[str]:
    values = getattr(tenant, "mcp_tool_allowlist", []) or []
    return {str(value) for value in values if str(value).strip()}


def _raw_tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("name") or tool.get("tool_name") or "").strip()


def _tool_parameters(tool: dict[str, Any]) -> dict[str, Any]:
    params = tool.get("input_schema") or tool.get("parameters") or {}
    return params if isinstance(params, dict) else {}


def discover_mcp_tools(
    tenant,
    servers: list[McpServerConfig],
    server_tools: dict[str, list[dict[str, Any]]],
) -> RuntimeToolset:
    """Build a tenant-scoped, prefixed MCP toolset without starting servers."""
    allowlist = _tool_allowlist(tenant)
    schemas: list[RuntimeToolSchema] = []
    handlers: dict[str, Any] = {}

    for server in visible_servers_for_tenant(tenant, servers):
        for raw_tool in server_tools.get(server.server_id, []):
            raw_name = _raw_tool_name(raw_tool)
            if not raw_name:
                continue
            prefixed_name = prefix_tool_name(server.server_id, raw_name)
            if allowlist and prefixed_name not in allowlist:
                continue
            schema = {
                "type": "function",
                "function": {
                    "name": prefixed_name,
                    "description": str(raw_tool.get("description") or server.description or server.label),
                    "parameters": _tool_parameters(raw_tool),
                },
            }
            schemas.append(RuntimeToolSchema(name=prefixed_name, schema=schema))
            handlers[prefixed_name] = {
                "server_id": server.server_id,
                "tool_name": raw_name,
            }

    return RuntimeToolset(
        tools=schemas,
        handlers=handlers,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
    )


def _resolve_prefixed_tool(
    tenant,
    servers: list[McpServerConfig],
    tool_name: str,
) -> tuple[McpServerConfig | None, str]:
    for server in visible_servers_for_tenant(tenant, servers):
        prefix = _server_prefix(server.server_id)
        if tool_name.startswith(prefix):
            return server, tool_name.removeprefix(prefix)
    return None, ""


def _env_for_server(server: McpServerConfig, env: dict[str, str]) -> dict[str, str]:
    return {ref: env[ref] for ref in server.env_refs if env.get(ref)}


def _normalize_mcp_result(result: Any, *, tool_name: str) -> RuntimeToolResult:
    if isinstance(result, RuntimeToolResult):
        return result
    if isinstance(result, ToolResult):
        return normalize_tool_result(result, tool_name=tool_name)
    if isinstance(result, dict):
        ok = bool(result.get("ok", True))
        content = str(result.get("content") or result.get("message") or result)
        structured = result if ok else {}
        return RuntimeToolResult(
            ok=ok,
            content=content,
            code=str(result.get("code") or ""),
            outcome=str(result.get("outcome") or ("ok" if ok else "retryable_error")),
            structured=structured,
        )
    return RuntimeToolResult(ok=True, content=str(result))


def _prepare_mcp_call(
    tenant,
    servers: list[McpServerConfig],
    tool_name: str,
    env: dict[str, str],
) -> tuple[RuntimeToolResult | None, McpServerConfig | None, str, dict[str, str]]:
    allowlist = _tool_allowlist(tenant)
    if allowlist and tool_name not in allowlist:
        return (
            RuntimeToolResult(
                ok=False,
                content=f"MCP tool '{tool_name}' is not allowed for this tenant.",
                code="policy_denied",
                outcome="blocked",
            ),
            None,
            "",
            {},
        )

    server, raw_tool_name = _resolve_prefixed_tool(tenant, servers, tool_name)
    if server is None or not raw_tool_name:
        return (
            RuntimeToolResult(
                ok=False,
                content=f"Unknown or disabled MCP tool: {tool_name}",
                code="mcp_tool_unavailable",
                outcome="blocked",
            ),
            None,
            "",
            {},
        )

    missing_env = [ref for ref in server.env_refs if not env.get(ref)]
    if missing_env:
        return (
            RuntimeToolResult(
                ok=False,
                content=f"MCP server '{server.server_id}' is missing required env refs: {', '.join(missing_env)}",
                code="mcp_missing_env",
                outcome="blocked",
            ),
            None,
            "",
            {},
        )

    return None, server, raw_tool_name, _env_for_server(server, env)


def execute_mcp_tool_call(
    tenant,
    servers: list[McpServerConfig],
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    handlers: dict[str, McpHandler],
    env: dict[str, str],
) -> RuntimeToolResult:
    args = args or {}
    error, server, raw_tool_name, server_env = _prepare_mcp_call(tenant, servers, tool_name, env)
    if error is not None:
        return error
    assert server is not None

    handler = handlers.get(server.server_id)
    if handler is None:
        return RuntimeToolResult(
            ok=False,
            content=f"MCP server '{server.server_id}' has no configured call handler.",
            code="mcp_server_unavailable",
            outcome="retryable_error",
        )

    try:
        result = handler(raw_tool_name, args, server_env)
    except Exception as exc:
        return RuntimeToolResult(
            ok=False,
            content=f"Error executing MCP tool '{tool_name}': {exc}",
            code="tool_bridge_error",
            outcome="retryable_error",
        )

    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        return RuntimeToolResult(
            ok=False,
            content=f"Async MCP tool '{tool_name}' must be executed by the async runtime worker.",
            code="async_tool_requires_worker",
            outcome="retryable_error",
        )
    return _normalize_mcp_result(result, tool_name=tool_name)


async def execute_mcp_tool_call_async(
    tenant,
    servers: list[McpServerConfig],
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    handlers: dict[str, McpHandler],
    env: dict[str, str],
) -> RuntimeToolResult:
    args = args or {}
    error, server, raw_tool_name, server_env = _prepare_mcp_call(tenant, servers, tool_name, env)
    if error is not None:
        return error
    assert server is not None

    handler = handlers.get(server.server_id)
    if handler is None:
        return RuntimeToolResult(
            ok=False,
            content=f"MCP server '{server.server_id}' has no configured call handler.",
            code="mcp_server_unavailable",
            outcome="retryable_error",
        )

    try:
        result = handler(raw_tool_name, args, server_env)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        return RuntimeToolResult(
            ok=False,
            content=f"Error executing MCP tool '{tool_name}': {exc}",
            code="tool_bridge_error",
            outcome="retryable_error",
        )
    return _normalize_mcp_result(result, tool_name=tool_name)


def build_mcp_runtime_toolset(
    tenant,
    servers: list[McpServerConfig],
    server_tools: dict[str, list[dict[str, Any]]],
    *,
    handlers: dict[str, McpHandler],
    env: dict[str, str],
) -> RuntimeToolset:
    discovered = discover_mcp_tools(tenant, servers, server_tools)
    runtime_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    for schema in discovered.tools:
        tool_name = schema.name

        async def call(args: dict[str, Any], *, _tool_name: str = tool_name) -> RuntimeToolResult:
            return await execute_mcp_tool_call_async(
                tenant,
                servers,
                _tool_name,
                args,
                handlers=handlers,
                env=env,
            )

        runtime_handlers[tool_name] = call

    return RuntimeToolset(
        tools=discovered.tools,
        handlers=runtime_handlers,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
    )
