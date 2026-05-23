from __future__ import annotations

import json

from app.hermes_runtime.events import RuntimeEvent
from app.services import redis_client as redis


def run_summary_key(tenant_id: str, run_id: str) -> str:
    return f"{tenant_id}:runtime:hermes:run:{run_id}"


def event_stream_key(tenant_id: str, run_id: str) -> str:
    return f"{tenant_id}:runtime:hermes:events:{run_id}"


def shadow_result_key(tenant_id: str, run_id: str) -> str:
    return f"{tenant_id}:runtime:hermes:shadow:{run_id}"


def record_runtime_event(event: RuntimeEvent) -> None:
    try:
        redis.execute(
            "RPUSH",
            event_stream_key(event.tenant_id, event.run_id),
            json.dumps(event.to_dict(), ensure_ascii=False),
        )
    except Exception:
        pass
