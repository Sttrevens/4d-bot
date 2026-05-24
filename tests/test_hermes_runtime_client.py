import pytest

from app.tenant.config import TenantConfig
from app.tools.tool_result import ToolResult


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



@pytest.mark.asyncio
async def test_client_records_failure_event_for_sidecar_errors(monkeypatch):
    from app.hermes_runtime.client import HermesRuntimeClient
    from app.hermes_runtime.types import RuntimeRequest, RuntimeSender, RuntimeConversation, RuntimeInput

    events = []
    monkeypatch.setattr(
        "app.hermes_runtime.client.record_runtime_event",
        lambda event: events.append(event),
    )

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

    await client.run_turn(request)

    failed = [event.to_dict() for event in events if event.event == "runtime.failed"]
    assert failed
    assert failed[0]["run_id"] == "run-1"
    assert failed[0]["tenant_id"] == "pm-bot"
    assert failed[0]["platform"] == "feishu"
    assert failed[0]["error"] == "runtime_unavailable"

def test_build_runtime_request_contains_allowlisted_tools_only():
    from app.hermes_runtime.client import build_runtime_request

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


@pytest.mark.asyncio
async def test_client_posts_runtime_request_to_configured_sidecar(monkeypatch):
    import httpx

    from app.hermes_runtime.client import HermesRuntimeClient
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeRequest, RuntimeSender

    captured = {}

    class FakeAsyncClient:
        def __init__(self, *, base_url, timeout):
            captured["base_url"] = str(base_url)
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            captured["path"] = path
            captured["json"] = json
            return httpx.Response(
                200,
                json={
                    "run_id": json["run_id"],
                    "runtime": "hermes_sidecar",
                    "status": "completed",
                    "final_text": "sidecar reply",
                },
                request=httpx.Request("POST", "http://sidecar.test/v1/runtime/turn"),
            )

    monkeypatch.setenv("HERMES_RUNTIME_URL", "http://sidecar.test/")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    request = RuntimeRequest(
        run_id="run-sidecar-1",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="u1"),
        conversation=RuntimeConversation(history_key="u1"),
        input=RuntimeInput(text="hello sidecar"),
    )

    response = await HermesRuntimeClient(timeout_seconds=9).run_turn(request)

    assert response.status == "completed"
    assert response.final_text == "sidecar reply"
    assert captured["base_url"] == "http://sidecar.test"
    assert captured["timeout"].read == 9
    assert captured["path"] == "/v1/runtime/turn"
    assert captured["json"]["tenant_id"] == "pm-bot"
    assert captured["json"]["input"]["text"] == "hello sidecar"


@pytest.mark.asyncio
async def test_client_maps_malformed_sidecar_response_to_bad_response(monkeypatch):
    import httpx

    from app.hermes_runtime.client import HermesRuntimeClient
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeRequest, RuntimeSender

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={"status": "completed", "final_text": "missing run id"},
                request=httpx.Request("POST", "http://sidecar.test/v1/runtime/turn"),
            )

    monkeypatch.setenv("HERMES_RUNTIME_URL", "http://sidecar.test")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    request = RuntimeRequest(
        run_id="run-bad-response",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="u1"),
        conversation=RuntimeConversation(history_key="u1"),
        input=RuntimeInput(text="hello"),
    )

    response = await HermesRuntimeClient().run_turn(request)

    assert response.status == "failed"
    assert response.error is not None
    assert response.error.code == "runtime_bad_response"
    assert response.error.retryable is True


@pytest.mark.asyncio
async def test_local_worker_executes_planned_tool_calls_through_tool_bridge(monkeypatch):
    from app.hermes_runtime.types import (
        RuntimeConversation,
        RuntimeInput,
        RuntimePolicy,
        RuntimeRequest,
        RuntimeSender,
    )
    from app.hermes_runtime.worker import run_runtime_turn
    from app.tenant.registry import tenant_registry

    async def async_echo(args):
        return ToolResult.success(f"echo:{args['text']}")

    def fake_get_tenant_tools(tenant, user_text="", override_groups=None, suggested_groups=None):
        return (
            [{"type": "function", "function": {"name": "async_echo", "parameters": {}}}],
            {"async_echo": async_echo},
        )

    monkeypatch.setenv("HERMES_RUNTIME_EXECUTE_LOCAL", "1")
    monkeypatch.setattr("app.services.base_agent._get_tenant_tools", fake_get_tenant_tools)
    tenant_registry.register(TenantConfig(tenant_id="pm-bot", tools_enabled=["async_echo"]))

    response = await run_runtime_turn(
        RuntimeRequest(
            run_id="run-tools",
            tenant_id="pm-bot",
            channel_id="pm-bot-feishu",
            platform="feishu",
            sender=RuntimeSender(sender_id="u1"),
            conversation=RuntimeConversation(history_key="u1"),
            input=RuntimeInput(
                text="call echo",
                attachments=[
                    {
                        "kind": "hermes_tool_calls",
                        "tool_calls": [
                            {"tool_name": "async_echo", "args": {"text": "hello"}},
                        ],
                    }
                ],
            ),
            policy=RuntimePolicy(allowed_tool_names=["async_echo"]),
        )
    )

    assert response.status == "completed"
    assert response.final_text == "async_echo: echo:hello"
    assert response.usage.tool_calls == 1
    assert response.tool_calls[0].tool_name == "async_echo"
    assert response.tool_calls[0].status == "success"
