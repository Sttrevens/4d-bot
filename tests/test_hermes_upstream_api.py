import pytest


def _runtime_request(text: str = "hello"):
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeRequest, RuntimeSender

    return RuntimeRequest(
        run_id="run-upstream-1",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="ou_1", identity_id="feishu:ou_1"),
        conversation=RuntimeConversation(history_key="hist-1", chat_id="oc_1", chat_type="group"),
        input=RuntimeInput(text=text, chat_context="context note", image_urls=["https://example.test/a.png"]),
    )


@pytest.mark.asyncio
async def test_upstream_adapter_posts_openai_chat_payload_with_session_headers(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn

    captured = {}

    class FakeAsyncClient:
        def __init__(self, *, base_url, timeout, headers):
            captured["base_url"] = str(base_url)
            captured["timeout"] = timeout
            captured["headers"] = dict(headers)

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
                    "choices": [
                        {"message": {"role": "assistant", "content": "upstream reply"}}
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test/")
    monkeypatch.setenv("HERMES_UPSTREAM_API_KEY", "secret-key")
    monkeypatch.setenv("HERMES_UPSTREAM_MODEL", "hermes-agent")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request(), timeout_seconds=13)

    assert response.status == "completed"
    assert response.final_text == "upstream reply"
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 7
    assert response.usage.api_calls == 1
    assert captured["base_url"] == "http://hermes-upstream.test"
    assert captured["timeout"].read == 13
    assert captured["path"] == "/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["headers"]["X-Hermes-Session-Id"] == "hist-1"
    assert captured["headers"]["X-Hermes-Session-Key"] == "feishu:ou_1"
    assert captured["json"]["model"] == "hermes-agent"
    assert captured["json"]["stream"] is False
    assert captured["json"]["messages"][0]["role"] == "system"
    assert "context note" in captured["json"]["messages"][0]["content"]
    assert captured["json"]["messages"][1]["content"][0]["text"] == "hello"
    assert captured["json"]["messages"][1]["content"][1]["image_url"]["url"] == "https://example.test/a.png"


@pytest.mark.asyncio
async def test_upstream_adapter_maps_http_failure_to_runtime_unavailable(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, *_args, **_kwargs):
            return httpx.Response(
                503,
                text="down",
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request(), timeout_seconds=3)

    assert response.status == "failed"
    assert response.error is not None
    assert response.error.code == "runtime_unavailable"
    assert response.error.retryable is True


@pytest.mark.asyncio
async def test_upstream_adapter_maps_malformed_response_to_bad_response(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn

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
                json={"choices": [{"message": {"role": "assistant"}}]},
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request(), timeout_seconds=3)

    assert response.status == "failed"
    assert response.error is not None
    assert response.error.code == "runtime_bad_response"
    assert response.error.retryable is True
