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


def test_selector_returns_shadow_without_visible_rollout():
    from app.hermes_runtime.selector import select_runtime

    tenant = TenantConfig(
        tenant_id="pm-bot",
        agent_runtime="hermes_sidecar",
        hermes_runtime_enabled=True,
        hermes_runtime_shadow=True,
        hermes_runtime_rollout_percent=0,
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


def test_runtime_request_from_dict_ignores_additive_nested_fields():
    from app.hermes_runtime.types import RuntimeRequest

    restored = RuntimeRequest.from_dict(
        {
            "run_id": "run-compat-1",
            "tenant_id": "pm-bot",
            "channel_id": "pm-bot-feishu",
            "platform": "feishu",
            "sender": {
                "sender_id": "ou_1",
                "sender_name": "Steven",
                "identity_id": "feishu:ou_1",
                "display_locale": "zh-CN",
            },
            "conversation": {
                "history_key": "hist-1",
                "chat_id": "oc_1",
                "chat_type": "group",
                "thread_id": "thread-future",
            },
            "input": {
                "text": "hello",
                "image_urls": ["https://example.test/a.png"],
                "attachments": [{"kind": "file", "url": "https://example.test/a.txt"}],
                "chat_context": "context",
                "future_modalities": ["audio"],
            },
            "policy": {
                "allowed_tool_names": ["think"],
                "allowed_tool_groups": ["core"],
                "admin": True,
                "future_policy": "ignored",
            },
            "runtime": {
                "profile": "default",
                "memory_enabled": True,
                "skills_enabled": True,
                "future_runtime_knob": 3,
            },
            "future_top_level": "ignored",
        }
    )

    assert restored.sender.identity_id == "feishu:ou_1"
    assert restored.conversation.history_key == "hist-1"
    assert restored.input.image_urls == ["https://example.test/a.png"]
    assert restored.policy.allowed_tool_names == ["think"]
    assert restored.runtime.skills_enabled is True


def test_runtime_response_from_dict_accepts_additive_sidecar_fields():
    from app.hermes_runtime.types import RuntimeResponse

    response = RuntimeResponse.from_dict(
        {
            "run_id": "run-compat-2",
            "runtime": "hermes_sidecar",
            "status": "completed",
            "final_text": "done",
            "artifacts": [
                {
                    "artifact_id": "file_1",
                    "kind": "html",
                    "filename": "deck.html",
                    "delivery_hint": "send_file",
                    "mime_type": "text/html",
                }
            ],
            "tool_calls": [
                {
                    "name": "export_file",
                    "status": "success",
                    "duration_ms": 12,
                    "side_effect": True,
                    "input_tokens": 99,
                }
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 4,
                "api_calls": 1,
                "tool_calls": 1,
                "total_tokens": 14,
            },
            "events": [{"event": "runtime.completed"}],
            "resume": {"resumable": False, "resume_token": "", "checkpoint": "future"},
            "future_top_level": "ignored",
        }
    )

    assert response.run_id == "run-compat-2"
    assert response.artifacts[0].artifact_id == "file_1"
    assert response.tool_calls[0].tool_name == "export_file"
    assert response.tool_calls[0].side_effect is True
    assert response.usage.input_tokens == 10
    assert response.events == [{"event": "runtime.completed"}]
