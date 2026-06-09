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

    summary = {
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
    }
    try:
        _redis_execute(
            "RPUSH",
            event_stream_key(event.tenant_id, event.run_id),
            json.dumps(event_data, ensure_ascii=False),
        )
        record_runtime_run_summary(summary)
    except Exception:
        pass


def record_runtime_run_summary(
    summary: dict[str, Any],
    *,
    ttl_seconds: int = DEFAULT_RUN_SUMMARY_TTL_SECONDS,
) -> None:
    """Persist a compact, dashboard-safe Hermes run summary."""
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
    tenant_id: str | None = None,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return newest Hermes run summaries globally or for one tenant."""
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


def load_runtime_run_detail(tenant_id: str, run_id: str) -> dict[str, Any]:
    """Load dashboard diagnostics for one tenant-scoped Hermes run."""
    if not tenant_id or not run_id:
        return {"summary": {}, "events": [], "shadow_response": None}

    try:
        raw_summary = _redis_execute("GET", run_summary_key(tenant_id, run_id))
    except Exception:
        raw_summary = None

    try:
        raw_events = _redis_execute("LRANGE", event_stream_key(tenant_id, run_id), 0, -1)
    except Exception:
        raw_events = []

    try:
        raw_shadow = _redis_execute("GET", shadow_result_key(tenant_id, run_id))
    except Exception:
        raw_shadow = None

    events: list[dict[str, Any]] = []
    for raw_event in raw_events or []:
        event = _load_json_object(raw_event)
        if event:
            events.append(event)

    return {
        "summary": _load_json_object(raw_summary),
        "events": events,
        "shadow_response": _load_json_object(raw_shadow) if raw_shadow else None,
    }


def build_shadow_qa_report(tenant_ids: list[str], *, limit: int = 100) -> dict[str, Any]:
    """Aggregate dashboard-safe QA signals for Hermes shadow runs."""
    safe_limit = max(1, min(int(limit or 100), 500))
    report: dict[str, Any] = {
        "total_shadow_runs": 0,
        "status_counts": {"pass": 0, "review": 0, "fail": 0},
        "tenants": {},
    }

    for tenant_id in [str(tid) for tid in tenant_ids if str(tid).strip()]:
        scored_runs: list[dict[str, Any]] = []
        for summary in list_runtime_run_summaries(tenant_id, limit=safe_limit):
            if not bool(summary.get("shadow_mode", summary.get("shadow", False))):
                continue
            run_id = str(summary.get("run_id", ""))
            if not run_id:
                continue
            detail = load_runtime_run_detail(tenant_id, run_id)
            scored = _score_shadow_run(summary, detail.get("shadow_response"))
            scored_runs.append(scored)
            report["total_shadow_runs"] += 1
            report["status_counts"][scored["qa_status"]] += 1

        tenant_counts = {"pass": 0, "review": 0, "fail": 0}
        for run in scored_runs:
            tenant_counts[run["qa_status"]] += 1

        report["tenants"][tenant_id] = {
            "shadow_run_count": len(scored_runs),
            "status_counts": tenant_counts,
            "severe_regression_count": tenant_counts["fail"],
            "review_count": tenant_counts["review"],
            "recent_runs": scored_runs[:10],
        }

    return report


def _score_shadow_run(summary: dict[str, Any], shadow_response: dict[str, Any] | None) -> dict[str, Any]:
    runtime_status = str(summary.get("status", "") or "unknown")
    shadow_status = str((shadow_response or {}).get("status", runtime_status) or "unknown")
    final_text = str((shadow_response or {}).get("final_text", "") or "")
    issues: list[str] = []

    if shadow_response is None:
        issues.append("missing_shadow_response")
    if runtime_status in {"failed", "timed_out", "blocked"} or shadow_status in {"failed", "timed_out", "blocked"}:
        issues.append("shadow_runtime_failed")
    if runtime_status == "completed" and not final_text.strip():
        issues.append("empty_shadow_final_text")

    if "shadow_runtime_failed" in issues:
        qa_status = "fail"
    elif issues:
        qa_status = "review"
    else:
        qa_status = "pass"

    return {
        "run_id": str(summary.get("run_id", "")),
        "tenant_id": str(summary.get("tenant_id", "")),
        "qa_status": qa_status,
        "issues": issues,
        "runtime_status": runtime_status,
        "duration_ms": int(summary.get("duration_ms", 0) or 0),
        "tool_calls": int(summary.get("tool_calls", 0) or 0),
        "error_code": str(summary.get("error_code") or summary.get("error") or ""),
        "created_at": float(summary.get("created_at", summary.get("updated_at", 0)) or 0),
    }


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
