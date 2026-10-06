import pytest


def _runtime_request(text: str = "hello"):
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeRequest, RuntimeSender

    return RuntimeRequest(
        run_id="run-worker-upstream",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="ou_1"),
        conversation=RuntimeConversation(history_key="hist-1"),
        input=RuntimeInput(text=text),
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
async def test_worker_adds_skill_activation_context_before_upstream(monkeypatch):
    from app.hermes_runtime.types import RuntimeResponse
    from app.hermes_runtime.worker import run_runtime_turn

    captured = {}

    async def fake_upstream(request, *, timeout_seconds):
        captured["chat_context"] = request.input.chat_context
        return RuntimeResponse(run_id=request.run_id, status="completed", final_text="used skill")

    monkeypatch.setenv("HERMES_RUNTIME_EXECUTE_LOCAL", "true")
    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr("app.hermes_runtime.upstream_api.run_upstream_turn", fake_upstream)
    monkeypatch.setattr(
        "app.hermes_runtime.skills_bridge.build_skill_activation_context",
        lambda tenant_id, text: '<skill-activation name="guizang-ppt-skill" type="repo">activation card</skill-activation>',
    )

    response = await run_runtime_turn(_runtime_request("帮我做一份瑞士风 PPT"))

    assert response.status == "completed"
    assert "guizang-ppt-skill" in captured["chat_context"]
    assert "activation card" in captured["chat_context"]
