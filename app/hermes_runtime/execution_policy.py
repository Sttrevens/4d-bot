from __future__ import annotations

import posixpath
import shlex
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


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    allowed: bool
    status: str
    code: str = ""
    message: str = ""
    approval_request: dict = field(default_factory=dict)


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


def evaluate_execution_request(
    policy: ExecutionPolicy,
    *,
    command: str = "",
    cwd: str = "/workspace",
    write_paths: list[str] | None = None,
    skill_script_path: str = "",
) -> ExecutionDecision:
    if skill_script_path and not can_execute_skill_script(skill_script_path, tenant=None):
        return ExecutionDecision(
            allowed=False,
            status="blocked",
            code="skill_script_execution_denied",
            message="Repo skill scripts are readable but not executable by default.",
        )
    if policy.backend == "none":
        return ExecutionDecision(
            allowed=False,
            status="blocked",
            code="execution_disabled",
            message="Hermes code execution is disabled for this runtime request.",
        )
    for path in write_paths or []:
        if not policy.allow_file_write:
            return ExecutionDecision(
                allowed=False,
                status="blocked",
                code="file_write_denied",
                message="This execution policy does not allow file writes.",
            )
        if not _path_allowed(path, policy.allowed_paths):
            return ExecutionDecision(
                allowed=False,
                status="blocked",
                code="path_denied",
                message=f"Write path is outside Hermes execution allowlist: {path}",
            )
    if command and _is_destructive_command(command):
        return ExecutionDecision(
            allowed=False,
            status="needs_confirmation",
            code="confirmation_required",
            message="Destructive terminal commands require explicit approval.",
            approval_request={
                "command": command,
                "cwd": cwd,
                "expected_side_effect": "destructive_terminal",
                "rollback_available": bool(policy.requires_checkpoint),
            },
        )
    return ExecutionDecision(allowed=True, status="allowed", code="ok")


def _path_allowed(path: str, allowed_paths: list[str]) -> bool:
    clean = _normalize_repo_path(path)
    for allowed in allowed_paths:
        base = _normalize_repo_path(allowed)
        if clean == base or clean.startswith(f"{base}/"):
            return True
    return False


def _normalize_repo_path(path: str) -> str:
    value = path.replace("\\", "/").strip()
    if value.startswith("/workspace/"):
        value = value[len("/workspace/") :]
    elif value == "/workspace":
        value = ""
    normalized = posixpath.normpath(value).lstrip("/")
    if normalized == ".":
        return ""
    return normalized


def _is_destructive_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return True
    if not tokens:
        return False
    head = tokens[0]
    if head == "rm":
        return True
    if head in {"chmod", "chown"} and "-R" in tokens[1:]:
        return True
    if head == "docker" and len(tokens) > 1 and tokens[1] in {"stop", "rm", "rmi"}:
        return True
    if head == "git" and tokens[1:3] == ["reset", "--hard"]:
        return True
    if head == "git" and len(tokens) > 2 and tokens[1] == "checkout" and "--" in tokens[2:]:
        return True
    if head in {"pip", "pip3"} and len(tokens) > 1 and tokens[1] == "install":
        return True
    return False
