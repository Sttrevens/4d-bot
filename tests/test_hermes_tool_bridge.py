import asyncio
import time

from app.tenant.config import TenantConfig
from app.tools.tool_result import ToolResult


def test_build_runtime_toolset_uses_tenant_scoped_tools(monkeypatch):
    from app.hermes_runtime.tool_bridge import build_runtime_toolset

    def fake_get_tenant_tools(tenant, user_text="", override_groups=None, suggested_groups=None):
        return (
            [{"type": "function", "function": {"name": "think", "description": "Think", "parameters": {}}}],
            {"think": lambda args: ToolResult.success("ok")},
        )

    monkeypatch.setattr("app.services.base_agent._get_tenant_tools", fake_get_tenant_tools)
    tenant = TenantConfig(tenant_id="pm-bot", tools_enabled=["think"])

    toolset = build_runtime_toolset(tenant, user_text="hello")

    assert [tool.name for tool in toolset.tools] == ["think"]
    assert set(toolset.handlers) == {"think"}


def test_execute_tool_call_denies_tool_not_in_policy(monkeypatch):
    from app.hermes_runtime.tool_bridge import RuntimeToolset, RuntimeToolSchema, execute_tool_call
    from app.hermes_runtime.types import RuntimePolicy

    toolset = RuntimeToolset(
        tools=[RuntimeToolSchema(name="think", schema={"name": "think"})],
        handlers={"think": lambda args: ToolResult.success("ok")},
    )
    policy = RuntimePolicy(allowed_tool_names=["web_search"])

    result = execute_tool_call(toolset, policy, "think", {})

    assert result.ok is False
    assert result.code == "policy_denied"
    assert "think" in result.content


def test_execute_tool_call_normalizes_tool_result():
    from app.hermes_runtime.tool_bridge import RuntimeToolset, RuntimeToolSchema, execute_tool_call
    from app.hermes_runtime.types import RuntimePolicy

    toolset = RuntimeToolset(
        tools=[RuntimeToolSchema(name="think", schema={"name": "think"})],
        handlers={"think": lambda args: ToolResult.invalid_param("bad args", retry_hint="pass text")},
    )
    policy = RuntimePolicy(allowed_tool_names=["think"])

    result = execute_tool_call(toolset, policy, "think", {})

    assert result.ok is False
    assert result.code == "invalid_param"
    assert result.retry_hint == "pass text"
    assert result.side_effect is False


def test_execute_tool_call_injects_runtime_tenant_for_repo_skill_tools():
    from app.hermes_runtime.tool_bridge import RuntimeToolset, execute_tool_call
    from app.hermes_runtime.types import RuntimePolicy

    seen_args = {}

    def read_agent_skill_file(args):
        seen_args.update(args)
        return ToolResult.success(args["tenant_id"])

    toolset = RuntimeToolset(
        tenant_id="tenant-a",
        handlers={"read_agent_skill_file": read_agent_skill_file},
    )
    policy = RuntimePolicy(allowed_tool_names=["read_agent_skill_file"])

    result = execute_tool_call(
        toolset,
        policy,
        "read_agent_skill_file",
        {"tenant_id": "tenant-b", "skill_name": "demo", "path": "SKILL.md"},
    )

    assert result.ok
    assert result.content == "tenant-a"
    assert seen_args["tenant_id"] == "tenant-a"


async def test_execute_tool_calls_injects_runtime_tenant_for_async_repo_skill_tools():
    from app.hermes_runtime.tool_bridge import RuntimeToolset, execute_tool_calls
    from app.hermes_runtime.types import RuntimePolicy

    seen_args = {}

    async def read_agent_skill_file(args):
        seen_args.update(args)
        return ToolResult.success(args["tenant_id"])

    toolset = RuntimeToolset(
        tenant_id="tenant-a",
        handlers={"read_agent_skill_file": read_agent_skill_file},
    )
    policy = RuntimePolicy(allowed_tool_names=["read_agent_skill_file"])

    results = await execute_tool_calls(
        toolset,
        policy,
        [
            {
                "tool_name": "read_agent_skill_file",
                "args": {"tenant_id": "tenant-b", "skill_name": "demo", "path": "SKILL.md"},
            }
        ],
    )

    assert results[0].ok
    assert results[0].content == "tenant-a"
    assert seen_args["tenant_id"] == "tenant-a"


def test_execute_tool_call_blocks_side_effects_in_shadow_mode():
    from app.hermes_runtime.tool_bridge import RuntimeToolset, RuntimeToolSchema, execute_tool_call
    from app.hermes_runtime.types import RuntimePolicy

    called = False

    def export_file(_args):
        nonlocal called
        called = True
        return ToolResult.success("created")

    toolset = RuntimeToolset(
        tools=[RuntimeToolSchema(name="export_file", schema={"name": "export_file"})],
        handlers={"export_file": export_file},
    )
    policy = RuntimePolicy(allowed_tool_names=["export_file"], shadow_mode=True)

    result = execute_tool_call(toolset, policy, "export_file", {"filename": "shadow.csv"})

    assert result.ok is False
    assert result.code == "shadow_side_effect_denied"
    assert result.outcome == "blocked"
    assert result.side_effect is True
    assert called is False


def test_tool_policy_serializes_writes_to_same_target():
    from app.hermes_runtime.tool_policy import tool_concurrency_key

    a = tool_concurrency_key("update_calendar_event", {"event_id": "evt-1"})
    b = tool_concurrency_key("update_calendar_event", {"event_id": "evt-1"})
    c = tool_concurrency_key("read_file", {"path": "README.md"})

    assert a == b
    assert a
    assert c == ""


async def test_execute_tool_calls_awaits_async_handlers_and_preserves_order():
    from app.hermes_runtime.tool_bridge import RuntimeToolset, execute_tool_calls
    from app.hermes_runtime.types import RuntimePolicy

    async def slow_read(args):
        await asyncio.sleep(0.03)
        return ToolResult.success(f"slow {args['value']}")

    async def fast_read(args):
        await asyncio.sleep(0.01)
        return ToolResult.success(f"fast {args['value']}")

    toolset = RuntimeToolset(handlers={"read_file": slow_read, "web_search": fast_read})
    policy = RuntimePolicy(allowed_tool_names=["read_file", "web_search"])

    results = await execute_tool_calls(
        toolset,
        policy,
        [
            {"tool_name": "read_file", "args": {"value": "first"}},
            {"tool_name": "web_search", "args": {"value": "second"}},
        ],
    )

    assert [result.content for result in results] == ["slow first", "fast second"]
    assert all(result.ok for result in results)


async def test_execute_tool_calls_runs_reads_concurrently_and_serializes_same_target_writes():
    from app.hermes_runtime.tool_bridge import RuntimeToolset, execute_tool_calls
    from app.hermes_runtime.types import RuntimePolicy

    write_active = 0
    max_write_active = 0

    async def read_file(args):
        await asyncio.sleep(0.05)
        return ToolResult.success(args["path"])

    async def update_calendar_event(args):
        nonlocal write_active, max_write_active
        write_active += 1
        max_write_active = max(max_write_active, write_active)
        await asyncio.sleep(0.02)
        write_active -= 1
        return ToolResult.success(args["event_id"])

    toolset = RuntimeToolset(
        handlers={
            "read_file": read_file,
            "update_calendar_event": update_calendar_event,
        }
    )
    policy = RuntimePolicy(allowed_tool_names=["read_file", "update_calendar_event"])

    read_started = time.monotonic()
    read_results = await execute_tool_calls(
        toolset,
        policy,
        [
            {"tool_name": "read_file", "args": {"path": "a.md"}},
            {"tool_name": "read_file", "args": {"path": "b.md"}},
        ],
    )
    read_elapsed = time.monotonic() - read_started

    write_results = await execute_tool_calls(
        toolset,
        policy,
        [
            {"tool_name": "update_calendar_event", "args": {"event_id": "evt-1"}},
            {"tool_name": "update_calendar_event", "args": {"event_id": "evt-1"}},
        ],
    )

    assert [result.content for result in read_results] == ["a.md", "b.md"]
    assert read_elapsed < 0.09
    assert [result.content for result in write_results] == ["evt-1", "evt-1"]
    assert max_write_active == 1
