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
