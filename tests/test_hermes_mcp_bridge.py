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


def test_mcp_discovery_exposes_prefixed_allowlisted_tools():
    from app.hermes_runtime.mcp_bridge import discover_mcp_tools
    from app.hermes_runtime.mcp_registry import McpServerConfig

    tenant = TenantConfig(
        tenant_id="code-bot",
        mcp_enabled=True,
        mcp_servers_enabled=["github"],
        mcp_tool_allowlist=["mcp_github_search_repos"],
    )
    servers = [
        McpServerConfig(
            server_id="github",
            label="GitHub",
            command="mcp-github",
            allowed_tenants=["code-bot"],
        )
    ]

    toolset = discover_mcp_tools(
        tenant,
        servers,
        {
            "github": [
                {
                    "name": "search_repos",
                    "description": "Search repositories",
                    "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
                {
                    "name": "delete_repo",
                    "description": "Delete a repository",
                    "input_schema": {"type": "object"},
                },
            ]
        },
    )

    assert [tool.name for tool in toolset.tools] == ["mcp_github_search_repos"]
    assert toolset.tools[0].schema["function"]["name"] == "mcp_github_search_repos"
    assert toolset.tools[0].schema["function"]["parameters"]["properties"]["q"]["type"] == "string"


def test_mcp_call_missing_env_ref_returns_structured_error():
    from app.hermes_runtime.mcp_bridge import execute_mcp_tool_call
    from app.hermes_runtime.mcp_registry import McpServerConfig

    tenant = TenantConfig(
        tenant_id="code-bot",
        mcp_enabled=True,
        mcp_servers_enabled=["github"],
        mcp_tool_allowlist=["mcp_github_search_repos"],
    )
    server = McpServerConfig(
        server_id="github",
        label="GitHub",
        command="mcp-github",
        env_refs=["GITHUB_TOKEN"],
        allowed_tenants=["code-bot"],
    )

    result = execute_mcp_tool_call(
        tenant,
        [server],
        "mcp_github_search_repos",
        {"q": "hermes"},
        handlers={"github": lambda _tool, _args, _env: "should not run"},
        env={},
    )

    assert result.ok is False
    assert result.code == "mcp_missing_env"
    assert "GITHUB_TOKEN" in result.content
