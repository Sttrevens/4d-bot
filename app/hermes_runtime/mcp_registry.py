from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    server_id: str
    label: str
    command: str
    args: list[str] = field(default_factory=list)
    env_refs: list[str] = field(default_factory=list)
    tool_prefix: str = ""
    default_enabled: bool = False
    allowed_tenants: list[str] = field(default_factory=list)
    risk_level: str = "yellow"
    description: str = ""


def visible_servers_for_tenant(tenant, servers: list[McpServerConfig]) -> list[McpServerConfig]:
    if not bool(getattr(tenant, "mcp_enabled", False)):
        return []
    enabled = set(getattr(tenant, "mcp_servers_enabled", []) or [])
    tenant_id = getattr(tenant, "tenant_id", "")
    visible = []
    for server in servers:
        if server.server_id not in enabled:
            continue
        if server.allowed_tenants and tenant_id not in set(server.allowed_tenants):
            continue
        visible.append(server)
    return visible
