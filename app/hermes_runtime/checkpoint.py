from __future__ import annotations

import hashlib
import time


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
