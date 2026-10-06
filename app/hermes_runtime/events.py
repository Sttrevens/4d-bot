from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


_SECRET_MARKERS = ("key", "token", "secret", "password", "authorization", "cookie")
_SAFE_TOKEN_FIELDS = {"input_tokens", "output_tokens", "total_tokens"}


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        lowered = key.lower()
        if lowered in _SAFE_TOKEN_FIELDS:
            redacted[key] = value
        elif any(marker in lowered for marker in _SECRET_MARKERS):
            redacted[key] = "[REDACTED]"
        elif isinstance(value, dict):
            redacted[key] = redact_payload(value)
        else:
            redacted[key] = value
    return redacted


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    run_id: str
    tenant_id: str
    event: str
    channel_id: str = ""
    session_id: str = ""
    platform: str = ""
    level: str = "info"
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    tool: str = ""
    provider: str = ""
    error: str = ""
    cost: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    runtime: str = "hermes_sidecar"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "channel_id": self.channel_id,
            "session_id": self.session_id,
            "platform": self.platform,
            "event": self.event,
            "level": self.level,
            "message": self.message,
            "payload": self.payload,
            "tool": self.tool,
            "provider": self.provider,
            "error": self.error,
            "cost": self.cost,
            "ts": self.ts,
            "runtime": self.runtime,
        }


def make_runtime_event(
    *,
    run_id: str,
    tenant_id: str,
    event: str,
    channel_id: str = "",
    session_id: str = "",
    platform: str = "",
    level: str = "info",
    message: str = "",
    payload: dict[str, Any] | None = None,
    tool: str = "",
    provider: str = "",
    error: str = "",
    cost: dict[str, Any] | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        run_id=run_id,
        tenant_id=tenant_id,
        event=event,
        channel_id=channel_id,
        session_id=session_id,
        platform=platform,
        level=level,
        message=message,
        payload=redact_payload(payload or {}),
        tool=tool,
        provider=provider,
        error=error,
        cost=redact_payload(cost or {}),
    )
