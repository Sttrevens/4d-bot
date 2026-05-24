from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


def credential_pool_key(tenant_id: str, provider: str) -> str:
    return f"hermes:{tenant_id}:provider:{provider}:pool"


def credential_status_key(tenant_id: str, provider: str, credential_id: str) -> str:
    return f"{credential_pool_key(tenant_id, provider)}:{credential_id}:status"


@dataclass(frozen=True, slots=True)
class CredentialCandidate:
    tenant_id: str
    provider: str
    model: str
    credential_ref: str
    credential_id: str = "primary"
    reason: str = "base"


@dataclass(frozen=True, slots=True)
class ProviderCredential:
    tenant_id: str
    provider: str
    credential_id: str
    secret_ref: str


@dataclass(frozen=True, slots=True)
class CredentialSelection:
    ok: bool
    candidate: CredentialCandidate | None = None
    error_code: str = ""
    message: str = ""
    retryable: bool = False


class CredentialExhaustion:
    """Tracks exhausted runtime credentials without crossing tenant boundaries."""

    def __init__(self, exhausted: dict[tuple[str, str, str], str] | None = None) -> None:
        self._exhausted = dict(exhausted or {})

    def mark_exhausted(
        self,
        *,
        tenant_id: str,
        provider: str,
        credential_id: str,
        reason: str,
    ) -> None:
        self._exhausted[(tenant_id, provider, credential_id)] = reason

    def is_exhausted(self, *, tenant_id: str, provider: str, credential_id: str) -> bool:
        return (tenant_id, provider, credential_id) in self._exhausted

    def reason_for(self, *, tenant_id: str, provider: str, credential_id: str) -> str:
        return self._exhausted.get((tenant_id, provider, credential_id), "")


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


def select_credential_candidate(
    candidates: list[CredentialCandidate],
    *,
    tenant_id: str,
    provider: str,
    exhaustion: CredentialExhaustion | None = None,
) -> CredentialSelection:
    state = exhaustion or CredentialExhaustion()
    scoped_candidates = [
        candidate
        for candidate in candidates
        if candidate.tenant_id == tenant_id and candidate.provider == provider
    ]
    for candidate in scoped_candidates:
        if not state.is_exhausted(
            tenant_id=candidate.tenant_id,
            provider=candidate.provider,
            credential_id=candidate.credential_id,
        ):
            return CredentialSelection(ok=True, candidate=candidate)
    return CredentialSelection(
        ok=False,
        error_code="provider_exhausted",
        message=f"No available {provider} credentials remain for tenant {tenant_id}.",
        retryable=True,
    )
