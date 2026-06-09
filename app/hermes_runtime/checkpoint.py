from __future__ import annotations

import hashlib
import time
from typing import Any

from app.hermes_runtime.side_effects import classify_side_effect


_TARGET_KEYS = (
    "path",
    "target",
    "filename",
    "doc_token",
    "event_id",
    "task_id",
    "customer_id",
    "chat_id",
    "message_id",
)


def build_checkpoint_record(
    *,
    run_id: str,
    tenant_id: str,
    tool_name: str,
    target: str,
) -> dict:
    raw = f"{tenant_id}:{run_id}:{tool_name}:{target}:{time.time_ns()}"
    digest = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]
    return {
        "checkpoint_id": f"cp_{digest}",
        "run_id": run_id,
        "tenant_id": tenant_id,
        "tool_name": tool_name,
        "target": target,
        "created_at": int(time.time()),
    }


def side_effect_target(args: dict[str, Any] | None) -> str:
    payload = args or {}
    for key in _TARGET_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def build_side_effect_ledger_entry(
    *,
    run_id: str,
    tenant_id: str,
    tool_name: str,
    args: dict[str, Any] | None = None,
    status: str = "started",
    duration_ms: int = 0,
) -> dict[str, Any]:
    effect = classify_side_effect(tool_name, args)
    target = side_effect_target(args)
    checkpoint_id = ""
    if effect.requires_checkpoint:
        checkpoint_id = build_checkpoint_record(
            run_id=run_id,
            tenant_id=tenant_id,
            tool_name=tool_name,
            target=target,
        )["checkpoint_id"]
    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "tool_name": tool_name,
        "side_effect_class": effect.side_effect_class,
        "checkpoint_id": checkpoint_id,
        "target": target,
        "status": status,
        "rollback_available": effect.rollback_available,
        "user_visible": effect.user_visible,
        "duration_ms": duration_ms,
        "created_at": int(time.time()),
    }
