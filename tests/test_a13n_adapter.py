"""Host contracts, using a scripted stdio peer and the existing tool bridge.

The peer supplies tool *requests*, not tool outputs. Handlers in this process
produce the results, so these tests exercise adapter permissions and dispatch.
SDK compatibility has a separate isolated environment in tests/a13n_worker.
"""
from __future__ import annotations

import asyncio
import builtins
import copy
import json
import os
from pathlib import Path
import re

import pytest

from app.harness.benchmark import run_benchmark_suite
from app.harness.scenario_replay import replay_scenario
from app.hermes_runtime.tool_bridge import RuntimeToolSchema, RuntimeToolset
from app.hermes_runtime.types import (
    RuntimeConversation, RuntimeInput, RuntimeOptions, RuntimePolicy,
    RuntimeRequest, RuntimeResponse, RuntimeSender,
)
from app.tenant.config import TenantConfig
from app.tools.tool_result import ToolResult


FIXTURE = Path(__file__).parent / "fixtures" / "a13n" / "read_only_replay.json"


def request(run_id="run-1", **changes):
    value = RuntimeRequest(
        run_id=run_id,
        tenant_id="contract-bot",
        channel_id="contract-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="u1", sender_name="Steven", identity_id="feishu:u1"),
        conversation=RuntimeConversation(history_key="history-u1", chat_id="chat-1", chat_type="group"),
        input=RuntimeInput(text="hello"),
        policy=RuntimePolicy(allowed_tool_names=["think"], allowed_tool_groups=["core"]),
        runtime=RuntimeOptions(provider="a13n_harness", command="fake-a13n-worker", auto_execute=True),
    )
    for key, item in changes.items():
        setattr(value, key, item)
    return value


def toolset(handlers, visible=None):
    names = list(handlers) if visible is None else visible
    return RuntimeToolset(
        tools=[RuntimeToolSchema(name=name, schema={"type": "function", "function": {
            "name": name, "description": "Contract tool", "parameters": {"type": "object"},
        }}) for name in names],
        handlers=handlers,
        tenant_id="contract-bot",
    )


class ScriptedProcess:
    """Minimal subprocess peer which waits for Host results before advancing."""
    def __init__(self, calls=(), *, result=None, state="{}", raw=None, hang=False, assemble=False, hang_after_tools=False):
        self.calls = list(calls)
        self.result = result or {"status": "completed", "final_text": "done"}
        self.state = state
        self.raw = raw
        self.hang = hang
        self.assemble = assemble
        self.hang_after_tools = hang_after_tools
        self.messages = []
        self.tool_results = []
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.stdin = self
        self.returncode = None
        self.killed = False
        self.waited = False
        self.closed = False
        self.started = asyncio.Event()

    def write(self, data):
        message = json.loads(data)
        self.messages.append(message)
        if message["type"] == "start":
            if self.state is not None:
                saved = json.loads(self.state)
                saved["thread_id"] = message["thread_id"]
                self.state = json.dumps(saved)
            self.started.set()
            if self.raw is not None:
                self.stdout.feed_data(self.raw)
                self.stdout.feed_eof()
            elif not self.hang:
                self._advance()
        elif message["type"] == "tool_result":
            self.tool_results.append(message)
            self._advance()

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.closed = True

    def _advance(self):
        if len(self.tool_results) < len(self.calls):
            item = self.calls[len(self.tool_results)]
            message = {"type": "tool_call", "call_id": f"call-{len(self.tool_results) + 1}",
                       "name": item["name"], "args": item.get("args", {})}
        else:
            if self.hang_after_tools:
                return
            message = {"type": "result", "state": self.state, "sdk_run_id": "sdk-run-1",
                       "sdk_thread_id": self.messages[0]["thread_id"], "usage": {"input_tokens": 7,
                           "output_tokens": 3, "api_calls": 1}, **self.result}
            if self.assemble:
                message["final_text"] = "\n".join(item["result"]["content"] for item in self.tool_results)
        self.stdout.feed_data(json.dumps(message).encode() + b"\n")
        if message["type"] == "result":
            self.stdout.feed_eof()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()

    def terminate(self):
        self.kill()

    async def wait(self):
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class ProcessFactory:
    def __init__(self, *processes):
        self.processes = list(processes)
        self.invocations = []

    async def __call__(self, *command, **kwargs):
        self.invocations.append((command, kwargs))
        return self.processes[len(self.invocations) - 1]


@pytest.fixture
def host(monkeypatch, tmp_path):
    from app.agent_runtime import a13n_adapter

    tenant = TenantConfig(tenant_id="contract-bot")
    events = []
    summaries = []
    monkeypatch.setattr(a13n_adapter, "get_current_tenant", lambda: tenant)
    monkeypatch.setattr(a13n_adapter, "record_runtime_event", lambda item: events.append(item.to_dict()))
    if hasattr(a13n_adapter, "record_runtime_run_summary"):
        monkeypatch.setattr(a13n_adapter, "record_runtime_run_summary", lambda item: summaries.append(item))

    def create(*processes, handlers=None, visible=None, timeout=1):
        factory = ProcessFactory(*processes)
        available = toolset(handlers or {"think": lambda args: ToolResult.success("thought")}, visible)
        def build(tenant, **kwargs):
            available.tenant_id = tenant.tenant_id
            return available

        async def prompt(value, visible):
            return "Use the current Host tools and permissions."

        client = a13n_adapter.A13nRuntimeClient(
            state_dir=tmp_path / "state", timeout_seconds=timeout, process_factory=factory,
            toolset_builder=build, prompt_builder=prompt,
        )
        return client, factory

    return create, tenant, events, summaries


@pytest.mark.parametrize("enabled,shadow,rollout,expected", [
    (False, False, 100, "legacy"), (True, False, 0, "legacy"),
    (True, True, 0, "legacy_shadow_a13n_harness"), (True, False, 100, "a13n_harness"),
])
def test_selector_a13n_requires_explicit_enable(enabled, shadow, rollout, expected):
    from app.hermes_runtime.selector import select_runtime
    tenant = TenantConfig(tenant_id="contract-bot", agent_runtime_provider="a13n_harness",
                          agent_runtime_enabled=enabled, agent_runtime_shadow=shadow,
                          agent_runtime_rollout_percent=rollout)
    assert select_runtime(tenant, sender_id="u1", run_id="r1") == expected
    assert TenantConfig(tenant_id="new").agent_runtime_enabled is False
    tenant.agent_runtime_enabled = False
    assert select_runtime(tenant, sender_id="u1") == "legacy"


def test_partial_rollout_is_stable_per_tenant_sender():
    from app.hermes_runtime.selector import select_runtime
    tenant = TenantConfig(tenant_id="contract-bot", agent_runtime_provider="a13n_harness",
                          agent_runtime_enabled=True, agent_runtime_rollout_percent=50)
    choices = [select_runtime(tenant, sender_id=f"u{index}", run_id="first") for index in range(100)]
    assert set(choices) == {"legacy", "a13n_harness"}
    assert choices == [select_runtime(tenant, sender_id=f"u{index}", run_id="second") for index in range(100)]


@pytest.mark.asyncio
async def test_dispatch_without_command_fails_without_sdk_import(monkeypatch, tmp_path):
    from app.agent_runtime.client import AgentRuntimeClient
    from app.agent_runtime import a13n_adapter
    imported = []
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "a13n" or name.startswith(("a13n.", "a13n_")):
            imported.append(name)
            raise AssertionError("Host must not import the optional SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setenv("A13N_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(a13n_adapter, "get_current_tenant", lambda: TenantConfig(tenant_id="contract-bot"))
    value = request()
    value.runtime.command = ""
    response = await AgentRuntimeClient(provider="a13n_harness").run_turn(value)
    assert response.runtime == "a13n_harness"
    assert response.status == "failed"
    assert response.error.code == "runtime_unavailable"
    assert response.resume["fallback_safe"] is True
    assert imported == []


@pytest.mark.asyncio
async def test_roundtrip_preserves_request_and_response_contract(host):
    create, _, _, _ = host
    process = ScriptedProcess(result={"status": "completed", "final_text": "done"},
                              state=json.dumps({"turn": 1}))
    client, factory = create(process)
    value = request()
    value.input.image_urls = ["https://example.test/image.png"]
    value.input.attachments = [{"kind": "domain_workflow", "workflow_id": "gdd.update"}]
    value.input.chat_context = "domain context"
    value.runtime.profile = "domain-contract"
    response = await client.run_turn(value)
    assert process.messages[0]["request"] == value.to_dict()
    assert process.messages[0]["protocol_version"] == 1
    assert response.run_id == value.run_id
    assert response.runtime == "a13n_harness"
    assert response.status == "completed"
    assert response.final_text == "done"
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 3
    assert response.usage.api_calls == 1
    assert RuntimeResponse.from_dict(response.to_dict()).to_dict() == response.to_dict()
    assert factory.invocations[0][0] == ("fake-a13n-worker",)


@pytest.mark.asyncio
@pytest.mark.parametrize("permission,executed,status", [
    ("allow", True, "completed"), ("deny", False, "completed"),
    ("ask", False, "needs_confirmation"), ("review", False, "needs_confirmation"),
])
async def test_host_enforces_permissions_even_if_worker_claims_completed(host, permission, executed, status):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"think": permission}
    calls = []
    process = ScriptedProcess([{"name": "think", "args": {"note": "a"}}])
    client, _ = create(process, handlers={"think": lambda args: calls.append(args) or "thought"})
    response = await client.run_turn(request())
    assert bool(calls) is executed
    assert response.status == status
    assert response.usage.tool_calls == 1
    result = process.tool_results[0]["result"]
    assert result["ok"] is executed
    if permission in {"ask", "review"}:
        assert response.resume["fallback_safe"] is False
        assert response.resume["resumable"] is False
        assert response.resume["pending_approvals"]
    assert process.messages[0]["tools"][0]["permission"] == permission


@pytest.mark.asyncio
async def test_bridge_confirmation_cannot_be_overridden_by_a13n_allow(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"send_message": "allow"}
    calls = []
    process = ScriptedProcess([{"name": "send_message", "args": {"text": "hello"}}])
    client, _ = create(process, handlers={"send_message": lambda args: calls.append(args) or "sent"})
    value = request(policy=RuntimePolicy(allowed_tool_names=["send_message"],
                                        requires_confirmation_for=["message_send"]))
    response = await client.run_turn(value)
    assert calls == []
    assert process.tool_results[0]["result"]["code"] == "confirmation_required"
    assert response.status == "needs_confirmation"
    assert response.resume["fallback_safe"] is False


@pytest.mark.asyncio
async def test_explicit_allow_cannot_override_disabled_auto_execute(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"send_message": "allow"}
    calls = []
    process = ScriptedProcess([{"name": "send_message", "args": {"text": "hello"}}])
    client, _ = create(process, handlers={"send_message": lambda args: calls.append(args) or "sent"})
    value = request(policy=RuntimePolicy(allowed_tool_names=["send_message"]))
    value.runtime.auto_execute = False
    response = await client.run_turn(value)
    assert calls == []
    assert process.messages[0]["tools"][0]["permission"] == "ask"
    assert response.status == "needs_confirmation"
    assert response.resume["fallback_safe"] is False


@pytest.mark.asyncio
async def test_execution_policy_is_reused_and_denies_disabled_terminal(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"local_agent_request": "allow"}
    calls = []
    process = ScriptedProcess([{"name": "local_agent_request", "args": {
        "tool": "bash.run", "tool_args": {"command": "pwd"},
    }}])
    client, _ = create(process, handlers={"local_agent_request": lambda args: calls.append(args) or "ran"})
    response = await client.run_turn(request(policy=RuntimePolicy(allowed_tool_names=["local_agent_request"])))
    assert calls == []
    assert process.tool_results[0]["result"]["code"] == "execution_disabled"
    assert response.tool_calls[0].status != "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("visible,allowed", [([], []), ([], ["think"]), (["think"], ["read_file"])])
async def test_empty_or_filtered_visible_set_never_opens_hidden_handlers(host, visible, allowed):
    create, _, _, _ = host
    calls = []
    process = ScriptedProcess([{"name": "think", "args": {}}])
    client, _ = create(process, handlers={"think": lambda args: calls.append(args) or "secret"}, visible=visible)
    response = await client.run_turn(request(policy=RuntimePolicy(allowed_tool_names=allowed)))
    assert calls == []
    assert process.messages[0]["tools"] == []
    assert process.tool_results[0]["result"]["ok"] is False
    assert response.tool_calls[0].status != "success"


@pytest.mark.asyncio
async def test_host_validates_arguments_without_trusting_worker_validation(host):
    create, _, _, _ = host
    calls = []
    process = ScriptedProcess([{"name": "think", "args": {"note": {"bad": "shape"}}}])
    client, _ = create(process)
    available = toolset({"think": lambda args: calls.append(args) or "thought"})
    available.tools[0].schema["function"]["parameters"] = {
        "type": "object", "properties": {"note": {"type": "string"}},
        "required": ["note"], "additionalProperties": False,
    }
    client.toolset_builder = lambda *args, **kwargs: available
    response = await client.run_turn(request())
    assert calls == []
    assert response.status == "completed"
    assert process.tool_results[0]["result"]["code"] == "invalid_param"
    assert response.tool_calls[0].status == "failed"


@pytest.mark.asyncio
async def test_saved_completed_state_restores_thread_with_current_policy(host):
    create, tenant, _, _ = host
    saved = json.dumps({"messages": ["first turn"], "policy": "stale"})
    first = ScriptedProcess(state=saved)
    second = ScriptedProcess([{"name": "think", "args": {}}], state=json.dumps({"messages": ["second"]}))
    client, _ = create(first, second)
    response1 = await client.run_turn(request("r1"))
    tenant.a13n_tool_permissions = {"think": "deny"}
    value2 = request("r2")
    value2.policy.admin = False
    response2 = await client.run_turn(value2)
    start1, start2 = first.messages[0], second.messages[0]
    assert start1["state"] is None
    assert start2["state"] == first.state
    assert start1["thread_id"] == start2["thread_id"]
    assert response1.resume["thread_id"] == response2.resume["thread_id"]
    assert start2["tools"][0]["permission"] == "deny"
    assert start2["request"]["policy"] == value2.to_dict()["policy"]
    assert second.tool_results[0]["result"]["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tenant", "channel", "sender", "shadow"])
async def test_state_is_scoped_to_tenant_channel_sender_and_shadow(host, change):
    create, tenant, _, _ = host
    first, second = ScriptedProcess(state="{}"), ScriptedProcess(state="{}")
    client, _ = create(first, second)
    response1 = await client.run_turn(request("r1"))
    value2 = request("r2")
    if change == "tenant":
        value2.tenant_id = "other-bot"
        tenant.tenant_id = "other-bot"
    elif change == "channel":
        value2.channel_id = "other-channel"
    elif change == "sender":
        value2.sender = RuntimeSender(sender_id="u2", identity_id="feishu:u2")
    else:
        value2.policy.shadow_mode = True
    response2 = await client.run_turn(value2)
    assert first.messages[0]["thread_id"] != second.messages[0]["thread_id"]
    assert second.messages[0]["state"] is None
    assert response1.resume["thread_id"] != response2.resume["thread_id"]


@pytest.mark.asyncio
async def test_fresh_start_discards_old_state(host):
    create, _, _, _ = host
    first, second = ScriptedProcess(state="{}"), ScriptedProcess(state="{}")
    client, _ = create(first, second)
    await client.run_turn(request("r1"))
    next_turn = request("r2")
    next_turn.conversation.fresh_start = True
    response = await client.run_turn(next_turn)
    assert response.status == "completed"
    assert second.messages[0]["state"] is None


@pytest.mark.asyncio
async def test_same_run_is_cached_but_changed_payload_cannot_reexecute(host):
    create, _, _, _ = host
    process = ScriptedProcess([{"name": "think", "args": {}}], state="{}")
    calls = []
    client, factory = create(process, handlers={"think": lambda args: calls.append(args) or "done"})
    value = request()
    first = await client.run_turn(value)
    cached = await client.run_turn(value)
    changed = copy.deepcopy(value)
    changed.input.text = "changed"
    rejected = await client.run_turn(changed)
    assert first.to_dict() == cached.to_dict()
    assert len(calls) == len(factory.invocations) == 1
    assert rejected.status == "failed"
    assert rejected.error.retryable is False
    assert rejected.resume["fallback_safe"] is False


@pytest.mark.asyncio
async def test_approval_state_is_not_committed_or_automatically_resumed(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"think": "ask"}
    process = ScriptedProcess([{"name": "think", "args": {}}], state='{"pending":"approval"}')
    client, factory = create(process)
    blocked = await client.run_turn(request("r1"))
    tenant.a13n_tool_permissions = {"think": "allow"}
    again = await client.run_turn(request("r2"))
    assert blocked.status == "needs_confirmation"
    assert again.status == "failed"
    assert again.resume["fallback_safe"] is False
    assert len(factory.invocations) == 1
    assert list(client.store.root.rglob("state.json")) == []


@pytest.mark.asyncio
async def test_explicit_fresh_start_can_abandon_unexecuted_approval(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"think": "ask"}
    first = ScriptedProcess([{"name": "think", "args": {}}])
    second = ScriptedProcess(state="{}")
    client, factory = create(first, second)
    await client.run_turn(request("approval-r1"))
    tenant.a13n_tool_permissions = {"think": "allow"}
    value = request("approval-r2")
    value.conversation.fresh_start = True
    response = await client.run_turn(value)
    assert response.status == "completed"
    assert len(factory.invocations) == 2
    assert second.messages[0]["state"] is None


@pytest.mark.asyncio
async def test_approval_cache_write_failure_cannot_enable_legacy_bypass(host, monkeypatch):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"think": "ask"}
    calls = []
    process = ScriptedProcess([{"name": "think", "args": {}}])
    client, factory = create(process, handlers={"think": lambda args: calls.append(args) or "thought"})
    real_write = client.store.write

    def failing_cache(thread_id, filename, value):
        if filename.startswith("run-"):
            raise OSError("checkpoint device unavailable")
        return real_write(thread_id, filename, value)

    monkeypatch.setattr(client.store, "write", failing_cache)
    response = await client.run_turn(request("approval-storage-failure"))
    assert calls == []
    assert response.status == "failed"
    assert response.error.code == "a13n_state_unavailable"
    assert response.resume["fallback_safe"] is False
    assert response.error.retryable is False
    assert len(factory.invocations) == 1


@pytest.mark.asyncio
async def test_unknown_side_effect_blocks_retry_next_turn_and_fallback(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"send_message": "allow"}
    calls = []

    def unknown_effect(args):
        calls.append(args)
        raise RuntimeError("connection dropped after delivery")

    process = ScriptedProcess([{"name": "send_message", "args": {"text": "only once"}}], state="{}")
    client, factory = create(process, handlers={"send_message": unknown_effect})
    value = request("r1", policy=RuntimePolicy(allowed_tool_names=["send_message"]))
    response = await client.run_turn(value)
    retried = await client.run_turn(value)
    next_turn = copy.deepcopy(value)
    next_turn.run_id = "r2"
    blocked = await client.run_turn(next_turn)
    assert len(calls) == len(factory.invocations) == 1
    assert response.error.code == "reconciliation_required"
    assert response.usage.tool_calls == 1
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].status == "failed"
    for item in (response, retried, blocked):
        assert item.status == "failed"
        assert item.resume["fallback_safe"] is False
        assert item.resume["reconciliation_required"] is True
        assert item.error.retryable is False


@pytest.mark.asyncio
async def test_removed_worker_command_cannot_hide_unresolved_effect_journal(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"send_message": "allow"}
    calls = []

    def unknown_effect(args):
        calls.append(args)
        raise RuntimeError("lost delivery acknowledgement")

    process = ScriptedProcess([{"name": "send_message", "args": {"text": "sent once"}}])
    client, factory = create(process, handlers={"send_message": unknown_effect})
    first = request("unresolved-1", policy=RuntimePolicy(allowed_tool_names=["send_message"]))
    await client.run_turn(first)
    after_config_change = copy.deepcopy(first)
    after_config_change.run_id = "unresolved-2"
    after_config_change.runtime.command = ""
    response = await client.run_turn(after_config_change)
    assert response.status == "failed"
    assert response.resume["fallback_safe"] is False
    assert response.resume["reconciliation_required"] is True
    assert response.error.retryable is False
    assert len(calls) == len(factory.invocations) == 1


@pytest.mark.asyncio
async def test_cached_old_safe_failure_cannot_bypass_newer_unresolved_effect(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"send_message": "allow"}
    calls = []

    def unknown_effect(args):
        calls.append(args)
        raise RuntimeError("delivery status unknown")

    first_process = ScriptedProcess(result={"status": "failed", "error_code": "runtime_failed"})
    second_process = ScriptedProcess([{"name": "send_message", "args": {"text": "sent once"}}])
    client, factory = create(first_process, second_process,
                            handlers={"think": lambda args: "thought", "send_message": unknown_effect})
    old_request = request("old-safe-run")
    initial = await client.run_turn(old_request)
    assert initial.resume["fallback_safe"] is True
    effect_request = request("new-uncertain-run", policy=RuntimePolicy(allowed_tool_names=["send_message"]))
    effect_response = await client.run_turn(effect_request)
    assert effect_response.resume["fallback_safe"] is False
    redelivered = await client.run_turn(old_request)
    assert redelivered.status == "failed"
    assert redelivered.resume["fallback_safe"] is False
    assert redelivered.resume["reconciliation_required"] is True
    assert redelivered.error.retryable is False
    assert len(calls) == 1 and len(factory.invocations) == 2


@pytest.mark.asyncio
async def test_shadow_never_dispatches_side_effects(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"send_message": "allow"}
    calls = []
    process = ScriptedProcess([{"name": "send_message", "args": {"text": "hello"}}])
    client, _ = create(process, handlers={"send_message": lambda args: calls.append(args) or "sent"})
    await client.run_turn(request(policy=RuntimePolicy(allowed_tool_names=["send_message"], shadow_mode=True)))
    assert calls == []
    assert process.tool_results[0]["result"]["code"] in {"shadow_side_effect_denied", "policy_denied"}


@pytest.mark.asyncio
async def test_timeout_reaps_worker(host):
    create, _, _, _ = host
    process = ScriptedProcess(hang=True)
    client, factory = create(process, timeout=.02)
    response = await client.run_turn(request())
    assert response.status == "failed"
    assert response.error.code == "timed_out"
    assert process.killed and process.waited
    assert response.resume["fallback_safe"] is True
    assert len(factory.invocations) == 1


@pytest.mark.asyncio
async def test_timeout_after_side_effect_blocks_fallback_and_a_new_worker(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"send_message": "allow"}
    calls = []
    process = ScriptedProcess([{"name": "send_message", "args": {"text": "sent once"}}], hang_after_tools=True)
    client, factory = create(process, handlers={"send_message": lambda args: calls.append(args) or "sent"}, timeout=1)
    value = request("effect-timeout", policy=RuntimePolicy(allowed_tool_names=["send_message"]))
    response = await client.run_turn(value)
    value2 = copy.deepcopy(value)
    value2.run_id = "effect-timeout-new"
    value2.conversation.fresh_start = True
    blocked = await client.run_turn(value2)
    assert process.killed and process.waited
    assert response.error.code == "timed_out"
    assert response.usage.tool_calls == 1
    assert len(response.tool_calls) == 1
    for item in (response, blocked):
        assert item.resume["fallback_safe"] is False
        assert item.resume["reconciliation_required"] is True
        assert item.error.retryable is False
    assert len(calls) == len(factory.invocations) == 1


@pytest.mark.asyncio
async def test_crash_journal_survives_new_client_and_fresh_start(host):
    create, _, _, _ = host
    first, first_factory = create()
    from app.agent_runtime.a13n_adapter import _thread_id
    value = request("next-after-crash")
    thread_id = _thread_id(value)
    first.store.write(thread_id, "active.json", {"run_id": "lost-worker", "status": "side_effect_started",
                                                "side_effect_started": True})
    # A new client reads the persisted journal, not an in-memory flag.
    restarted, restarted_factory = create()
    value.conversation.fresh_start = True
    response = await restarted.run_turn(value)
    assert response.error.code == "reconciliation_required"
    assert response.resume["fallback_safe"] is False
    assert not first_factory.invocations and not restarted_factory.invocations


@pytest.mark.asyncio
async def test_cancel_reaps_worker_and_preserves_cancellation(host):
    create, _, _, _ = host
    process = ScriptedProcess(hang=True)
    client, _ = create(process, timeout=10)
    turn = asyncio.create_task(client.run_turn(request()))
    await process.started.wait()
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert process.killed and process.waited


@pytest.mark.asyncio
async def test_cancellation_during_effect_keeps_journal_and_blocks_new_turn(host):
    create, tenant, _, _ = host
    tenant.a13n_tool_permissions = {"send_message": "allow"}
    started = asyncio.Event()
    calls = []

    async def incomplete_effect(args):
        calls.append(args)
        started.set()
        await asyncio.Event().wait()

    process = ScriptedProcess([{"name": "send_message", "args": {"text": "delivery unknown"}}])
    client, factory = create(process, handlers={"send_message": incomplete_effect}, timeout=10)
    value = request("cancel-effect", policy=RuntimePolicy(allowed_tool_names=["send_message"]))
    turn = asyncio.create_task(client.run_turn(value))
    await asyncio.wait_for(started.wait(), 1)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert process.killed and process.waited
    following = copy.deepcopy(value)
    following.run_id = "after-cancel-effect"
    following.conversation.fresh_start = True
    response = await client.run_turn(following)
    assert response.resume["fallback_safe"] is False
    assert response.resume["reconciliation_required"] is True
    assert len(calls) == len(factory.invocations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"log on protocol stdout\n", b'{"type":"unknown"}\n',
                                      b'{"type":"tool_call","name":"think","args":[]}\n'])
async def test_malformed_protocol_cannot_dispatch_handler(host, raw):
    create, _, _, _ = host
    process = ScriptedProcess(raw=raw)
    calls = []
    client, _ = create(process, handlers={"think": lambda args: calls.append(args) or "done"})
    response = await client.run_turn(request())
    assert calls == []
    assert response.status == "failed"
    assert response.error.code == "a13n_protocol_error"
    assert process.waited


@pytest.mark.asyncio
async def test_oversize_protocol_is_rejected(host):
    from app.agent_runtime import a13n_adapter
    create, _, _, _ = host
    limit = getattr(a13n_adapter, "MAX_LINE_BYTES", 1024 * 1024)
    process = ScriptedProcess(raw=b"x" * (limit + 1) + b"\n")
    client, _ = create(process)
    response = await client.run_turn(request())
    assert response.status == "failed"
    assert response.error.code == "a13n_protocol_error"
    assert process.waited


@pytest.mark.asyncio
async def test_observability_correlates_usage_without_contents_or_worker_secrets(host, monkeypatch):
    create, _, events, summaries = host
    secret = "sk-example-never-persist-in-observability"
    monkeypatch.setenv("UNRELATED_API_KEY", secret)
    process = ScriptedProcess([{"name": "think", "args": {"secret": secret}}],
                              result={"status": "completed", "final_text": secret,
                                      "arbitrary_worker_secret": secret}, state=json.dumps({"secret": secret}))
    client, factory = create(process, handlers={"think": lambda args: secret})
    value = request(input=RuntimeInput(text=secret, chat_context=secret))
    response = await client.run_turn(value)
    serialized = json.dumps({"events": events, "summaries": summaries, "response_events": response.events})
    assert secret not in serialized
    assert response.resume["thread_id"] in serialized
    assert "sdk-run-1" in serialized
    assert "tool_calls" in serialized
    assert "input_tokens" in serialized
    assert all(item["run_id"] == value.run_id for item in events)
    assert all(item["runtime"] == "a13n_harness" for item in events)
    assert "UNRELATED_API_KEY" not in factory.invocations[0][1]["env"]


@pytest.mark.asyncio
async def test_replay_fixture_drives_host_tool_bridge_and_benchmark_detects_divergence(host, tmp_path):
    create, _, _, _ = host
    fixture = json.loads(FIXTURE.read_text())
    reference = replay_scenario(fixture)
    observed = []

    def read_file(args):
        observed.append(("read_file", args))
        return ToolResult.success(f"Artifact {args['path']}: ledger_id=ledger-evidence-7.")

    def think(args):
        observed.append(("think", args))
        return ToolResult.success(f"Plan: {args['note']}.")

    processes = [ScriptedProcess([{"name": item.name, "args": item.arguments} for item in reference.tool_calls],
                                 assemble=True, state="{}") for _ in range(2)]
    client, factory = create(*processes, handlers={"read_file": read_file, "think": think})

    async def adapter_replay(scenario, *, recorder=None):
        value = request(f"benchmark-{len(factory.invocations)}", input=RuntimeInput(text=scenario["user_text"]),
                        policy=RuntimePolicy(allowed_tool_names=scenario["expected_visible_tools"]))
        response = await client.run_turn(value)
        process = processes[len(factory.invocations) - 1]
        actual_outputs = [item["result"]["content"] for item in process.tool_results]
        assert response.status == "completed"
        if recorder:
            recorder.record("adapter_result", {"status": response.status, "thread_id": response.resume["thread_id"]})
        return {"final": response.final_text, "call_sequence": [item.tool_name for item in response.tool_calls],
                "visible_tools": [item["name"] for item in process.messages[0]["tools"]],
                "ledger_ids": re.findall(r"ledger_id=(ledger-[\w-]+)", "\n".join(actual_outputs))}

    summary = await run_benchmark_suite(FIXTURE, tmp_path / "baseline", replay_fn=adapter_replay)
    assert summary["passed"] == 1
    assert [name for name, _ in observed] == reference.call_sequence
    assert [item["result"]["content"] for item in processes[0].tool_results] == [item.text for item in reference.tool_outputs]
    # Keep the same recorded model requests, but change a Host handler's output.
    # A benchmark that merely reads the expected transcript would still pass.
    client.toolset_builder = lambda *args, **kwargs: toolset({"read_file": lambda args: "wrong evidence", "think": think})
    divergent = await run_benchmark_suite(FIXTURE, tmp_path / "divergent", replay_fn=adapter_replay)
    assert divergent["failed"] == 1
    assert any("final:" in item for item in divergent["cases"][0]["failures"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status,fallback_safe,expected", [
    ("failed", True, None), ("awaiting_approval", False, "审批"), ("needs_confirmation", False, "审批"),
    ("failed", False, "核对"),
])
async def test_router_retains_safe_fallback_but_never_bypasses_gates(monkeypatch, status, fallback_safe, expected):
    from app.router.intent import _try_agent_runtime
    tenant = TenantConfig(tenant_id="contract-bot", tools_enabled=["think"],
                          agent_runtime_provider="a13n_harness", agent_runtime_enabled=True,
                          agent_runtime_rollout_percent=100)

    async def fake_run(self, value):
        return RuntimeResponse(run_id=value.run_id, runtime="a13n_harness", status=status,
                               resume={"fallback_safe": fallback_safe})

    monkeypatch.setattr("app.agent_runtime.client.AgentRuntimeClient.run_turn", fake_run)
    monkeypatch.setattr("app.hermes_runtime.observability.record_runtime_event", lambda item: None)
    result = await _try_agent_runtime(tenant=tenant, user_text="hello", sender_id="u1", sender_name="Steven",
        history_key="u1", chat_id="", chat_type="", mode="safe", chat_context="", image_urls=None,
        run_id="route-1", channel_platform="feishu")
    if expected is None:
        assert result is None
    else:
        assert expected in result


@pytest.mark.asyncio
async def test_router_preserves_fresh_start_in_adapter_contract(monkeypatch):
    from app.router.intent import _try_agent_runtime
    tenant = TenantConfig(tenant_id="contract-bot", tools_enabled=["think"],
                          agent_runtime_provider="a13n_harness", agent_runtime_enabled=True,
                          agent_runtime_rollout_percent=100)
    seen = []

    async def fake_run(self, value):
        seen.append(value)
        return RuntimeResponse(run_id=value.run_id, runtime="a13n_harness", final_text="new conversation")

    monkeypatch.setattr("app.agent_runtime.client.AgentRuntimeClient.run_turn", fake_run)
    monkeypatch.setattr("app.hermes_runtime.observability.record_runtime_event", lambda item: None)
    await _try_agent_runtime(tenant=tenant, user_text="start over and say hello", sender_id="u1", sender_name="Steven",
        history_key="u1", chat_id="", chat_type="", mode="safe", chat_context="", image_urls=None,
        run_id="fresh-route", channel_platform="feishu")
    assert seen[0].conversation.fresh_start is True


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("A13N_TEST_PYTHON"), reason="Set A13N_TEST_PYTHON to isolated Python 3.13 SDK environment")
async def test_real_sdk_subprocess_saves_and_restores_with_current_host_permissions(host, tmp_path):
    from app.agent_runtime.a13n_adapter import A13nRuntimeClient
    _, tenant, _, _ = host
    worker_path = Path(__file__).resolve().parents[1] / "scripts" / "a13n_worker.py"
    launcher = tmp_path / "injected_worker.py"
    launcher.write_text(
        "import asyncio, importlib.util, json\n"
        "from pydantic_ai.models.function import FunctionModel, DeltaToolCall\n"
        "from pydantic_ai.messages import ToolReturnPart\n"
        f"spec = importlib.util.spec_from_file_location('isolated_worker', {str(worker_path)!r})\n"
        "worker = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(worker)\n"
        "async def respond(messages, info):\n"
        "    returns = [p for p in messages[-1].parts if isinstance(p, ToolReturnPart)]\n"
        "    if returns:\n"
        "        yield returns[-1].content['content']\n"
        "    else:\n"
        "        name = info.function_tools[0].name\n"
        "        args = {'path': 'delivery.md'} if name == 'read_file' else {'note': 'Turn two'}\n"
        "        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id='call-one')}\n"
        "async def main():\n"
        "    transport = worker.StdioTransport()\n"
        "    await worker.run_worker(await transport.read(), transport,\n"
        "        model_factory=lambda start: FunctionModel(stream_function=respond))\n"
        "asyncio.run(main())\n",
        encoding="utf-8",
    )
    calls = []

    def read_file(args):
        calls.append(("read_file", args))
        return ToolResult.success("evidence from host")

    def think(args):
        calls.append(("think", args))
        return ToolResult.success("second turn from host")

    available = toolset({"read_file": read_file})
    async def prompt(value, visible):
        return "Use the current Host tools and permissions."

    client = A13nRuntimeClient(state_dir=tmp_path / "sdk-state", timeout_seconds=30,
                               toolset_builder=lambda *args, **kwargs: available, prompt_builder=prompt)
    first_request = request("sdk-r1", policy=RuntimePolicy(allowed_tool_names=["read_file"]))
    first_request.runtime.command = os.environ["A13N_TEST_PYTHON"]
    first_request.runtime.args = [str(launcher)]
    first = await client.run_turn(first_request)
    assert first.status == "completed", first.to_dict()
    assert first.final_text == "evidence from host"
    first_state = json.loads(next(client.store.root.rglob("state.json")).read_text())["state"]
    assert json.loads(first_state)["thread_id"] == first.resume["thread_id"]

    available = toolset({"think": think})
    second_request = copy.deepcopy(first_request)
    second_request.run_id = "sdk-r2"
    second_request.input.text = "now plan turn two"
    second_request.policy.allowed_tool_names = ["think"]
    second = await client.run_turn(second_request)
    assert second.status == "completed", second.to_dict()
    assert second.final_text == "second turn from host"
    assert second.resume["thread_id"] == first.resume["thread_id"]
    assert second.resume["sdk_run_id"] != first.resume["sdk_run_id"]
    second_state = json.loads(next(client.store.root.rglob("state.json")).read_text())["state"]
    assert len(second_state) > len(first_state)
    assert "evidence from host" in second_state
    assert "second turn from host" in second_state
    assert [name for name, _ in calls] == ["read_file", "think"]
    # Third turn can restore SDK memory, but cannot inherit the old tool grant.
    tenant.a13n_tool_permissions = {"think": "ask"}
    third_request = copy.deepcopy(second_request)
    third_request.run_id = "sdk-r3"
    third = await client.run_turn(third_request)
    assert third.status == "needs_confirmation", third.to_dict()
    assert third.resume["fallback_safe"] is False
    assert [name for name, _ in calls] == ["read_file", "think"]
    assert json.loads(next(client.store.root.rglob("state.json")).read_text())["state"] == second_state
