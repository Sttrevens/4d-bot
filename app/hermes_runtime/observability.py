from __future__ import annotations

import json
import time

from app.hermes_runtime.events import RuntimeEvent
from app.services import redis_client as redis

_HEALTH: dict[str, str] = {
    "last_success_at": "",
    "last_error_at": "",
    "last_error": "",
}


def run_summary_key(tenant_id: str, run_id: str) -> str:
    return f"{tenant_id}:runtime:hermes:run:{run_id}"


def event_stream_key(tenant_id: str, run_id: str) -> str:
    return f"{tenant_id}:runtime:hermes:events:{run_id}"


def shadow_result_key(tenant_id: str, run_id: str) -> str:
    return f"{tenant_id}:runtime:hermes:shadow:{run_id}"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_runtime_health(*, success: bool, error: str = "") -> None:
    if success:
        _HEALTH["last_success_at"] = _utc_now()
        _HEALTH["last_error"] = ""
        _HEALTH["last_error_at"] = ""
        return
    _HEALTH["last_error"] = error
    _HEALTH["last_error_at"] = _utc_now()


def health_state() -> dict[str, str]:
    return dict(_HEALTH)


def record_runtime_event(event: RuntimeEvent) -> None:
    if event.event == "runtime.failed":
        record_runtime_health(success=False, error=str(event.payload.get("error") or "runtime_failed"))
    elif event.event == "runtime.completed":
        record_runtime_health(success=True)
    try:
        redis.execute(
            "RPUSH",
            event_stream_key(event.tenant_id, event.run_id),
            json.dumps(event.to_dict(), ensure_ascii=False),
        )
    except Exception:
        pass
