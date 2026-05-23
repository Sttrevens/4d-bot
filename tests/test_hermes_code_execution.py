from app.tenant.config import TenantConfig


def test_execution_policy_defaults_to_no_execution_for_customer_tenant():
    from app.hermes_runtime.execution_policy import build_execution_policy

    tenant = TenantConfig(tenant_id="kf-steven-ai", platform="wecom_kf")

    policy = build_execution_policy(tenant, admin=False)

    assert policy.backend == "none"
    assert policy.allow_file_write is False


def test_execution_policy_allows_internal_docker_when_enabled():
    from app.hermes_runtime.execution_policy import build_execution_policy

    tenant = TenantConfig(
        tenant_id="code-bot",
        container_sandbox_enabled=True,
        hermes_code_execution_backend="docker",
    )

    policy = build_execution_policy(tenant, admin=True)

    assert policy.backend == "docker"
    assert policy.allow_file_write is True
    assert policy.requires_checkpoint is True


def test_skill_script_execution_is_denied_by_default():
    from app.hermes_runtime.execution_policy import can_execute_skill_script

    assert can_execute_skill_script("validate-swiss-deck.mjs", tenant=None) is False
