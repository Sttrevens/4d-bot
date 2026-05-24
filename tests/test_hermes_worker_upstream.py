import pytest


def _runtime_request(*, attachments=None):
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeRequest, RuntimeSender

    return RuntimeRequest(
        run_id="run-worker-upstream",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="ou_1"),
        conversation=RuntimeConversation(history_key="hist-1"),
        input=RuntimeInput(text="hello", attachments=list(attachments or [])),
    )


@pytest.mark.asyncio
async def test_worker_routes_to_upstream_when_configured(monkeypatch):
    from app.hermes_runtime.types import RuntimeResponse
    from app.hermes_runtime.worker import run_runtime_turn

    calls = []

    async def fake_upstream(request, *, timeout_seconds):
        calls.append((request.run_id, timeout_seconds))
        return RuntimeResponse(run_id=request.run_id, status="completed", final_text="real upstream")

    monkeypatch.setenv("HERMES_RUNTIME_EXECUTE_LOCAL", "true")
    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setenv("HERMES_UPSTREAM_TIMEOUT_SECONDS", "42")
    monkeypatch.setattr("app.hermes_runtime.upstream_api.run_upstream_turn", fake_upstream)

    response = await run_runtime_turn(_runtime_request())

    assert response.status == "completed"
    assert response.final_text == "real upstream"
    assert calls == [("run-worker-upstream", 42)]


@pytest.mark.asyncio
async def test_worker_without_upstream_keeps_existing_planned_tool_path(monkeypatch):
    from app.hermes_runtime.types import RuntimeResponse
    from app.hermes_runtime.worker import run_runtime_turn

    async def should_not_call_upstream(*_args, **_kwargs):
        return RuntimeResponse(run_id="bad", status="completed", final_text="should not happen")

    monkeypatch.setenv("HERMES_RUNTIME_EXECUTE_LOCAL", "true")
    monkeypatch.delenv("HERMES_UPSTREAM_API_URL", raising=False)
    monkeypatch.setattr("app.hermes_runtime.upstream_api.run_upstream_turn", should_not_call_upstream)

    response = await run_runtime_turn(_runtime_request())

    assert response.status == "completed"
    assert "no upstream Hermes loop" in response.final_text
