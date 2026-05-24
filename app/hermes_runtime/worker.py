from __future__ import annotations

import os
from typing import Any

from app.hermes_runtime.types import RuntimeRequest, RuntimeResponse, RuntimeToolCallRecord, RuntimeUsage

UPSTREAM_HERMES_SHA = "c6a992e3e3cb99d935da3d059093b5b1f839738c"


def _configured_for_local_execution() -> bool:
    return os.getenv("HERMES_RUNTIME_EXECUTE_LOCAL", "").strip().lower() in {"1", "true", "yes", "on"}


def _upstream_timeout_seconds() -> int:
    raw = os.getenv("HERMES_UPSTREAM_TIMEOUT_SECONDS") or os.getenv("HERMES_RUNTIME_TIMEOUT_SECONDS") or "180"
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 180


def _planned_tool_calls(request: RuntimeRequest) -> list[dict[str, Any]]:
    """Extract the test/sidecar handoff shape without making it user-facing API."""
    for attachment in request.input.attachments:
        if not isinstance(attachment, dict):
            continue
        if attachment.get("kind") != "hermes_tool_calls":
            continue
        calls = attachment.get("tool_calls")
        if not isinstance(calls, list):
            return []
        return [call for call in calls if isinstance(call, dict)]
    return []


def _load_tenant(tenant_id: str):
    from app.tenant.registry import tenant_registry

    return tenant_registry.get(tenant_id) or tenant_registry.get_default()


async def run_runtime_turn(request: RuntimeRequest) -> RuntimeResponse:
    """Local sidecar worker bootstrap.

    The adapter is deployable before a real Hermes worker is configured. When
    disabled, it returns a retryable failure so callers can fall back to the
    legacy provider instead of exposing partial runtime behavior to users.
    """
    if not _configured_for_local_execution():
        return RuntimeResponse.failed(
            request.run_id,
            "runtime_unavailable",
            "Hermes runtime sidecar is not configured in this deployment.",
            retryable=True,
        )

    from app.hermes_runtime import upstream_api

    if upstream_api.upstream_api_url():
        return await upstream_api.run_upstream_turn(
            request,
            timeout_seconds=_upstream_timeout_seconds(),
        )

    planned_calls = _planned_tool_calls(request)
    if planned_calls:
        from app.hermes_runtime.tool_bridge import build_runtime_toolset, execute_tool_calls

        tenant = _load_tenant(request.tenant_id)
        toolset = build_runtime_toolset(tenant, user_text=request.input.text)
        results = await execute_tool_calls(toolset, request.policy, planned_calls)
        records = [
            RuntimeToolCallRecord(
                tool_name=str(call.get("tool_name") or call.get("name") or ""),
                status="success" if result.ok else "failed",
                duration_ms=result.duration_ms,
                side_effect=result.side_effect,
                code=result.code,
            )
            for call, result in zip(planned_calls, results, strict=False)
        ]
        final_text = "\n".join(
            f"{record.tool_name}: {result.content}" if record.tool_name else result.content
            for record, result in zip(records, results, strict=False)
        )
        return RuntimeResponse(
            run_id=request.run_id,
            status="completed",
            final_text=final_text,
            tool_calls=records,
            usage=RuntimeUsage(tool_calls=len(results)),
        )

    return RuntimeResponse(
        run_id=request.run_id,
        status="completed",
        final_text=(
            "Hermes runtime adapter is enabled, but no upstream Hermes loop is "
            "configured for local execution."
        ),
    )


def health() -> dict:
    from app.hermes_runtime.observability import health_state

    enabled = os.getenv("HERMES_RUNTIME_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    execute_local = os.getenv("HERMES_RUNTIME_EXECUTE_LOCAL", "").strip().lower() in {"1", "true", "yes", "on"}
    observed = health_state()
    if observed.get("last_error"):
        sidecar_status = "error"
    elif execute_local:
        sidecar_status = "configured"
    else:
        sidecar_status = "not_configured"
    return {
        "enabled": enabled,
        "source_sha": os.getenv("HERMES_RUNTIME_SOURCE_SHA", UPSTREAM_HERMES_SHA),
        "sidecar_status": sidecar_status,
        "last_success_at": observed.get("last_success_at", ""),
        "last_error": observed.get("last_error", ""),
        "last_error_at": observed.get("last_error_at", ""),
    }
