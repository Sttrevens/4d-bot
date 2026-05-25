# 14 Deployment Runbook

## Purpose

Deploy Hermes runtime safely inside the existing production-first repository. Local edits and unpushed commits do not affect live bot behavior. Production changes deploy only after merge to `main`.

## Default State

All tenants remain legacy by default:

```json
{
  "agent_runtime": "legacy",
  "hermes_runtime_enabled": false,
  "hermes_runtime_shadow": false,
  "hermes_runtime_rollout_percent": 0
}
```

This ensures the integration can ship dormant before tenant rollout.

## Environment

Required runtime environment once sidecar is implemented:

```text
HERMES_RUNTIME_ENABLED=1
HERMES_RUNTIME_MODE=sidecar
HERMES_RUNTIME_TIMEOUT_SECONDS=180
HERMES_RUNTIME_MAX_CONCURRENCY=4
HERMES_RUNTIME_SOURCE_SHA=c6a992e3e3cb99d935da3d059093b5b1f839738c
HERMES_RUNTIME_HOST=127.0.0.1
HERMES_RUNTIME_PORT=8765
HERMES_RUNTIME_URL=http://127.0.0.1:8765
HERMES_UPSTREAM_API_URL=http://127.0.0.1:8642
HERMES_UPSTREAM_MODEL=hermes-agent
HERMES_UPSTREAM_API_KEY=
HERMES_UPSTREAM_TIMEOUT_SECONDS=180
```

Secrets continue to use existing tenant config and environment resolution. Do not create a global Hermes key that all tenants share unless it is explicitly configured as a fallback and metered separately.

## Sidecar Service

The dormant sidecar service entrypoint is:

```bash
python -m app.hermes_runtime.sidecar_app
```

It exposes:

```text
GET /health
POST /v1/runtime/turn
```

`POST /v1/runtime/turn` accepts the serialized `RuntimeRequest` contract and returns a serialized `RuntimeResponse`. Until a real upstream Hermes loop is configured, the worker remains fail-closed so the production app can fall back to legacy routing.

When `HERMES_RUNTIME_EXECUTE_LOCAL=true` and `HERMES_UPSTREAM_API_URL` is set, the sidecar worker posts to the upstream Hermes OpenAI-compatible `/v1/chat/completions` endpoint. The adapter passes `X-Hermes-Session-Id` from `RuntimeRequest.conversation.history_key` and `X-Hermes-Session-Key` from `RuntimeRequest.sender.identity_id` or `sender_id`.

## Pre-Deploy Checklist

- Focused runtime tests pass.
- Existing skill and tool group tests pass.
- Existing route integration tests pass.
- No runtime tenant config enables Hermes user-visible mode by accident.
- Sidecar health check passes locally.
- Shadow mode blocks side-effect tools.
- Admin runtime events are redacted.
- Commit notes include `PORT_TO_4D_BOT` or a defer reason.

## Branch CI

For this repo:

- Branch pushes such as `codex/**` trigger sanity checks.
- Production deployment does not happen on branch push.
- Merge to `main` triggers deploy workflow.

Expected branch workflow:

```bash
git push origin codex/hermes-runtime-design
```

Then inspect GitHub Actions for branch sanity.

## Production Rollout Steps

1. Merge dormant runtime code to `main`.
2. Confirm deploy workflow finishes.
3. Confirm all tenants still have `agent_runtime=legacy`.
4. Enable shadow mode for `pm-bot` only.
5. Review runtime events and shadow comparisons.
6. Enable 10 percent visible rollout for `pm-bot`.
7. Increase to 50 percent after SLOs hold.
8. Increase to 100 percent for `pm-bot`.
9. Repeat with `code-bot` only after code execution policy passes.
10. Keep customer-facing tenants legacy until internal tenants pass.

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

Code rollback:

1. Revert the runtime integration commit or disable import path through env.
2. Push revert to `main`.
3. Confirm deploy workflow finishes.
4. Confirm runtime selector returns legacy for all tenants.

Side-effect rollback:

- Use checkpoint ledger for file/code mutations.
- Use platform object ids for calendar/task/doc repair where available.
- Message sends are irreversible and should be handled with a corrective follow-up message.

## Runtime Health Checks

Add endpoint or admin API:

```text
GET /admin/api/runtime/hermes/health
GET /admin/api/runtime/hermes/adoption
GET /admin/api/runtime/hermes/shadow-qa
GET /admin/api/runtime/hermes/{tenant_id}/runs/{run_id}
GET /admin/api/runtime/hermes/{tenant_id}/runs/{run_id}/events
GET /admin/api/runtime/hermes/{tenant_id}/runs/{run_id}/shadow
```

Use the adoption endpoint to compare visible Hermes, legacy, and shadow traffic by tenant. Use run detail and shadow QA before moving a tenant from shadow mode to visible rollout.

Sidecar HTTP checks once the sidecar service is packaged and running:

```text
GET http://127.0.0.1:8765/health
POST http://127.0.0.1:8765/v1/runtime/turn
```

When `HERMES_RUNTIME_URL` is set, the admin health endpoint probes `<url>/health` and returns `sidecar_url`, `sidecar_status`, and the sidecar-reported source/last-success fields. If the probe fails, `sidecar_status` is `unreachable`; visible Hermes traffic must remain disabled or fall back to legacy until the probe recovers.

Local sidecar process:

```bash
python -m app.hermes_runtime.sidecar_app
```

Sidecar HTTP checks:

```text
GET http://127.0.0.1:8765/health
POST http://127.0.0.1:8765/v1/runtime/turn
```

When `HERMES_RUNTIME_URL` is set, the admin health endpoint probes `<url>/health` and returns `sidecar_url`, `sidecar_status`, and the sidecar-reported source/last-success fields. If the probe fails, `sidecar_status` is `unreachable` and visible Hermes traffic must remain disabled or fall back to legacy.

Response:

```json
{
  "enabled": true,
  "source_sha": "c6a992e3e3cb99d935da3d059093b5b1f839738c",
  "sidecar_status": "ok",
  "last_success_at": "2026-05-21T12:00:00Z",
  "last_error": ""
}
```

## Incident Signals

Page or alert on:

- Runtime unavailable for more than 5 consecutive Hermes-enabled turns.
- Tool policy bypass attempt.
- Tenant isolation assertion failure.
- Side-effect unknown state.
- Shadow severe regression rate over threshold.
- Provider exhaustion across all tenant credentials.
