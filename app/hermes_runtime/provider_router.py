from __future__ import annotations


def build_provider_config(tenant, *, run_id: str) -> dict:
    tenant_id = getattr(tenant, "tenant_id", "")
    return {
        "tenant_id": tenant_id,
        "provider": getattr(tenant, "llm_provider", "") or "gemini",
        "base_model": getattr(tenant, "llm_model", "") or "",
        "strong_model": getattr(tenant, "llm_model_strong", "") or "",
        "base_url": getattr(tenant, "llm_base_url", "") or "",
        "credential_ref": f"tenant:{tenant_id}:llm_api_key",
        "allow_fallback": True,
        "fallbacks": [
            {
                "provider": getattr(tenant, "llm_provider", "") or "gemini",
                "model": getattr(tenant, "llm_model_strong", "") or "",
                "reason": "complex_tool_loop",
            }
        ] if getattr(tenant, "llm_model_strong", "") else [],
        "metering": {"run_id": run_id, "quota_checked": True},
    }
