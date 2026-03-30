"""Shared task-board helpers for long-running agent plans."""

from __future__ import annotations

from typing import Any

PLAN_ACTIVE_STATUSES = ("active", "draft")
STEP_STATUSES = ("pending", "in_progress", "completed", "blocked")
_STEP_ICONS = {
    "pending": "○",
    "in_progress": "◉",
    "completed": "●",
    "blocked": "✕",
}


def normalize_steps(
    steps: list[dict[str, Any]],
    *,
    force_pending: bool = False,
) -> list[dict[str, Any]]:
    """Normalize raw plan steps into a consistent task-board schema."""
    normalized_steps: list[dict[str, Any]] = []
    for i, step in enumerate(steps):
        status = step.get("status", "pending")
        if force_pending or status not in STEP_STATUSES:
            status = "pending"
        normalized_steps.append({
            "id": f"step_{i + 1}",
            "title": step.get("title", f"步骤 {i + 1}"),
            "description": step.get("description", ""),
            "status": status,
            "depends_on": list(step.get("depends_on", [])),
            "outcome": step.get("outcome", ""),
            "started_at": step.get("started_at", ""),
            "completed_at": step.get("completed_at", ""),
        })
    return normalized_steps


def prune_invalid_dependencies(steps: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    """Remove invalid dependencies in-place and report what was removed."""
    valid_ids = {step["id"] for step in steps}
    removed: list[tuple[str, list[str]]] = []
    for step in steps:
        bad_deps = [dep for dep in step.get("depends_on", []) if dep not in valid_ids]
        if bad_deps:
            step["depends_on"] = [dep for dep in step.get("depends_on", []) if dep in valid_ids]
            removed.append((step["id"], bad_deps))
    return removed


def advance_next_step(plan: dict[str, Any], now_iso: str) -> None:
    """Advance the next dependency-ready step to in_progress."""
    completed_ids = {step["id"] for step in plan.get("steps", []) if step.get("status") == "completed"}
    for step in plan.get("steps", []):
        if step.get("status") != "pending":
            continue
        deps = step.get("depends_on", [])
        if all(dep in completed_ids for dep in deps):
            step["status"] = "in_progress"
            step["started_at"] = now_iso
            plan["next_action"] = step.get("title", "")
            return
    plan["next_action"] = ""


def summarize_progress(plan: dict[str, Any]) -> tuple[int, int]:
    steps = plan.get("steps", [])
    done = sum(1 for step in steps if step.get("status") == "completed")
    return done, len(steps)


def format_plan_text(plan: dict[str, Any]) -> str:
    lines = [
        f"计划: {plan['title']}",
        f"状态: {plan['status']}",
        f"创建者: {plan.get('created_by', '?')}",
    ]
    if plan.get("summary"):
        lines.append(f"概要: {plan['summary']}")
    if plan.get("estimated_days"):
        lines.append(f"预计天数: {plan['estimated_days']}")

    lines.append("\n步骤:")
    for step in plan.get("steps", []):
        icon = _STEP_ICONS.get(step.get("status"), "?")
        line = f"  {icon} {step['id']}: {step['title']}"
        if step.get("outcome"):
            line += f"\n      → {step['outcome']}"
        lines.append(line)

    if plan.get("next_action"):
        lines.append(f"\n下一步: {plan['next_action']}")
    lines.append(f"\nplan_id: {plan['plan_id']}")
    return "\n".join(lines)


def build_active_plan_context(plans: list[dict[str, Any]], get_plan: Any) -> str:
    if not plans:
        return ""
    lines = ["你有以下进行中的计划："]
    for entry in plans[:3]:
        plan = get_plan(entry["plan_id"])
        if not plan:
            continue
        done, total = summarize_progress(plan)
        lines.append(
            f"\n计划: {plan['title']} ({done}/{total} 步完成)"
            f"\n  状态: {plan['status']}"
            f"\n  下一步: {plan.get('next_action', '?')}"
            f"\n  plan_id: {plan['plan_id']}"
        )
    return "\n".join(lines)
