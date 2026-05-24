from __future__ import annotations

import hashlib

from app.hermes_runtime.types import RuntimeName


def _rollout_bucket(tenant_id: str, sender_id: str) -> int:
    raw = f"{tenant_id}:{sender_id}".encode("utf-8", "ignore")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16) % 100


def select_runtime(tenant, sender_id: str, run_id: str = "") -> RuntimeName:
    """Select legacy or Hermes runtime for a tenant.

    The selector is deliberately fail-safe: every missing or falsey switch
    returns legacy. A deploy to main therefore cannot route traffic to Hermes
    unless the tenant config explicitly opts in.
    """
    _ = run_id
    if getattr(tenant, "agent_runtime", "legacy") != "hermes_sidecar":
        return "legacy"
    if not bool(getattr(tenant, "hermes_runtime_enabled", False)):
        return "legacy"

    rollout = int(getattr(tenant, "hermes_runtime_rollout_percent", 0) or 0)
    if rollout <= 0:
        return "legacy"

    if bool(getattr(tenant, "hermes_runtime_shadow", False)):
        return "legacy_shadow_hermes"

    if rollout >= 100:
        return "hermes_sidecar"

    bucket = _rollout_bucket(getattr(tenant, "tenant_id", ""), sender_id)
    return "hermes_sidecar" if bucket < rollout else "legacy"
