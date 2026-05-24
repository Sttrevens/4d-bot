# 14 Deployment Runbook

## Purpose

Run the Hermes runtime sidecar locally or in a branch deployment while keeping the default bot path on the legacy runtime until explicitly enabled.

## Default State

Tenants should remain legacy by default:

```json
{
  "agent_runtime": "legacy",
  "hermes_runtime_enabled": false,
  "hermes_runtime_shadow": false,
  "hermes_runtime_rollout_percent": 0
}
```

## Environment

Required runtime environment once the sidecar is enabled:

```text
HERMES_RUNTIME_ENABLED=1
HERMES_RUNTIME_MODE=sidecar
HERMES_RUNTIME_HOST=127.0.0.1
HERMES_RUNTIME_PORT=8765
HERMES_RUNTIME_URL=http://127.0.0.1:8765
HERMES_RUNTIME_TIMEOUT_SECONDS=180
HERMES_RUNTIME_MAX_CONCURRENCY=4
HERMES_RUNTIME_SOURCE_SHA=c6a992e3e3cb99d935da3d059093b5b1f839738c
```

Secrets should continue to resolve through tenant configuration. Avoid global provider credentials unless they are intentionally configured as a metered fallback.

## Pre-Deploy Checklist

- Focused runtime tests pass.
- Route integration tests pass.
- No tenant config enables visible Hermes mode by accident.
- Sidecar health check passes locally.
- Shadow mode blocks side-effect tools.
- Runtime events are redacted.

## Local Sidecar

Start the local sidecar process:

```bash
python -m app.hermes_runtime.sidecar_app
```

Check the sidecar:

```text
GET http://127.0.0.1:8765/health
POST http://127.0.0.1:8765/v1/runtime/turn
```

Expected health shape:

```json
{
  "enabled": true,
  "source_sha": "c6a992e3e3cb99d935da3d059093b5b1f839738c",
  "sidecar_status": "configured",
  "last_success_at": "",
  "last_error": ""
}
```

## Rollback

Fast config rollback:

```json
{
  "agent_runtime": "legacy",
  "hermes_runtime_enabled": false,
  "hermes_runtime_shadow": false,
  "hermes_runtime_rollout_percent": 0
}
```

For code rollback, revert the runtime integration commit or disable the runtime import path through environment configuration, then verify the selector returns legacy for all tenants.

## Incident Signals

- Runtime unavailable for more than 5 consecutive Hermes-enabled turns.
- Tool policy bypass attempt.
- Tenant isolation assertion failure.
- Side-effect unknown state.
- Shadow severe regression rate over threshold.
- Provider exhaustion across all tenant credentials.
