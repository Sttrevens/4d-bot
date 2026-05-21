from __future__ import annotations

import os

from app.hermes_runtime.types import RuntimeRequest, RuntimeResponse

UPSTREAM_HERMES_SHA = "c6a992e3e3cb99d935da3d059093b5b1f839738c"


async def run_runtime_turn(request: RuntimeRequest) -> RuntimeResponse:
    """Local sidecar worker bootstrap.

    The adapter is deployable before a real Hermes worker is configured. When
    disabled, it returns a retryable failure so callers can fall back to the
    legacy provider instead of exposing partial runtime behavior to users.
    """
    if os.getenv("HERMES_RUNTIME_EXECUTE_LOCAL", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return RuntimeResponse.failed(
            request.run_id,
            "runtime_unavailable",
            "Hermes runtime sidecar is not configured in this deployment.",
            retryable=True,
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
    return {
        "enabled": os.getenv("HERMES_RUNTIME_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"},
        "source_sha": os.getenv("HERMES_RUNTIME_SOURCE_SHA", UPSTREAM_HERMES_SHA),
        "sidecar_status": "configured" if os.getenv("HERMES_RUNTIME_EXECUTE_LOCAL") else "not_configured",
    }
