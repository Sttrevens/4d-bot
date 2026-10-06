from __future__ import annotations

import hashlib

from app.hermes_runtime.types import RuntimeName

_LOCAL_AGENT_PROVIDERS = {"codex_cli", "claude_cli", "custom_command"}


def _str_attr(obj, name: str, default: str = "") -> str:
    value = getattr(obj, name, default)
    return value if isinstance(value, str) else default


def _bool_attr(obj, name: str, default: bool = False) -> bool:
    value = getattr(obj, name, default)
    return value if isinstance(value, bool) else default


def _int_attr(obj, name: str, default: int = 0) -> int:
    value = getattr(obj, name, default)
    return value if isinstance(value, int) else default


def _rollout_bucket(tenant_id: str, sender_id: str) -> int:
    raw = f"{tenant_id}:{sender_id}".encode("utf-8", "ignore")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16) % 100


def select_runtime(tenant, sender_id: str, run_id: str = "") -> RuntimeName:
    """Select legacy or an explicitly enabled agent runtime for a tenant.

    The selector is deliberately fail-safe: every missing or falsey switch
    returns legacy. A deploy to main therefore cannot route traffic to Hermes,
    Codex, Claude, or another local agent unless tenant config opts in.
    """
    _ = run_id
    legacy_runtime = _str_attr(tenant, "agent_runtime", "legacy") or "legacy"
    provider = _str_attr(tenant, "agent_runtime_provider", "") or legacy_runtime or "legacy"
    if provider == "legacy":
        return "legacy"

    if provider == "hermes_sidecar":
        enabled = _bool_attr(tenant, "agent_runtime_enabled", False) or _bool_attr(
            tenant, "hermes_runtime_enabled", False
        )
        shadow = _bool_attr(tenant, "agent_runtime_shadow", False) or _bool_attr(
            tenant, "hermes_runtime_shadow", False
        )
        rollout = _int_attr(tenant, "agent_runtime_rollout_percent", 0) or _int_attr(
            tenant, "hermes_runtime_rollout_percent", 0
        )
    elif provider in _LOCAL_AGENT_PROVIDERS:
        enabled = _bool_attr(tenant, "agent_runtime_enabled", False)
        shadow = _bool_attr(tenant, "agent_runtime_shadow", False)
        rollout = _int_attr(tenant, "agent_runtime_rollout_percent", 0)
    else:
        return "legacy"

    if not enabled:
        return "legacy"

    if shadow:
        if provider == "hermes_sidecar":
            return "legacy_shadow_hermes"
        return f"legacy_shadow_{provider}"  # type: ignore[return-value]

    if rollout <= 0:
        return "legacy"

    if rollout >= 100:
        return provider  # type: ignore[return-value]

    bucket = _rollout_bucket(getattr(tenant, "tenant_id", ""), sender_id)
    return provider if bucket < rollout else "legacy"  # type: ignore[return-value]
