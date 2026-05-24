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


def test_credential_policy_selects_only_available_same_tenant_key():
    from app.hermes_runtime.credential_policy import (
        ProviderCredential,
        select_available_credential,
    )

    candidates = [
        ProviderCredential(
            tenant_id="pm-bot",
            provider="gemini",
            credential_id="base",
            secret_ref="tenant:pm-bot:llm_api_key",
        ),
        ProviderCredential(
            tenant_id="pm-bot",
            provider="gemini",
            credential_id="backup",
            secret_ref="tenant:pm-bot:llm_api_key:backup",
        ),
        ProviderCredential(
            tenant_id="code-bot",
            provider="gemini",
            credential_id="global-looking",
            secret_ref="tenant:code-bot:llm_api_key",
        ),
    ]

    selected = select_available_credential(
        "pm-bot",
        "gemini",
        candidates,
        exhausted_credential_ids={"base"},
    )

    assert selected is not None
    assert selected.credential_id == "backup"
    assert selected.tenant_id == "pm-bot"

    no_cross_tenant_fallback = select_available_credential(
        "pm-bot",
        "gemini",
        candidates,
        exhausted_credential_ids={"base", "backup"},
    )

    assert no_cross_tenant_fallback is None
