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


def test_tool_policy_serializes_writes_to_same_target():
    from app.hermes_runtime.tool_policy import tool_concurrency_key

    a = tool_concurrency_key("update_calendar_event", {"event_id": "evt-1"})
    b = tool_concurrency_key("update_calendar_event", {"event_id": "evt-1"})
    c = tool_concurrency_key("read_file", {"path": "README.md"})

    assert a == b
    assert a
    assert c == ""
