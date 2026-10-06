# 02 Runtime Adapter Contract

## Purpose

The adapter contract prevents the production app from depending on Hermes internals. `route_message` should only know that it is calling an `AgentRuntime`. Legacy Gemini and Hermes sidecar must satisfy the same contract.

## Runtime Interface

Create:

- `app/hermes_runtime/types.py`
- `app/hermes_runtime/client.py`
- `app/hermes_runtime/selector.py`
- `tests/test_hermes_runtime_contract.py`

Primary Python surface:

```python
class AgentRuntime(Protocol):
    async def run_turn(self, request: RuntimeRequest) -> RuntimeResponse:
        ...
```

`HermesRuntimeClient` implements the protocol by calling a sidecar process or local service. `LegacyGeminiRuntime` wraps the current provider so the selector can compare both paths.

## RuntimeRequest

```json
{
  "run_id": "tr_20260521_abc123",
  "tenant_id": "pm-bot",
  "channel_id": "pm-bot-feishu",
  "platform": "feishu",
  "sender": {
    "sender_id": "ou_xxx",
    "sender_name": "吴天骄",
    "identity_id": "feishu:ou_xxx"
  },
  "conversation": {
    "history_key": "ou_xxx",
    "chat_id": "oc_xxx",
    "chat_type": "group",
    "mode": "safe",
    "fresh_start": false
  },
  "input": {
    "text": "帮我做一份瑞士风 PPT",
    "image_urls": [],
    "attachments": [],
    "chat_context": ""
  },
  "policy": {
    "allowed_tool_names": ["think", "export_file", "read_agent_skill_file"],
    "allowed_tool_groups": ["core", "extension"],
    "admin": true,
    "self_iteration_enabled": true,
    "side_effect_budget": "normal",
    "requires_confirmation_for": ["deploy", "send_external_message", "destructive_terminal"]
  },
  "runtime": {
    "profile": "default",
    "max_rounds": 12,
    "max_tool_result_chars": 20000,
    "context_strategy": "hermes_compressor",
    "memory_enabled": true,
    "skills_enabled": true,
    "mcp_enabled": false,
    "code_execution_backend": "docker"
  }
}
```

## RuntimeResponse

```json
{
  "run_id": "tr_20260521_abc123",
  "runtime": "hermes_sidecar",
  "status": "completed",
  "final_text": "已经生成瑞士风 PPT...",
  "artifacts": [
    {
      "artifact_id": "file_abc",
      "kind": "html",
      "filename": "swiss-deck.html",
      "delivery_hint": "send_file"
    }
  ],
  "tool_calls": [
    {
      "tool_name": "read_agent_skill_file",
      "status": "success",
      "duration_ms": 24,
      "side_effect": false
    }
  ],
  "usage": {
    "input_tokens": 1200,
    "output_tokens": 640,
    "api_calls": 3,
    "tool_calls": 2
  },
  "events": [
    {
      "ts": "2026-05-21T12:00:00Z",
      "level": "info",
      "event": "runtime.tool.completed",
      "message": "read_agent_skill_file completed"
    }
  ],
  "resume": {
    "resumable": false,
    "resume_token": ""
  },
  "error": null
}
```

## Status Values

| Status | Meaning | User Handling |
|---|---|---|
| `completed` | Final answer is ready | Send `final_text` and artifacts |
| `partial` | Runtime made progress but did not finish | Send progress/fallback and persist resume |
| `needs_confirmation` | Runtime requests explicit user approval | Ask confirmation through existing channel |
| `blocked` | Policy denied an action | Explain the denied action briefly |
| `failed` | Runtime failed before useful output | Fallback to legacy if enabled |
| `timed_out` | Runtime exceeded request budget | Use existing timeout continuation path |

## Error Taxonomy

| Code | Description | Retry |
|---|---|---|
| `runtime_unavailable` | Sidecar missing or unhealthy | Retry legacy, alert operator |
| `runtime_bad_request` | Adapter built invalid request | Do not retry; fix code |
| `policy_denied` | Tool/action not allowed | Do not retry unless user/admin changes permission |
| `tool_bridge_error` | Our tool handler raised unexpectedly | Retry only if tool result says retryable |
| `provider_exhausted` | Model credentials exhausted | Retry alternate credential/model |
| `context_overflow` | Context engine failed to fit prompt | Retry with stronger compression |
| `side_effect_unknown` | Runtime lost response after side effect | Pause and request operator review |

## Selector Rules

`app/hermes_runtime/selector.py` must expose:

```python
def select_runtime(tenant, sender_id: str, run_id: str) -> str:
    ...
```

Rules:

1. If `tenant.agent_runtime == "legacy"`, return `legacy`.
2. If `tenant.hermes_runtime_enabled` is false, return `legacy`.
3. If `tenant.hermes_runtime_shadow` is true, return `legacy_shadow_hermes`.
4. If rollout percent is `0`, return `legacy`.
5. If rollout percent is `100`, return `hermes_sidecar`.
6. For partial rollout, hash `tenant_id + sender_id` into `0..99`.

## Backward Compatibility

No handler changes are allowed beyond passing through the existing `run_id`, `resume_token`, inbox, and channel context. The runtime adapter must return a plain final string path compatible with current delivery.

## Contract Tests

Add tests that assert:

- Legacy tenants select legacy runtime.
- Hermes-enabled tenants select Hermes runtime.
- Shadow mode returns both legacy output and Hermes telemetry without delivering Hermes text.
- Runtime request contains tenant-scoped allowlisted tools only.
- Runtime response with artifacts is converted into existing delivery structures.
- `runtime_unavailable` falls back to legacy when fallback is enabled.
