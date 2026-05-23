from app.tenant.config import TenantConfig


def test_provider_router_uses_tenant_models_without_raw_key():
    from app.hermes_runtime.provider_router import build_provider_config

    tenant = TenantConfig(
        tenant_id="pm-bot",
        llm_provider="gemini",
        llm_model="gemini-3-flash-preview",
        llm_model_strong="gemini-3.1-pro-preview-customtools",
        llm_api_key="secret-key",
    )

    config = build_provider_config(tenant, run_id="run-1")

    assert config["provider"] == "gemini"
    assert config["base_model"] == "gemini-3-flash-preview"
    assert config["strong_model"] == "gemini-3.1-pro-preview-customtools"
    assert config["credential_ref"] == "tenant:pm-bot:llm_api_key"
    assert "secret-key" not in str(config)


def test_credential_policy_scopes_pool_by_tenant():
    from app.hermes_runtime.credential_policy import credential_pool_key

    assert credential_pool_key("tenant-a", "gemini") != credential_pool_key("tenant-b", "gemini")
    assert credential_pool_key("tenant-a", "gemini") == "hermes:tenant-a:provider:gemini:pool"


def test_credential_policy_skips_exhausted_credentials_inside_tenant():
    from app.hermes_runtime.credential_policy import (
        CredentialCandidate,
        CredentialExhaustion,
        select_credential_candidate,
    )

    exhausted = CredentialExhaustion()
    exhausted.mark_exhausted(
        tenant_id="tenant-a",
        provider="gemini",
        credential_id="primary",
        reason="quota_exceeded",
    )
    candidates = [
        CredentialCandidate(
            tenant_id="tenant-a",
            provider="gemini",
            model="gemini-3-flash-preview",
            credential_ref="tenant:tenant-a:llm_api_key",
            credential_id="primary",
        ),
        CredentialCandidate(
            tenant_id="tenant-a",
            provider="gemini",
            model="gemini-3-flash-preview",
            credential_ref="tenant:tenant-a:llm_api_key_2",
            credential_id="backup",
        ),
        CredentialCandidate(
            tenant_id="tenant-b",
            provider="gemini",
            model="gemini-3-flash-preview",
            credential_ref="tenant:tenant-b:llm_api_key",
            credential_id="other-tenant",
        ),
    ]

    selection = select_credential_candidate(
        candidates,
        tenant_id="tenant-a",
        provider="gemini",
        exhaustion=exhausted,
    )

    assert selection.ok is True
    assert selection.candidate is not None
    assert selection.candidate.tenant_id == "tenant-a"
    assert selection.candidate.credential_id == "backup"
    assert selection.error_code == ""


def test_credential_policy_reports_provider_exhausted_without_cross_tenant_fallback():
    from app.hermes_runtime.credential_policy import (
        CredentialCandidate,
        CredentialExhaustion,
        select_credential_candidate,
    )

    exhausted = CredentialExhaustion()
    exhausted.mark_exhausted(
        tenant_id="tenant-a",
        provider="gemini",
        credential_id="primary",
        reason="quota_exceeded",
    )
    candidates = [
        CredentialCandidate(
            tenant_id="tenant-a",
            provider="gemini",
            model="gemini-3-flash-preview",
            credential_ref="tenant:tenant-a:llm_api_key",
            credential_id="primary",
        ),
        CredentialCandidate(
            tenant_id="tenant-b",
            provider="gemini",
            model="gemini-3-flash-preview",
            credential_ref="tenant:tenant-b:llm_api_key",
            credential_id="other-tenant",
        ),
    ]

    selection = select_credential_candidate(
        candidates,
        tenant_id="tenant-a",
        provider="gemini",
        exhaustion=exhausted,
    )

    assert selection.ok is False
    assert selection.candidate is None
    assert selection.error_code == "provider_exhausted"
    assert "tenant-a" in selection.message
