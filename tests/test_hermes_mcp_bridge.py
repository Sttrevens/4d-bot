from app.tenant.config import TenantConfig


def test_mcp_registry_hidden_when_tenant_disabled():
    from app.hermes_runtime.mcp_registry import McpServerConfig, visible_servers_for_tenant

    tenant = TenantConfig(tenant_id="pm-bot", mcp_enabled=False, mcp_servers_enabled=["github"])
    servers = [McpServerConfig(server_id="github", label="GitHub", command="mcp-github")]

    assert visible_servers_for_tenant(tenant, servers) == []


def test_mcp_registry_returns_only_enabled_servers():
    from app.hermes_runtime.mcp_registry import McpServerConfig, visible_servers_for_tenant

    tenant = TenantConfig(tenant_id="code-bot", mcp_enabled=True, mcp_servers_enabled=["github"])
    servers = [
        McpServerConfig(server_id="github", label="GitHub", command="mcp-github"),
        McpServerConfig(server_id="slack", label="Slack", command="mcp-slack"),
    ]

    visible = visible_servers_for_tenant(tenant, servers)

    assert [server.server_id for server in visible] == ["github"]


def test_mcp_tool_names_are_prefixed():
    from app.hermes_runtime.mcp_bridge import prefix_tool_name

    assert prefix_tool_name("github", "search_repos") == "mcp_github_search_repos"
