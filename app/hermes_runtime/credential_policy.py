from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderCredential:
    tenant_id: str
    provider: str
    credential_id: str
    secret_ref: str


def credential_pool_key(tenant_id: str, provider: str) -> str:
    return f"hermes:{tenant_id}:provider:{provider}:pool"


def credential_status_key(tenant_id: str, provider: str, credential_id: str) -> str:
    return f"{credential_pool_key(tenant_id, provider)}:{credential_id}:status"


def select_available_credential(
    tenant_id: str,
    provider: str,
    candidates: Iterable[ProviderCredential],
    *,
    exhausted_credential_ids: set[str] | None = None,
) -> ProviderCredential | None:
    exhausted = exhausted_credential_ids or set()
    for credential in candidates:
        if credential.tenant_id != tenant_id:
            continue
        if credential.provider != provider:
            continue
        if credential.credential_id in exhausted:
            continue
        return credential
    return None
