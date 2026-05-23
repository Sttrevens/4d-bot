import pytest


@pytest.mark.asyncio
async def test_client_maps_worker_exception_to_runtime_unavailable():
    from app.hermes_runtime.client import HermesRuntimeClient
    from app.hermes_runtime.types import RuntimeRequest, RuntimeSender, RuntimeConversation, RuntimeInput

    async def broken_worker(_request):
        raise RuntimeError("sidecar down")

    request = RuntimeRequest(
        run_id="run-1",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="u1"),
        conversation=RuntimeConversation(history_key="u1"),
        input=RuntimeInput(text="hello"),
    )
    client = HermesRuntimeClient(worker=broken_worker)

    response = await client.run_turn(request)

    assert response.status == "failed"
    assert response.error is not None
    assert response.error.code == "runtime_unavailable"
    assert response.error.retryable is True


def test_build_runtime_request_contains_allowlisted_tools_only():
    from app.hermes_runtime.client import build_runtime_request
    from app.tenant.config import TenantConfig

    tenant = TenantConfig(
        tenant_id="pm-bot",
        platform="feishu",
        tools_enabled=["think", "export_file"],
        self_iteration_enabled=False,
    )

    request = build_runtime_request(
        tenant,
        run_id="run-1",
        user_text="hello",
        sender_id="u1",
        sender_name="Steven",
        history_key="u1",
        chat_id="",
        chat_type="",
    )

    assert request.tenant_id == "pm-bot"
    assert request.policy.allowed_tool_names == ["think", "export_file"]
    assert request.runtime.profile == "default"
