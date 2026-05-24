from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


_SECRET_MARKERS = ("key", "token", "secret", "password", "authorization", "cookie")


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
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
    level: str = "info"
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    runtime: str = "hermes_sidecar"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "event": self.event,
            "level": self.level,
            "message": self.message,
            "payload": self.payload,
            "ts": self.ts,
            "runtime": self.runtime,
        }


def make_runtime_event(
    *,
    run_id: str,
    tenant_id: str,
    event: str,
    level: str = "info",
    message: str = "",
    payload: dict[str, Any] | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        run_id=run_id,
        tenant_id=tenant_id,
        event=event,
        level=level,
        message=message,
        payload=redact_payload(payload or {}),
    )
