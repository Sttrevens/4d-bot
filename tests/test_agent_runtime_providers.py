import pytest

from app.tenant.config import TenantConfig


def test_provider_manifest_lists_first_class_local_agents():
    from app.agent_runtime.providers import list_provider_manifests

    manifests = {item.name: item for item in list_provider_manifests()}

    assert {"legacy", "hermes_sidecar", "codex_cli", "claude_cli", "custom_command"} <= set(manifests)
    assert manifests["codex_cli"].supports_streaming is True
    assert manifests["codex_cli"].supports_file_write is True
    assert manifests["codex_cli"].default_sandbox == "read-only"
    assert manifests["claude_cli"].supports_sessions is True
    assert manifests["claude_cli"].requires_explicit_enable is True


def test_build_runtime_request_carries_local_agent_options_with_safe_defaults():
    from app.hermes_runtime.client import build_runtime_request

    tenant = TenantConfig(
        tenant_id="pm-bot",
        platform="feishu",
        agent_runtime_provider="codex_cli",
        agent_runtime_command="codex",
        agent_runtime_workspace="/tmp/example-repo",
        agent_runtime_sandbox="workspace-write",
        agent_runtime_auto_execute=False,
    )

    request = build_runtime_request(
        tenant,
        run_id="run-1",
        user_text="review this repo",
        sender_id="u1",
        history_key="u1",
    )

    assert request.runtime.provider == "codex_cli"
    assert request.runtime.command == "codex"
    assert request.runtime.workspace == "/tmp/example-repo"
    assert request.runtime.sandbox == "read-only"
    assert request.runtime.auto_execute is False


def test_build_runtime_request_allows_write_sandbox_only_when_auto_execute_enabled():
    from app.hermes_runtime.client import build_runtime_request

    tenant = TenantConfig(
        tenant_id="pm-bot",
        platform="feishu",
        agent_runtime_provider="codex_cli",
        agent_runtime_sandbox="workspace-write",
        agent_runtime_auto_execute=True,
    )

    request = build_runtime_request(
        tenant,
        run_id="run-1",
        user_text="fix the tests",
        sender_id="u1",
        history_key="u1",
    )

    assert request.runtime.sandbox == "workspace-write"
    assert request.runtime.auto_execute is True


@pytest.mark.asyncio
async def test_codex_cli_runtime_uses_jsonl_exec_and_returns_final_message():
    from app.agent_runtime.cli_adapters import CodexCliRuntimeClient
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeOptions, RuntimeRequest, RuntimeSender

    captured = {}

    async def fake_run(command, *, cwd, env, stdin, timeout_seconds):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = env
        captured["stdin"] = stdin
        captured["timeout_seconds"] = timeout_seconds
        return (
            0,
            "\n".join(
                [
                    '{"type":"thread.started","thread_id":"thread-1"}',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"Codex done"}}',
                    '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":5}}',
                ]
            ),
            "",
        )

    request = RuntimeRequest(
        run_id="run-codex",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="u1"),
        conversation=RuntimeConversation(history_key="u1"),
        input=RuntimeInput(text="summarize the repo"),
        runtime=RuntimeOptions(
            provider="codex_cli",
            command="codex",
            workspace="/tmp/example-repo",
            sandbox="read-only",
        ),
    )

    response = await CodexCliRuntimeClient(process_runner=fake_run).run_turn(request)

    assert response.status == "completed"
    assert response.runtime == "codex_cli"
    assert response.final_text == "Codex done"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 5
    assert response.resume["resume_token"] == "thread-1"
    assert captured["command"] == ["codex", "exec", "--json", "--sandbox", "read-only", "summarize the repo"]
    assert captured["cwd"] == "/tmp/example-repo"


@pytest.mark.asyncio
async def test_claude_cli_runtime_uses_print_stream_json_and_returns_result():
    from app.agent_runtime.cli_adapters import ClaudeCliRuntimeClient
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeOptions, RuntimeRequest, RuntimeSender

    captured = {}

    async def fake_run(command, *, cwd, env, stdin, timeout_seconds):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["stdin"] = stdin
        captured["timeout_seconds"] = timeout_seconds
        return (
            0,
            "\n".join(
                [
                    '{"type":"assistant","message":{"content":[{"type":"text","text":"Working"}]}}',
                    '{"type":"result","result":"Claude done","session_id":"session-1"}',
                ]
            ),
            "",
        )

    request = RuntimeRequest(
        run_id="run-claude",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="u1"),
        conversation=RuntimeConversation(history_key="u1"),
        input=RuntimeInput(text="summarize the repo"),
        runtime=RuntimeOptions(
            provider="claude_cli",
            command="claude",
            workspace="/tmp/example-repo",
            permission_mode="plan",
            max_rounds=3,
        ),
    )

    response = await ClaudeCliRuntimeClient(process_runner=fake_run).run_turn(request)

    assert response.status == "completed"
    assert response.runtime == "claude_cli"
    assert response.final_text == "Claude done"
    assert response.resume["resume_token"] == "session-1"
    assert captured["command"] == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--permission-mode",
        "plan",
        "--max-turns",
        "3",
        "summarize the repo",
    ]


@pytest.mark.asyncio
async def test_agent_runtime_client_dispatches_to_codex_provider(monkeypatch):
    from app.agent_runtime.client import AgentRuntimeClient
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeOptions, RuntimeRequest, RuntimeResponse, RuntimeSender

    async def fake_codex_run(self, request):
        return RuntimeResponse(run_id=request.run_id, runtime="codex_cli", final_text="codex reply")

    monkeypatch.setattr("app.agent_runtime.cli_adapters.CodexCliRuntimeClient.run_turn", fake_codex_run)

    request = RuntimeRequest(
        run_id="run-codex",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="u1"),
        conversation=RuntimeConversation(history_key="u1"),
        input=RuntimeInput(text="hello"),
        runtime=RuntimeOptions(provider="codex_cli"),
    )

    response = await AgentRuntimeClient(provider="codex_cli").run_turn(request)

    assert response.final_text == "codex reply"


@pytest.mark.asyncio
async def test_codex_cli_runtime_failure_keeps_provider_identity():
    from app.agent_runtime.cli_adapters import CodexCliRuntimeClient
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeOptions, RuntimeRequest, RuntimeSender

    async def fake_run(command, *, cwd, env, stdin, timeout_seconds):
        return 2, "", "codex auth missing"

    request = RuntimeRequest(
        run_id="run-codex-failed",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="u1"),
        conversation=RuntimeConversation(history_key="u1"),
        input=RuntimeInput(text="hello"),
        runtime=RuntimeOptions(provider="codex_cli", command="codex"),
    )

    response = await CodexCliRuntimeClient(process_runner=fake_run).run_turn(request)

    assert response.status == "failed"
    assert response.runtime == "codex_cli"
    assert response.error is not None
    assert response.error.code == "runtime_failed"
