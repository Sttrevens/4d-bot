import pytest

from app.tenant.config import TenantConfig
from app.tools.tool_result import ToolResult


@pytest.fixture(autouse=True)
def _reset_pm_bot_tenant():
    from app.tenant.registry import tenant_registry

    tenant_registry.unregister("pm-bot")
    yield
    tenant_registry.unregister("pm-bot")


def _runtime_request(text: str = "hello", *, tools=None):
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimePolicy, RuntimeRequest, RuntimeSender

    return RuntimeRequest(
        run_id="run-upstream-1",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="ou_1", identity_id="feishu:ou_1"),
        conversation=RuntimeConversation(history_key="hist-1", chat_id="oc_1", chat_type="group"),
        input=RuntimeInput(text=text, chat_context="context note", image_urls=["https://example.test/a.png"]),
        policy=RuntimePolicy(allowed_tool_names=list(tools or [])),
    )


@pytest.fixture(autouse=True)
def _restore_tenant_registry():
    from app.tenant.registry import tenant_registry

    old_tenants = tenant_registry._tenants.copy()
    old_default = tenant_registry._default_tenant_id
    try:
        yield
    finally:
        tenant_registry._tenants.clear()
        tenant_registry._tenants.update(old_tenants)
        tenant_registry._default_tenant_id = old_default


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
async def test_upstream_adapter_executes_openai_tool_call_round_trip(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn
    from app.tenant.registry import tenant_registry

    payloads = []

    async def echo(args):
        return ToolResult.success(f"echo:{args['text']}")

    def fake_get_tenant_tools(tenant, user_text="", override_groups=None, suggested_groups=None):
        return (
            [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "Echo text",
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                }
            ],
            {"echo": echo},
        )

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            payloads.append(json)
            if len(payloads) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call_echo_1",
                                            "type": "function",
                                            "function": {
                                                "name": "echo",
                                                "arguments": '{"text": "hello"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                    },
                    request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "final after tool"}}],
                    "usage": {"prompt_tokens": 17, "completion_tokens": 5},
                },
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    tenant_registry.register(TenantConfig(tenant_id="pm-bot", tools_enabled=["echo"]))
    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr("app.services.base_agent._get_tenant_tools", fake_get_tenant_tools)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request(tools=["echo"]), timeout_seconds=3)

    assert response.status == "completed"
    assert response.final_text == "final after tool"
    assert response.usage.input_tokens == 28
    assert response.usage.output_tokens == 8
    assert response.usage.api_calls == 2
    assert response.usage.tool_calls == 1
    assert response.tool_calls[0].tool_name == "echo"
    assert response.tool_calls[0].status == "success"

    assert payloads[0]["tools"][0]["function"]["name"] == "echo"
    assert payloads[0]["tool_choice"] == "auto"
    assert payloads[1]["messages"][-2]["tool_calls"][0]["id"] == "call_echo_1"
    assert payloads[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_echo_1",
        "content": "echo:hello",
    }


@pytest.mark.asyncio
async def test_upstream_adapter_empty_policy_tools_uses_tenant_visible_toolset(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn
    from app.tenant.registry import tenant_registry

    payloads = []

    async def echo(args):
        return ToolResult.success(f"echo:{args['text']}")

    def fake_get_tenant_tools(tenant, user_text="", override_groups=None, suggested_groups=None):
        return (
            [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "Echo text",
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                }
            ],
            {"echo": echo},
        )

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            payloads.append(json)
            if len(payloads) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call_echo_1",
                                            "type": "function",
                                            "function": {
                                                "name": "echo",
                                                "arguments": '{"text": "hello"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                    request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    tenant_registry.register(TenantConfig(tenant_id="pm-bot", tools_enabled=[]))
    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr("app.services.base_agent._get_tenant_tools", fake_get_tenant_tools)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request(), timeout_seconds=3)

    assert response.status == "completed"
    assert response.final_text == "done"
    assert payloads[0]["tools"][0]["function"]["name"] == "echo"
    assert response.tool_calls[0].tool_name == "echo"
    assert response.usage.tool_calls == 1


@pytest.mark.asyncio
async def test_upstream_adapter_emits_checkpoint_event_for_side_effect_tool(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn
    from app.tenant.registry import tenant_registry

    payloads = []
    executed = []

    async def edit_file(args):
        executed.append(dict(args))
        return ToolResult.success("edited")

    def fake_get_tenant_tools(tenant, user_text="", override_groups=None, suggested_groups=None):
        return (
            [
                {
                    "type": "function",
                    "function": {
                        "name": "self_edit_file",
                        "description": "Edit a repository file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                }
            ],
            {"self_edit_file": edit_file},
        )

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            payloads.append(json)
            if len(payloads) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call_edit_1",
                                            "type": "function",
                                            "function": {
                                                "name": "self_edit_file",
                                                "arguments": '{"path": "app/tools/x.py"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                    request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    tenant_registry.register(TenantConfig(tenant_id="pm-bot", tools_enabled=["self_edit_file"]))
    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr("app.services.base_agent._get_tenant_tools", fake_get_tenant_tools)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request(tools=["self_edit_file"]), timeout_seconds=3)

    checkpoint_events = [
        event for event in response.events if event.get("event") == "runtime.checkpoint.created"
    ]
    assert response.status == "completed"
    assert executed == [{"path": "app/tools/x.py"}]
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0]["run_id"] == "run-upstream-1"
    assert checkpoint_events[0]["tenant_id"] == "pm-bot"
    assert checkpoint_events[0]["tool_name"] == "self_edit_file"
    assert checkpoint_events[0]["side_effect_class"] == "code_mutation"
    assert checkpoint_events[0]["checkpoint_id"].startswith("cp_")
    assert checkpoint_events[0]["target"] == "app/tools/x.py"
    assert checkpoint_events[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_upstream_adapter_pauses_for_infrastructure_confirmation_before_execution(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn
    from app.tenant.registry import tenant_registry

    payloads = []
    executed = []

    async def restart_instance(args):
        executed.append(dict(args))
        return ToolResult.success("restarted")

    def fake_get_tenant_tools(tenant, user_text="", override_groups=None, suggested_groups=None):
        return (
            [
                {
                    "type": "function",
                    "function": {
                        "name": "restart_instance",
                        "description": "Restart a tenant instance",
                        "parameters": {
                            "type": "object",
                            "properties": {"tenant_id": {"type": "string"}},
                            "required": ["tenant_id"],
                        },
                    },
                }
            ],
            {"restart_instance": restart_instance},
        )

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            payloads.append(json)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_restart_1",
                                        "type": "function",
                                        "function": {
                                            "name": "restart_instance",
                                            "arguments": '{"tenant_id": "pm-bot"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    tenant_registry.register(TenantConfig(tenant_id="pm-bot", tools_enabled=["restart_instance"]))
    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr("app.services.base_agent._get_tenant_tools", fake_get_tenant_tools)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    request = _runtime_request(tools=["restart_instance"])
    request.policy.admin = True
    request.policy.requires_confirmation_for = ["infrastructure"]

    response = await run_upstream_turn(request, timeout_seconds=3)

    assert response.status == "needs_confirmation"
    assert executed == []
    assert len(payloads) == 1
    assert response.tool_calls[0].tool_name == "restart_instance"
    assert response.tool_calls[0].status == "failed"

    assert response.tool_calls[0].code == "confirmation_required"
    assert response.resume["approval_request"]["tool_name"] == "restart_instance"
    assert response.resume["approval_request"]["side_effect_class"] == "infrastructure"


@pytest.mark.asyncio
async def test_upstream_adapter_blocks_autofix_protected_path_before_tool_execution(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn
    from app.tenant.registry import tenant_registry

    payloads = []
    executed = []

    async def edit_file(args):
        executed.append(dict(args))
        return ToolResult.success("edited")

    def fake_get_tenant_tools(tenant, user_text="", override_groups=None, suggested_groups=None):
        return (
            [
                {
                    "type": "function",
                    "function": {
                        "name": "self_edit_file",
                        "description": "Edit a repository file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                }
            ],
            {"self_edit_file": edit_file},
        )

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            payloads.append(json)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_edit_1",
                                        "type": "function",
                                        "function": {
                                            "name": "self_edit_file",
                                            "arguments": '{"path": "app/hermes_runtime/tool_bridge.py", "old": "a", "new": "b"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    tenant_registry.register(TenantConfig(tenant_id="pm-bot", tools_enabled=["self_edit_file"]))
    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr("app.services.base_agent._get_tenant_tools", fake_get_tenant_tools)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    request = _runtime_request(tools=["self_edit_file"])
    request.policy.self_iteration_enabled = True

    response = await run_upstream_turn(request, timeout_seconds=3)

    assert response.status == "blocked"
    assert executed == []
    assert len(payloads) == 1
    assert response.error is not None
    assert response.error.code == "policy_denied"
    assert response.tool_calls[0].tool_name == "self_edit_file"
    assert response.tool_calls[0].code == "policy_denied"
    assert not [event for event in response.events if event.get("event") == "runtime.checkpoint.created"]


@pytest.mark.asyncio
async def test_upstream_adapter_uses_tenant_provider_model_and_emits_selection_event(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn
    from app.tenant.registry import tenant_registry

    captured = {}

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            captured["json"] = json
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "tenant model reply"}}]},
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    tenant_registry.register(
        TenantConfig(
            tenant_id="pm-bot",
            llm_provider="gemini",
            llm_model="gemini-3-flash-preview",
            llm_model_strong="gemini-3.1-pro-preview-customtools",
            llm_api_key="secret-key",
        )
    )
    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.delenv("HERMES_UPSTREAM_MODEL", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request(), timeout_seconds=3)

    assert response.status == "completed"
    assert captured["json"]["model"] == "gemini-3-flash-preview"
    assert response.events == [
        {
            "event": "runtime.model.selected",
            "provider": "gemini",
            "model": "gemini-3-flash-preview",
            "credential_ref": "tenant:pm-bot:llm_api_key",
        }
    ]
    assert "secret-key" not in str(response.events)


@pytest.mark.asyncio
async def test_upstream_adapter_retries_tenant_fallback_model_on_provider_exhaustion(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn
    from app.tenant.registry import tenant_registry

    payloads = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            payloads.append(json)
            if len(payloads) == 1:
                return httpx.Response(
                    429,
                    text="quota exhausted",
                    request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "fallback reply"}}]},
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    tenant_registry.register(
        TenantConfig(
            tenant_id="pm-bot",
            llm_provider="gemini",
            llm_model="gemini-3-flash-preview",
            llm_model_strong="gemini-3.1-pro-preview-customtools",
            llm_api_key="secret-key",
        )
    )
    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.delenv("HERMES_UPSTREAM_MODEL", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request(), timeout_seconds=3)

    assert response.status == "completed"
    assert response.final_text == "fallback reply"
    assert [payload["model"] for payload in payloads] == [
        "gemini-3-flash-preview",
        "gemini-3.1-pro-preview-customtools",
    ]
    assert response.events == [
        {
            "event": "runtime.model.selected",
            "provider": "gemini",
            "model": "gemini-3-flash-preview",
            "credential_ref": "tenant:pm-bot:llm_api_key",
        },
        {
            "event": "runtime.provider.exhausted",
            "provider": "gemini",
            "model": "gemini-3-flash-preview",
            "credential_ref": "tenant:pm-bot:llm_api_key",
            "status_code": 429,
        },
        {
            "event": "runtime.model.selected",
            "provider": "gemini",
            "model": "gemini-3.1-pro-preview-customtools",
            "credential_ref": "tenant:pm-bot:llm_api_key",
        },
    ]
    assert "secret-key" not in str(response.events)


@pytest.mark.asyncio
async def test_upstream_adapter_tries_backup_credential_before_model_fallback(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn
    from app.tenant.registry import tenant_registry

    attempts = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json, headers=None):
            attempts.append({"json": json, "headers": dict(headers or {})})
            if len(attempts) == 1:
                return httpx.Response(
                    429,
                    text="quota exhausted",
                    request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "backup reply"}}]},
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    tenant = TenantConfig(
        tenant_id="pm-bot",
        llm_provider="gemini",
        llm_model="gemini-3-flash-preview",
        llm_model_strong="gemini-3.1-pro-preview-customtools",
        llm_api_key="secret-key",
    )
    tenant.hermes_provider_credential_refs = ["tenant:pm-bot:llm_api_key:backup"]
    tenant_registry.register(tenant)

    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.delenv("HERMES_UPSTREAM_MODEL", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request(), timeout_seconds=3)

    assert response.status == "completed"
    assert response.final_text == "backup reply"
    assert [attempt["json"]["model"] for attempt in attempts] == [
        "gemini-3-flash-preview",
        "gemini-3-flash-preview",
    ]
    assert [attempt["headers"]["X-Hermes-Credential-Ref"] for attempt in attempts] == [
        "tenant:pm-bot:llm_api_key",
        "tenant:pm-bot:llm_api_key:backup",
    ]
    assert response.events == [
        {
            "event": "runtime.model.selected",
            "provider": "gemini",
            "model": "gemini-3-flash-preview",
            "credential_ref": "tenant:pm-bot:llm_api_key",
            "credential_id": "primary",
        },
        {
            "event": "runtime.provider.exhausted",
            "provider": "gemini",
            "model": "gemini-3-flash-preview",
            "credential_ref": "tenant:pm-bot:llm_api_key",
            "credential_id": "primary",
            "status_code": 429,
        },
        {
            "event": "runtime.model.selected",
            "provider": "gemini",
            "model": "gemini-3-flash-preview",
            "credential_ref": "tenant:pm-bot:llm_api_key:backup",
            "credential_id": "backup",
        },
    ]
    assert "secret-key" not in str(response.events)


@pytest.mark.asyncio
async def test_upstream_adapter_injects_repo_skill_activation_card(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn

    captured = {}

    monkeypatch.setattr(
        "app.tools.skill_engine.load_triggered_skills",
        lambda tenant_id, text: (
            """
<skill name="guizang-ppt-skill" type="repo">
Repo 型 skill 已激活：生成横向翻页网页 PPT，提供瑞士国际主义风格。
可用文件: SKILL.md, assets/template-swiss.html
Full SKILL.md contents should not be injected into upstream context.
</skill>
""",
            [],
            {},
        ),
    )

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            captured["json"] = json
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "deck ready"}}]},
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = await run_upstream_turn(_runtime_request("帮我做一份瑞士风 PPT"), timeout_seconds=3)

    system_context = captured["json"]["messages"][0]["content"]
    assert response.status == "completed"
    assert '<skill-activation name="guizang-ppt-skill" type="repo">' in system_context
    assert "read_agent_skill_file" in system_context
    assert "Full SKILL.md contents" not in system_context


@pytest.mark.asyncio
async def test_upstream_adapter_prefetches_and_syncs_memory(monkeypatch):
    import httpx

    from app.hermes_runtime.upstream_api import run_upstream_turn

    captured = {}
    diary_calls = []

    async def fake_build_memory_context(user_id, user_name="", current_text=""):
        captured["memory_read"] = {
            "user_id": user_id,
            "user_name": user_name,
            "current_text": current_text,
        }
        return "memory says: user prefers Swiss style decks"

    async def fake_write_diary(user_id, user_name, user_content, assistant_content, *, tool_names_called=None):
        diary_calls.append(
            {
                "user_id": user_id,
                "user_name": user_name,
                "user_content": user_content,
                "assistant_content": assistant_content,
                "tool_names_called": list(tool_names_called or []),
            }
        )

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, path, json):
            captured["json"] = json
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "deck ready"}}]},
                request=httpx.Request("POST", "http://hermes-upstream.test/v1/chat/completions"),
            )

    monkeypatch.setenv("HERMES_UPSTREAM_API_URL", "http://hermes-upstream.test")
    monkeypatch.setattr("app.services.memory.build_memory_context", fake_build_memory_context)
    monkeypatch.setattr("app.services.memory.write_diary", fake_write_diary)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    request = _runtime_request("帮我继续做瑞士风 PPT")
    request.sender.sender_name = "Steven"

    response = await run_upstream_turn(request, timeout_seconds=3)

    assert response.status == "completed"
    assert "memory says: user prefers Swiss style decks" in captured["json"]["messages"][0]["content"]
    assert captured["memory_read"] == {
        "user_id": "feishu:ou_1",
        "user_name": "Steven",
        "current_text": "帮我继续做瑞士风 PPT",
    }
    assert diary_calls == [
        {
            "user_id": "feishu:ou_1",
            "user_name": "Steven",
            "user_content": "帮我继续做瑞士风 PPT",
            "assistant_content": "deck ready",
            "tool_names_called": [],
        }
    ]


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
