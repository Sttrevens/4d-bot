from __future__ import annotations

from app.hermes_runtime.execution_policy import ExecutionPolicy


def describe_execution_backend(policy: ExecutionPolicy) -> dict:
    return {
        "backend": policy.backend,
        "network": policy.network,
        "timeout_seconds": policy.timeout_seconds,
        "allow_file_write": policy.allow_file_write,
    }
