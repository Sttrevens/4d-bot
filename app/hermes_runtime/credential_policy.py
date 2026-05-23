from __future__ import annotations


def credential_pool_key(tenant_id: str, provider: str) -> str:
    return f"hermes:{tenant_id}:provider:{provider}:pool"


def credential_status_key(tenant_id: str, provider: str, credential_id: str) -> str:
    return f"{credential_pool_key(tenant_id, provider)}:{credential_id}:status"
