"""任务规划系统 —— 让 bot 能拆解大任务、跨会话执行

核心概念：
- Plan: 一个大任务的分解方案，包含多个 Step
- Step: 一个具体的子步骤，有状态（pending/in_progress/completed/blocked）
- 每个 Plan 持久化存储在 GitHub（.bot-memory/plans/{plan_id}.json）

工作流：
1. 用户提出大任务 → bot 检测到复杂度 → 创建 Plan
2. 发给用户确认 → 用户同意 → 开始执行
3. 每步完成后更新 Plan → 通知用户进度
4. 下次用户聊天时 → 自动加载活跃 Plan → bot 知道做到哪了
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app.services import memory_store

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_plan_id() -> str:
    return f"plan_{int(time.time())}_{hex(hash(time.time()))[2:6]}"


# ── Plan CRUD ──


def create_plan(
    title: str,
    steps: list[dict],
    created_by: str = "",
    summary: str = "",
    estimated_days: int = 0,
) -> dict:
    """创建一个新的执行计划。

    steps 格式: [{"title": "分析现有代码", "description": "..."}, ...]
    """
    plan_id = _gen_plan_id()

    # 标准化 steps
    normalized_steps = []
    for i, s in enumerate(steps):
        normalized_steps.append({
            "id": f"step_{i + 1}",
            "title": s.get("title", f"步骤 {i + 1}"),
            "description": s.get("description", ""),
            "status": "pending",
            "depends_on": s.get("depends_on", []),
            "outcome": "",
            "started_at": "",
            "completed_at": "",
        })

    # 校验依赖：确保 depends_on 引用的 step_id 都存在
    valid_ids = {s["id"] for s in normalized_steps}
    for s in normalized_steps:
        bad_deps = [d for d in s["depends_on"] if d not in valid_ids]
        if bad_deps:
            logger.warning("plan '%s' step %s has invalid depends_on: %s (removed)",
                           title, s["id"], bad_deps)
            s["depends_on"] = [d for d in s["depends_on"] if d in valid_ids]

    plan = {
        "plan_id": plan_id,
        "title": title,
        "summary": summary,
        "created_by": created_by,
        "created_at": _now_iso(),
        "status": "draft",  # draft → active → completed → cancelled
        "estimated_days": estimated_days,
        "steps": normalized_steps,
        "next_action": normalized_steps[0]["title"] if normalized_steps else "",
    }

    key = f"plans/{plan_id}"
    if memory_store.write_json(key, plan, message=f"plan: create '{title}'"):
        logger.info("created plan %s: %s (%d steps)", plan_id, title, len(normalized_steps))
        return plan
    return plan  # 返回 plan 即使写入失败，内存中可用


def get_plan(plan_id: str) -> dict | None:
    """获取计划详情。"""
    key = f"plans/{plan_id}"
    return memory_store.read_json(key)


def activate_plan(plan_id: str) -> dict | None:
    """激活计划（draft → active），开始执行。"""
    plan = get_plan(plan_id)
    if not plan:
        return None
    plan["status"] = "active"
    # 第一步设为 in_progress
    for step in plan.get("steps", []):
        if step["status"] == "pending":
            step["status"] = "in_progress"
            step["started_at"] = _now_iso()
            plan["next_action"] = step["title"]
            break
    _save_plan(plan)
    return plan


def update_step(
    plan_id: str,
    step_id: str,
    status: str = "",
    outcome: str = "",
) -> dict | None:
    """更新计划中某个步骤的状态。

    status: pending / in_progress / completed / blocked
    """
    plan = get_plan(plan_id)
    if not plan:
        return None

    for step in plan.get("steps", []):
        if step["id"] == step_id:
            if status:
                step["status"] = status
            if outcome:
                step["outcome"] = outcome
            if status == "completed":
                step["completed_at"] = _now_iso()
            elif status == "in_progress":
                step["started_at"] = _now_iso()
            break

    # 如果当前步骤完成，自动推进下一步
    if status == "completed":
        _auto_advance(plan)

    # 检查是否所有步骤都完成
    all_steps = plan.get("steps", [])
    if all_steps and all(s["status"] == "completed" for s in all_steps):
        plan["status"] = "completed"
        plan["next_action"] = ""
    elif any(s["status"] == "blocked" for s in all_steps):
        plan["next_action"] = "有步骤被阻塞，需要人工介入"

    _save_plan(plan)
    return plan


def cancel_plan(plan_id: str) -> dict | None:
    """取消计划。"""
    plan = get_plan(plan_id)
    if not plan:
        return None
    plan["status"] = "cancelled"
    plan["next_action"] = ""
    _save_plan(plan)
    return plan


def list_active_plans(user_id: str = "") -> list[dict]:
    """列出活跃的计划。

    由于 GitHub API 不支持目录列表的内容搜索，
    我们维护一个 plans/_index.json 索引文件。
    """
    index = memory_store.read_json("plans/_index")
    if not index:
        return []

    plans = []
    for entry in index:
        if entry.get("status") not in ("active", "draft"):
            continue
        if user_id and entry.get("created_by_id", "")[:12] != user_id[:12]:
            continue
        plans.append(entry)
    return plans


def get_active_plans_context(user_id: str = "") -> str:
    """获取活跃计划的摘要，用于注入 system prompt。"""
    plans = list_active_plans(user_id)
    if not plans:
        return ""

    lines = ["你有以下进行中的计划："]
    for p in plans[:3]:  # 最多 3 个
        plan = get_plan(p["plan_id"])
        if not plan:
            continue
        steps = plan.get("steps", [])
        done = sum(1 for s in steps if s["status"] == "completed")
        total = len(steps)
        lines.append(
            f"\n计划: {plan['title']} ({done}/{total} 步完成)"
            f"\n  状态: {plan['status']}"
            f"\n  下一步: {plan.get('next_action', '?')}"
            f"\n  plan_id: {plan['plan_id']}"
        )
    return "\n".join(lines)


# ── 格式化 ──


def format_plan(plan: dict) -> str:
    """将 plan 格式化为人类可读的文本。"""
    lines = [
        f"计划: {plan['title']}",
        f"状态: {plan['status']}",
        f"创建者: {plan.get('created_by', '?')}",
    ]
    if plan.get("summary"):
        lines.append(f"概要: {plan['summary']}")
    if plan.get("estimated_days"):
        lines.append(f"预计天数: {plan['estimated_days']}")

    lines.append(f"\n步骤:")
    for step in plan.get("steps", []):
        icon = {"pending": "○", "in_progress": "◉", "completed": "●", "blocked": "✕"}.get(
            step["status"], "?"
        )
        line = f"  {icon} {step['id']}: {step['title']}"
        if step.get("outcome"):
            line += f"\n      → {step['outcome']}"
        lines.append(line)

    if plan.get("next_action"):
        lines.append(f"\n下一步: {plan['next_action']}")

    lines.append(f"\nplan_id: {plan['plan_id']}")
    return "\n".join(lines)


# ── 内部辅助 ──


def _auto_advance(plan: dict) -> None:
    """自动将下一个可执行的步骤设为 in_progress。"""
    completed_ids = {s["id"] for s in plan["steps"] if s["status"] == "completed"}

    for step in plan["steps"]:
        if step["status"] != "pending":
            continue
        # 检查依赖是否都完成了
        deps = step.get("depends_on", [])
        if all(d in completed_ids for d in deps):
            step["status"] = "in_progress"
            step["started_at"] = _now_iso()
            plan["next_action"] = step["title"]
            return
    # 没有可推进的步骤
    plan["next_action"] = ""


def _save_plan(plan: dict) -> bool:
    """保存计划并更新索引。"""
    plan_id = plan["plan_id"]
    key = f"plans/{plan_id}"
    ok = memory_store.write_json(key, plan, message=f"plan: update '{plan['title']}'")

    # 更新索引
    _update_index(plan)
    return ok


def _update_index(plan: dict) -> None:
    """更新 plans/_index.json 索引。"""
    index = memory_store.read_json("plans/_index") or []

    # 查找并更新或添加
    found = False
    for entry in index:
        if entry.get("plan_id") == plan["plan_id"]:
            entry["status"] = plan["status"]
            entry["title"] = plan["title"]
            entry["next_action"] = plan.get("next_action", "")
            found = True
            break

    if not found:
        index.append({
            "plan_id": plan["plan_id"],
            "title": plan["title"],
            "status": plan["status"],
            "created_by": plan.get("created_by", ""),
            "created_by_id": plan.get("created_by_id", ""),
            "created_at": plan.get("created_at", ""),
            "next_action": plan.get("next_action", ""),
        })

    # 只保留最近 50 条索引
    if len(index) > 50:
        # 保留 active/draft + 最近的 completed/cancelled
        active = [e for e in index if e.get("status") in ("active", "draft")]
        rest = [e for e in index if e.get("status") not in ("active", "draft")]
        index = active + rest[-30:]

    memory_store.write_json("plans/_index", index, message="plan: update index")
