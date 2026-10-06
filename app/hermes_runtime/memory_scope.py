from __future__ import annotations

import hashlib
from dataclasses import dataclass


def _short_hash(value: str) -> str:
    if not value:
        return "self"
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:12]


def build_session_id(
    *,
    tenant_id: str,
    channel_id: str,
    scope: str,
    identity_id: str,
    chat_id: str = "",
) -> str:
    return f"hr:{tenant_id}:{channel_id}:{scope}:{_short_hash(identity_id)}:{_short_hash(chat_id)}"


@dataclass(frozen=True, slots=True)
class MemoryScope:
    tenant_id: str
    session_id: str
    identity_id: str
    sender_name: str = ""
    chat_id: str = ""
    channel_id: str = ""
