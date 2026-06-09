from __future__ import annotations

import json
import time
from typing import Any

from app.hermes_runtime.events import RuntimeEvent


RUNTIME_RUN_INDEX_KEY = "runtime:hermes:runs"
DEFAULT_RUN_SUMMARY_TTL_SECONDS = 7 * 24 * 60 * 60

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


def tenant_run_index_key(tenant_id: str) -> str:
    return f"{tenant_id}:runtime:hermes:runs"


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
    status = _status_from_event(event.event)
    if event.event == "runtime.failed":
        record_runtime_health(success=False, error=event.error or str(event.payload.get("error") or "runtime_failed"))
    elif event.event == "runtime.completed":
        record_runtime_health(success=True)

    try:
        _redis_execute(
            "RPUSH",
            event_stream_key(event.tenant_id, event.run_id),
            json.dumps(event_data, ensure_ascii=False),
        )
        record_runtime_run_summary({
            "run_id": event.run_id,
            "tenant_id": event.tenant_id,
            "channel_id": event.channel_id,
            "session_id": event.session_id,
            "platform": event.platform,
            "runtime": event.runtime,
            "status": status,
            "last_event": event.event,
            "level": event.level,
            "tool": event.tool,
            "provider": event.provider,
            "error": event.error,
            "cost": event.cost,
            "updated_at": event.ts,
        })
    except Exception:
        pass


def record_runtime_run_summary(
    summary: dict[str, Any],
    *,
    ttl_seconds: int = DEFAULT_RUN_SUMMARY_TTL_SECONDS,
) -> None:
    tenant_id = str(summary.get("tenant_id") or "").strip()
    run_id = str(summary.get("run_id") or "").strip()
    if not tenant_id or not run_id:
        return

    indexed = dict(summary)
    indexed["tenant_id"] = tenant_id
    indexed["run_id"] = run_id
    indexed.setdefault("updated_at", time.time())
    score = _float_or_now(indexed.get("updated_at"))

    try:
        _redis_execute(
            "SET",
            run_summary_key(tenant_id, run_id),
            json.dumps(indexed, ensure_ascii=False),
            "EX",
            str(ttl_seconds),
        )
        _redis_execute("ZADD", RUNTIME_RUN_INDEX_KEY, score, f"{tenant_id}:{run_id}")
        _redis_execute("ZADD", tenant_run_index_key(tenant_id), score, run_id)
        _redis_execute("EXPIRE", RUNTIME_RUN_INDEX_KEY, str(ttl_seconds))
        _redis_execute("EXPIRE", tenant_run_index_key(tenant_id), str(ttl_seconds))
    except Exception:
        pass


def list_runtime_run_summaries(
    *,
    limit: int = 100,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    tenant = str(tenant_id or "").strip()
    index_key = tenant_run_index_key(tenant) if tenant else RUNTIME_RUN_INDEX_KEY

    try:
        members = _redis_execute("ZREVRANGE", index_key, 0, limit - 1)
    except Exception:
        return []

    summaries: list[dict[str, Any]] = []
    for member in members or []:
        member_text = _decode_redis_value(member)
        if tenant:
            summary_key = run_summary_key(tenant, member_text)
        else:
            member_tenant, _, member_run = member_text.partition(":")
            if not member_tenant or not member_run:
                continue
            summary_key = run_summary_key(member_tenant, member_run)
        try:
            raw = _redis_execute("GET", summary_key)
        except Exception:
            continue
        data = _load_json_object(raw)
        if data:
            summaries.append(data)
    return summaries


def load_runtime_events(tenant_id: str, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    try:
        rows = _redis_execute("LRANGE", event_stream_key(tenant_id, run_id), 0, limit - 1)
    except Exception:
        return []

    events: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return events
    for row in rows:
        parsed = _load_json_object(row)
        if parsed:
            events.append(parsed)
    return events


def load_shadow_response(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    try:
        row = _redis_execute("GET", shadow_result_key(tenant_id, run_id))
    except Exception:
        return None
    parsed = _load_json_object(row)
    return parsed or None


def _status_from_event(event_name: str) -> str:
    mapping = {
        "runtime.completed": "completed",
        "runtime.partial": "partial",
        "runtime.failed": "failed",
    }
    return mapping.get(event_name, "running")


def _float_or_now(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _decode_redis_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _load_json_object(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(_decode_redis_value(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
