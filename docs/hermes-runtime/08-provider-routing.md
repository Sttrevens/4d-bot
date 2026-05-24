# 08 Provider Routing

## Purpose

Hermes supports provider registries, model switching, and credential pools. Our bot has tenant LLM config, Gemini default routing, strong-model escalation, quota, and metering. The runtime integration should adopt Hermes provider flexibility while preserving tenant quotas and billing records.

## Current Routing

`app/router/intent.py`:

- Applies tenant access, quota, rate-limit, trial, and per-user token quota.
- Routes text and multimodal requests.
- Defaults production tenants to Gemini.
- Records usage after provider execution.

This pre-runtime gate remains mandatory.

## New Files

- `app/hermes_runtime/provider_router.py`
- `app/hermes_runtime/credential_policy.py`
- `tests/test_hermes_provider_routing.py`

## Runtime Provider Config

Generate a Hermes provider envelope from tenant config:

```json
{
  "tenant_id": "pm-bot",
  "provider": "gemini",
  "base_model": "gemini-3-flash-preview",
  "strong_model": "gemini-3.1-pro-preview-customtools",
  "base_url": "",
  "credential_ref": "tenant:pm-bot:llm_api_key",
  "allow_fallback": true,
  "fallbacks": [
    {
      "provider": "gemini",
      "model": "gemini-3.1-pro-preview-customtools",
      "reason": "complex_tool_loop"
    }
  ],
  "metering": {
    "run_id": "tr_...",
    "quota_checked": true
  }
}
```

Do not pass raw API keys through logs or runtime events. The sidecar gets secrets through environment or a process-local secret resolver.

## Credential Pool Policy

Adopt Hermes credential pool behavior only after wrapping it with tenant boundaries:

- Pool entries are scoped by tenant and provider.
- Exhaustion state is scoped by tenant and credential id.
- Fallback across tenants is not allowed.
- Manual global fallback credentials require explicit operator configuration.
- Metering records the actual provider/model used.

## Model Escalation

Preserve current behavior:

- Start with tenant base model.
- Escalate to tenant strong model for complex tool loops, long context, code tasks, or repeated model failures.
- Record escalation reason in runtime events.
- If strong model is unavailable, continue with base model only when the task is still safe.

Hermes can make escalation decisions inside the runtime, but final metering must come back in `RuntimeResponse.usage`.

## Shadow Routing

Shadow mode:

1. Legacy runtime produces the user-visible answer.
2. Hermes receives the same sanitized request.
3. Hermes output is stored in `runtime:hermes:shadow:{run_id}`.
4. Benchmarks compare completion quality, tool calls, latency, and cost.

Shadow mode must not execute side-effecting tools. ToolBridge returns simulated read-only responses or denies side-effect calls in shadow runs.

## Tests

- Tenant base and strong models map into runtime provider config.
- Raw API keys do not appear in logs, events, or Redis run summaries.
- Provider exhaustion retries another credential only inside the same tenant scope.
- Shadow mode blocks side-effect tools.
- Usage returned from Hermes is recorded by existing metering code.
- Runtime fallback to legacy preserves quota and rate-limit decisions already made.
