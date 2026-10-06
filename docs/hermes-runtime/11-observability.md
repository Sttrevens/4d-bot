# 11 Observability

## Purpose

Hermes runtime must be easier to debug than the current provider loop, not harder. Every runtime turn needs one run ID that connects platform ingress, runtime events, tool calls, memory activity, artifacts, metering, and delivery.

## Existing Sources

- Task run: `app/services/task_run.py`
- Latency trace: `app/services/latency_trace.py`
- Tool tracker: `app/services/tool_tracker.py`
- Error log: `app/services/error_log.py`
- Artifact registry: `app/services/artifact_registry.py`
- Metering: `app/services/metering.py`
- Admin logs: `app/admin/routes.py`

## New Files

- `app/hermes_runtime/observability.py`
- `app/hermes_runtime/events.py`
- `tests/test_hermes_observability.py`

## Event Schema

```json
{
  "run_id": "tr_20260521_abc123",
  "tenant_id": "pm-bot",
  "channel_id": "pm-bot-feishu",
  "session_id": "hr:pm-bot:pm-bot-feishu:dm:abc:self",
  "runtime": "hermes_sidecar",
  "event": "runtime.tool.completed",
  "level": "info",
  "message": "export_file completed",
  "payload": {
    "tool_name": "export_file",
    "duration_ms": 340,
    "side_effect": true
  },
  "ts": "2026-05-21T12:00:00Z"
}
```

## Required Event Names

| Event | When |
|---|---|
| `runtime.selected` | Selector chooses legacy, shadow, or Hermes |
| `runtime.started` | Hermes run begins |
| `runtime.model.selected` | Provider and model chosen |
| `runtime.context.compressed` | Context engine compacts messages |
| `runtime.skill.activated` | Skill activation card is injected |
| `runtime.tool.started` | ToolBridge starts a tool call |
| `runtime.tool.completed` | Tool call finishes successfully |
| `runtime.tool.denied` | Policy denies a tool call |
| `runtime.memory.prefetched` | Memory bridge returns recall context |
| `runtime.memory.write` | Memory bridge persists a write |
| `runtime.checkpoint.created` | Checkpoint is created before mutation |
| `runtime.subagent.started` | Subagent starts |
| `runtime.subagent.completed` | Subagent completes |
| `runtime.artifact.created` | Artifact registered |
| `runtime.completed` | Run completed |
| `runtime.partial` | Run paused or timed out with partial progress |
| `runtime.failed` | Runtime failed |

## Dashboard Views

Admin dashboard should eventually expose:

1. Runtime adoption by tenant.
2. Legacy vs Hermes success rate.
3. Average tool-call count and latency.
4. Side-effect ledger by run.
5. Skill activation frequency.
6. Memory recall/write frequency and scope.
7. Provider/model usage and fallback reasons.
8. Shadow-mode quality comparison.

Initial implementation can expose these through JSON admin endpoints before UI work.

## SLOs

| Metric | Target |
|---|---|
| Sidecar startup failure | Under 1 percent of Hermes-enabled turns |
| Runtime timeout rate | No worse than legacy baseline |
| Tool policy bypass | Zero |
| Tenant isolation incident | Zero |
| Shadow answer severe regression | Under 5 percent before production cutover |
| Missing run telemetry | Under 1 percent |

## Redaction

Events must redact:

- API keys, OAuth tokens, webhook secrets.
- Raw user IDs when not needed for debugging.
- Raw file contents unless artifact inspection is explicitly requested.
- Full memory profile contents in general logs.

Admin memory diagnostics can show memory content because they already require admin token.

## Tests

- Runtime events include `run_id`, `tenant_id`, `runtime`, and timestamp.
- Secret-looking values are redacted from runtime event payloads.
- ToolBridge emits started and completed/denied events.
- Metering records provider/model returned by Hermes.
- Shadow run telemetry is stored without delivering shadow text.
- A failed runtime produces a `runtime.failed` event and error log entry.
