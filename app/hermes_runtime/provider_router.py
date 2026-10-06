from __future__ import annotations

from app.hermes_runtime.credential_policy import build_credential_candidates, credential_candidate_dicts


def build_provider_config(tenant, *, run_id: str) -> dict:
    tenant_id = getattr(tenant, "tenant_id", "")
    provider = getattr(tenant, "llm_provider", "") or "gemini"
    credential_candidates = build_credential_candidates(
        tenant_id,
        provider,
        getattr(tenant, "hermes_provider_credential_refs", None),
    )
    return {
        "tenant_id": tenant_id,
        "provider": provider,
        "base_model": getattr(tenant, "llm_model", "") or "",
        "strong_model": getattr(tenant, "llm_model_strong", "") or "",
        "base_url": getattr(tenant, "llm_base_url", "") or "",
        "credential_ref": f"tenant:{tenant_id}:llm_api_key",
        "credential_candidates": credential_candidate_dicts(credential_candidates),
        "allow_fallback": True,
        "fallbacks": [
            {
                "provider": provider,
                "model": getattr(tenant, "llm_model_strong", "") or "",
                "reason": "complex_tool_loop",
            }
        ] if getattr(tenant, "llm_model_strong", "") else [],
        "metering": {"run_id": run_id, "quota_checked": True},
    }
