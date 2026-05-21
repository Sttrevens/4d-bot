from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    backend: str = "none"
    cwd: str = "/workspace"
    network: str = "restricted"
    timeout_seconds: int = 120
    max_output_chars: int = 20000
    allow_file_write: bool = False
    allow_package_install: bool = False
    allowed_paths: list[str] = field(default_factory=list)
    requires_checkpoint: bool = False


def build_execution_policy(tenant, *, admin: bool = False) -> ExecutionPolicy:
    enabled = bool(getattr(tenant, "hermes_code_execution_enabled", False))
    backend = getattr(tenant, "hermes_code_execution_backend", "none") or "none"
    if not enabled and not bool(getattr(tenant, "container_sandbox_enabled", False)):
        return ExecutionPolicy()
    if backend == "docker" and admin and bool(getattr(tenant, "container_sandbox_enabled", False)):
        return ExecutionPolicy(
            backend="docker",
            allow_file_write=True,
            allowed_paths=list(getattr(tenant, "autofix_allowed_paths", []) or ["app/tools", "app/knowledge"]),
            requires_checkpoint=True,
        )
    return ExecutionPolicy(backend="none")


def can_execute_skill_script(path: str, tenant) -> bool:
    _ = path, tenant
    return False
