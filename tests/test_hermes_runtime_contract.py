from app.tenant.config import TenantConfig


def test_tenant_runtime_defaults_are_legacy_and_disabled():
    tenant = TenantConfig(tenant_id="pm-bot")

    assert tenant.agent_runtime == "legacy"
    assert tenant.hermes_runtime_enabled is False
    assert tenant.hermes_runtime_shadow is False
    assert tenant.hermes_runtime_rollout_percent == 0


def test_selector_returns_legacy_when_disabled():
    from app.hermes_runtime.selector import select_runtime

    tenant = TenantConfig(
        tenant_id="pm-bot",
        agent_runtime="hermes_sidecar",
        hermes_runtime_enabled=False,
        hermes_runtime_rollout_percent=100,
    )

    assert select_runtime(tenant, sender_id="u1", run_id="r1") == "legacy"


def test_selector_returns_shadow_when_shadow_enabled():
    from app.hermes_runtime.selector import select_runtime

    tenant = TenantConfig(
        tenant_id="pm-bot",
        agent_runtime="hermes_sidecar",
        hermes_runtime_enabled=True,
        hermes_runtime_shadow=True,
        hermes_runtime_rollout_percent=100,
    )

    assert select_runtime(tenant, sender_id="u1", run_id="r1") == "legacy_shadow_hermes"


def test_selector_returns_hermes_for_full_rollout():
    from app.hermes_runtime.selector import select_runtime

    tenant = TenantConfig(
        tenant_id="pm-bot",
        agent_runtime="hermes_sidecar",
        hermes_runtime_enabled=True,
        hermes_runtime_rollout_percent=100,
    )

    assert select_runtime(tenant, sender_id="u1", run_id="r1") == "hermes_sidecar"


def test_runtime_request_round_trips_without_losing_policy():
    from app.hermes_runtime.types import (
        RuntimeConversation,
        RuntimeInput,
        RuntimePolicy,
        RuntimeRequest,
        RuntimeOptions,
        RuntimeSender,
    )

    request = RuntimeRequest(
        run_id="run-1",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="ou_1", sender_name="Steven", identity_id="id_1"),
        conversation=RuntimeConversation(history_key="id_1", chat_id="oc_1", chat_type="group"),
        input=RuntimeInput(text="帮我做 PPT", image_urls=[]),
        policy=RuntimePolicy(allowed_tool_names=["think"], allowed_tool_groups=["core"], admin=True),
        runtime=RuntimeOptions(profile="default", memory_enabled=True, skills_enabled=True),
    )

    restored = RuntimeRequest.from_dict(request.to_dict())

    assert restored.run_id == "run-1"
    assert restored.policy.allowed_tool_names == ["think"]
    assert restored.policy.admin is True
