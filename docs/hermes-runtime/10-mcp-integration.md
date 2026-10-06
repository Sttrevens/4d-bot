# 10 MCP Integration

## Purpose

Hermes treats MCP as a first-class extension surface. Our bot already has many native tools and Lark CLI skills. MCP should be introduced as a tenant-scoped, lazy-loaded extension layer, not as an unrestricted global tool pool.

## New Files

- `app/hermes_runtime/mcp_bridge.py`
- `app/hermes_runtime/mcp_registry.py`
- `tests/test_hermes_mcp_bridge.py`

## Registry Shape

```json
{
  "server_id": "github",
  "label": "GitHub",
  "transport": "stdio",
  "command": "mcp-github",
  "args": [],
  "env_refs": ["GITHUB_TOKEN"],
  "tool_prefix": "mcp_github",
  "default_enabled": false,
  "allowed_tenants": ["code-bot"],
  "risk_level": "yellow",
  "description": "GitHub repo, issue, and PR operations"
}
```

Registry can be stored in a static config file first and later moved to admin dashboard.

## Tenant Controls

Add tenant config:

```python
mcp_enabled: bool = False
mcp_servers_enabled: list[str] = field(default_factory=list)
mcp_tool_allowlist: list[str] = field(default_factory=list)
```

Rules:

- MCP is disabled by default.
- Server must be listed in tenant config.
- Exposed MCP tools must pass tenant allowlist and risk checks.
- MCP secrets are resolved per tenant.
- MCP tool names are prefixed to avoid collisions with native tools.

## Lazy Loading

MCP tool schemas should not be injected into every prompt:

1. Initial runtime context lists available MCP servers by short description.
2. Runtime calls `request_more_tools` or MCP discovery bridge for a server.
3. Bridge returns tool schemas for that server only.
4. ToolBridge executes selected MCP tool calls and records results in the same ledger.

## Interaction With Native Tools

Native tools remain preferred for production-critical flows:

- Feishu docs/calendar/tasks use native or `lark_cli_*` tools first.
- GitHub repo operations use existing `github_ops` unless MCP gives a clear missing capability.
- Browser automation stays in existing browser tools.

MCP adds breadth, not a replacement for hardened business integrations.

## Security Requirements

- No MCP server can access all tenant secrets.
- MCP process environment includes only declared `env_refs`.
- MCP tool output is subject to context budget truncation.
- MCP server startup, shutdown, and crash events are logged by `run_id`.
- MCP servers cannot be installed by a model without admin approval.

## Tests

- Tenant with `mcp_enabled=false` sees no MCP servers.
- Tenant sees only configured server IDs.
- MCP tool names are prefixed and cannot collide with native tools.
- Missing env refs prevent server startup and return a structured error.
- MCP result normalization matches native tool result normalization.
- Server crash is reported as `tool_bridge_error` with retry policy.
