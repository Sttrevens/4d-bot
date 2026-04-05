"""Shared coding-workflow policy inspired by Superpowers.

This layer is intentionally lighter than the full Superpowers workflow:
- broad coding / feature requests should clarify before editing
- multi-step / architectural work should create a plan before implementation
- coding completion claims require fresh verification
"""

from __future__ import annotations

import re

from app.harness.turn_mode import has_explicit_code_intent, sanitize_suggested_groups

_CODE_TOOL_HINTS = {
    "read_file", "write_file", "edit_file", "self_edit_file", "self_write_file",
    "bash_execute", "repo_search", "git_status", "git_diff", "github_create_pr",
}

_COMPLEX_CODE_RE = re.compile(
    r"(功能|feature|实现|开发|build|搭建|重构|refactor|架构|architecture|"
    r"迁移|migrate|系统|workflow|harness|agent|多步骤|多文件|端到端|整体|一口气)",
    re.IGNORECASE,
)
_FIXLIKE_RE = re.compile(
    r"(修|fix|bug|报错|错误|异常|crash|fail|失败|timeout|挂了|不工作|问题)",
    re.IGNORECASE,
)
_CONCRETE_SCOPE_RE = re.compile(
    r"(`[^`]+`|/[A-Za-z0-9._/-]+|[A-Za-z0-9._-]+\.[A-Za-z0-9]+|第\d+行|line \d+|函数|function|类|class)",
    re.IGNORECASE,
)
_MULTI_ACTION_RE = re.compile(r"(然后|并且|同时|顺便|再把|另外|以及)")


def is_coding_workflow_turn(
    user_text: str,
    suggested_groups: list[str] | set[str] | tuple[str, ...] | None,
) -> bool:
    groups = sanitize_suggested_groups(user_text, suggested_groups)
    runtime_hints = {str(item) for item in (suggested_groups or ())}
    has_code_runtime = bool(runtime_hints & _CODE_TOOL_HINTS)
    return ("code_dev" in groups or has_code_runtime) and has_explicit_code_intent(user_text)


def should_clarify_before_coding(
    user_text: str,
    suggested_groups: list[str] | set[str] | tuple[str, ...] | None,
) -> bool:
    text = (user_text or "").strip()
    if not is_coding_workflow_turn(text, suggested_groups):
        return False
    if _CONCRETE_SCOPE_RE.search(text):
        return False
    if _FIXLIKE_RE.search(text) and len(text) < 80:
        return False
    return bool(_COMPLEX_CODE_RE.search(text))


def should_plan_before_coding(
    user_text: str,
    suggested_groups: list[str] | set[str] | tuple[str, ...] | None,
) -> bool:
    text = (user_text or "").strip()
    if not is_coding_workflow_turn(text, suggested_groups):
        return False
    if _MULTI_ACTION_RE.search(text):
        return True
    return bool(_COMPLEX_CODE_RE.search(text) and len(text) >= 16)


def build_coding_workflow_instructions(
    user_text: str,
    suggested_groups: list[str] | set[str] | tuple[str, ...] | None,
) -> str:
    text = (user_text or "").strip()
    if not is_coding_workflow_turn(text, suggested_groups):
        return ""

    lines = [
        "\n\n[编码工作流]",
        "- 编码类任务优先先想清楚目标、边界和成功标准，再动手修改代码。",
        "- 只有当用户给的修改范围已经很明确时，才可以直接改代码；如果目标仍然模糊，先用 1-3 个简短问题把需求问清楚。",
        "- 如果任务涉及多个步骤、多个文件、架构调整、重构、迁移、部署或“一口气做掉”，优先用 create_plan 先拆计划，再执行。",
        "- 在声称“修好了/完成了/可以提交了”之前，必须亲自运行相关验证命令，基于最新结果汇报状态，不要凭感觉宣布完成。",
    ]

    if should_clarify_before_coding(text, suggested_groups):
        lines.append("- 当前这类请求更像需求/方案题：先澄清再写，不要第一轮就直接改代码。")

    if should_plan_before_coding(text, suggested_groups):
        lines.append("- 当前这类请求默认先 create_plan，再开始具体实现。")

    return "\n".join(lines)
