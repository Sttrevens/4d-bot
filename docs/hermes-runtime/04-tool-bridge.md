# 04 Tool Bridge

## Purpose

Hermes must be able to call our production tools without bypassing our policy system. ToolBridge converts our existing tool registry and tenant allowlists into a Hermes-compatible toolset and executes calls through existing handlers.

## Existing Sources

- Tool maps: `app/services/base_agent.py` `ALL_TOOL_MAP`
- Tool definitions: `app/services/base_agent.py` `_ALL_TOOL_DEFS`
- Tenant filtering: `_get_tenant_tools`
- Lazy groups: `_TOOL_GROUPS`, `_GROUP_DESCRIPTIONS`, `_expand_tool_group`
- Tool result type: `app/tools/tool_result.py`
- Tool tracking: `app/services/tool_tracker.py`
- Latency tracing: `app/services/latency_trace.py`

## New Files

- `app/hermes_runtime/tool_bridge.py`
- `app/hermes_runtime/tool_policy.py`
- `tests/test_hermes_tool_bridge.py`

## Bridge Responsibilities

1. Build a tenant-scoped toolset from existing definitions.
2. Convert our tool schema to the shape Hermes expects.
3. Inject tenant metadata for custom tools, skill tools, and tools requiring current tenant context.
4. Apply pre-call policy gates before tool execution.
5. Execute the existing handler.
6. Normalize `ToolResult` or raw returns into a Hermes tool result message.
7. Emit trace, metering, and tool tracker events.
8. Return ordered results even when Hermes requests concurrent execution.

## Permission Envelope

Hermes receives:

```json
{
  "allowed_tool_names": ["think", "export_file", "read_agent_skill_file"],
  "allowed_tool_groups": ["core", "extension"],
  "restricted_tool_groups": ["devops", "admin"],
  "admin": false,
  "self_iteration_enabled": false,
  "platform": "feishu"
}
```

ToolBridge must reject any tool not present in `allowed_tool_names`, even if Hermes has a stale schema for it.

## Side-Effect Classes

| Class | Examples | Policy |
|---|---|---|
| Read-only | `read_file`, `web_search`, `read_agent_skill_file` | Allowed when in tool allowlist |
| User-visible write | `send_message`, `reply_feishu_message`, `export_file` | Allowed with delivery evidence recording |
| External mutation | calendar/task/doc creation | Requires platform and tenant permission |
| Code mutation | `self_write_file`, `edit_file`, `commit_batch` | Requires admin/self-iteration boundary |
| Infrastructure mutation | deploy/restart/destroy | Requires explicit admin permission and confirmation |
| Terminal/code execution | sandbox/browser/Lark CLI | Requires backend policy and time budget |

## Result Normalization

Every tool result returned to Hermes must include:

```json
{
  "ok": true,
  "content": "human-readable result",
  "structured": {},
  "retry_hint": "",
  "side_effect": false,
  "artifact_ids": [],
  "duration_ms": 42
}
```

Long outputs must be summarized with the same budget rules used by `base_agent` today. Full raw outputs stay in logs or artifact storage, not in the model context.

## Concurrent Execution

Hermes supports concurrent tool calls. ToolBridge must classify tools before allowing parallel execution:

- Read-only tools may run concurrently.
- Independent external reads may run concurrently.
- Writes to the same platform object must run sequentially.
- Code mutations, deployment actions, reminders, and customer/provisioning mutations run sequentially.
- If safety is unknown, execute sequentially.

Add:

```python
def tool_concurrency_key(tool_name: str, args: dict) -> str:
    ...
```

Tools with the same non-empty key are serialized.

## Lazy Tool Groups

Hermes runtime should use a small initial toolset:

- Always include core conversation and answer tools.
- Include `request_more_tools`-style expansion if the runtime asks for a group.
- Never allow lazy loading of restricted groups from the model alone.
- For `extension`, include both old skill tools and new repo-skill management tools after the repo-skill branch is merged.

## Tests

- A tenant with `tools_enabled=[]` receives all non-restricted tools.
- A tenant with a whitelist receives only explicitly allowed tools.
- `devops` and `admin` groups cannot be loaded through model-requested expansion.
- Unknown tool calls return a structured denial, not a Python exception.
- ToolResult success, invalid param, retry hint, and artifact results normalize correctly.
- Read-only concurrent calls preserve original tool-call order in returned results.
- Two writes to the same calendar/doc/customer object are serialized.
