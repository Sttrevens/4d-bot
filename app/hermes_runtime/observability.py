from __future__ import annotations

import json
import time
from typing import Any

from app.hermes_runtime.events import RuntimeEvent

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


def _redis_execute(*args):
    from app.services import redis_client as redis

    return redis.execute(*args)


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
    event_data = event.to_dict()
    if event.event == "runtime.failed":
        record_runtime_health(success=False, error=event.error or str(event.payload.get("error") or "runtime_failed"))
    elif event.event == "runtime.completed":
        record_runtime_health(success=True)

    summary = {
        "run_id": event.run_id,
        "tenant_id": event.tenant_id,
        "channel_id": event.channel_id,
        "session_id": event.session_id,
        "platform": event.platform,
        "runtime": event.runtime,
        "last_event": event.event,
        "level": event.level,
        "tool": event.tool,
        "provider": event.provider,
        "error": event.error,
        "cost": event.cost,
        "ts": event.ts,
    }
    try:
        _redis_execute(
            "RPUSH",
            event_stream_key(event.tenant_id, event.run_id),
            json.dumps(event_data, ensure_ascii=False),
        )
        _redis_execute(
            "SET",
            run_summary_key(event.tenant_id, event.run_id),
            json.dumps(summary, ensure_ascii=False),
            "EX",
            "604800",
        )
    except Exception:
        pass


def load_runtime_events(tenant_id: str, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    rows = _redis_execute("LRANGE", event_stream_key(tenant_id, run_id), 0, limit - 1)
    events: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return events
    for row in rows:
        if not isinstance(row, str):
            continue
        try:
            parsed = json.loads(row)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events
