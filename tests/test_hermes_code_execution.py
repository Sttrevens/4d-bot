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


def test_destructive_command_requires_confirmation():
    from app.hermes_runtime.execution_policy import ExecutionPolicy, evaluate_execution_request

    policy = ExecutionPolicy(
        backend="docker",
        allow_file_write=True,
        allowed_paths=["app/tools", "app/knowledge"],
        requires_checkpoint=True,
    )

    decision = evaluate_execution_request(
        policy,
        command="rm -rf app/tools/generated",
        cwd="/workspace",
    )

    assert decision.allowed is False
    assert decision.status == "needs_confirmation"
    assert decision.code == "confirmation_required"
    assert decision.approval_request["command"] == "rm -rf app/tools/generated"
    assert decision.approval_request["rollback_available"] is True


def test_write_outside_allowed_paths_is_denied():
    from app.hermes_runtime.execution_policy import ExecutionPolicy, evaluate_execution_request

    policy = ExecutionPolicy(
        backend="docker",
        allow_file_write=True,
        allowed_paths=["app/tools", "app/knowledge"],
        requires_checkpoint=True,
    )

    decision = evaluate_execution_request(
        policy,
        command="python generate.py",
        write_paths=["app/hermes_runtime/execution_policy.py"],
    )

    assert decision.allowed is False
    assert decision.status == "blocked"
    assert decision.code == "path_denied"
