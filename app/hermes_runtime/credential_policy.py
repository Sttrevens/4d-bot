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


def credential_id_from_secret_ref(secret_ref: str) -> str:
    parts = [part for part in secret_ref.split(":") if part]
    if len(parts) <= 3:
        return "primary"
    return parts[-1] or "primary"


def build_credential_candidates(
    tenant_id: str,
    provider: str,
    extra_secret_refs: Iterable[str] | None = None,
) -> list[ProviderCredential]:
    primary_ref = f"tenant:{tenant_id}:llm_api_key"
    scoped_prefix = f"tenant:{tenant_id}:"
    refs = [primary_ref]
    refs.extend(str(ref).strip() for ref in (extra_secret_refs or []) if str(ref).strip())

    candidates: list[ProviderCredential] = []
    seen_refs: set[str] = set()
    seen_ids: set[str] = set()
    for ref in refs:
        if ref in seen_refs or not ref.startswith(scoped_prefix):
            continue
        credential_id = "primary" if ref == primary_ref else credential_id_from_secret_ref(ref)
        if credential_id in seen_ids:
            continue
        candidates.append(
            ProviderCredential(
                tenant_id=tenant_id,
                provider=provider,
                credential_id=credential_id,
                secret_ref=ref,
            )
        )
        seen_refs.add(ref)
        seen_ids.add(credential_id)
    return candidates


def credential_candidate_dicts(candidates: Iterable[ProviderCredential]) -> list[dict[str, str]]:
    return [
        {
            "tenant_id": credential.tenant_id,
            "provider": credential.provider,
            "credential_id": credential.credential_id,
            "secret_ref": credential.secret_ref,
        }
        for credential in candidates
    ]


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
