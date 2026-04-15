"""记忆与规划工具 —— 暴露给 LLM 的记忆系统接口

让模型能主动：
- 保存/检索记忆
- 创建/推进/查看执行计划
"""

from __future__ import annotations

import logging

from app.services import memory as mem
from app.services import planner
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)


# ── 工具实现 ──


def save_memory(args: dict) -> ToolResult:
    """主动保存重要信息到长期记忆。"""
    action = args.get("what", "")
    if not action:
        return ToolResult.invalid_param("缺少 'what' 参数")
    user_id = args.get("user_id", "system")
    user_name = args.get("user_name", "bot")
    tags = args.get("tags", [])
    outcome = args.get("outcome", "")
    solution = args.get("solution", False)

    ok = mem.remember(user_id, user_name, action, outcome, tags,
                      solution=solution)
    if ok:
        return ToolResult.success("已保存到记忆。")
    return ToolResult.error("保存失败（GitHub 写入错误）。", code="api_error")


def recall_memory(args: dict) -> ToolResult:
    """回忆过去的交互和知识。"""
    user_id = args.get("user_id", "")
    tags = args.get("tags", [])
    keyword = args.get("keyword", "")
    query_text = args.get("query_text", "")
    limit = args.get("limit", 10)

    return ToolResult.success(mem.recall_text(
        user_id=user_id,
        tags=tags,
        keyword=keyword,
        limit=limit,
        query_text=query_text,
    ))


def recall_org_memory(args: dict) -> ToolResult:
    """搜索组织内其他成员的解决方案（跨用户知识共享）。"""
    tags = args.get("tags", [])
    keyword = args.get("keyword", "")
    limit = args.get("limit", 5)
    exclude_user_id = args.get("exclude_user_id", "")

    return ToolResult.success(mem.recall_org_text(
        tags=tags, keyword=keyword, limit=limit,
        exclude_user_id=exclude_user_id,
    ))


def create_plan(args: dict) -> ToolResult:
    """创建多步骤执行计划。"""
    title = args.get("title", "")
    if not title:
        return ToolResult.invalid_param("缺少 'title' 参数")

    steps_raw = args.get("steps", [])
    if not steps_raw:
        return ToolResult.invalid_param("缺少 'steps' 参数（至少需要一个步骤）")

    # steps 可以是字符串列表或 dict 列表
    steps = []
    for s in steps_raw:
        if isinstance(s, str):
            steps.append({"title": s})
        elif isinstance(s, dict):
            steps.append(s)

    plan = planner.create_plan(
        title=title,
        steps=steps,
        created_by=args.get("created_by", ""),
        summary=args.get("summary", ""),
        estimated_days=args.get("estimated_days", 0),
    )
    return ToolResult.success(planner.format_plan(plan))


def update_plan_step(args: dict) -> ToolResult:
    """更新计划中某个步骤的状态。"""
    plan_id = args.get("plan_id", "")
    step_id = args.get("step_id", "")
    if not plan_id or not step_id:
        return ToolResult.invalid_param("需要 'plan_id' 和 'step_id' 参数")

    plan = planner.update_step(
        plan_id=plan_id,
        step_id=step_id,
        status=args.get("status", ""),
        outcome=args.get("outcome", ""),
    )
    if not plan:
        return ToolResult.not_found(f"找不到计划 {plan_id}")
    return ToolResult.success(planner.format_plan(plan))


def list_plans(args: dict) -> ToolResult:
    """查看当前活跃的计划。"""
    user_id = args.get("user_id", "")
    active = planner.list_active_plans(user_id)
    if not active:
        return ToolResult.success("当前没有活跃的计划。")

    lines = [f"找到 {len(active)} 个活跃计划：\n"]
    for entry in active:
        plan = planner.get_plan(entry["plan_id"])
        if plan:
            lines.append(planner.format_plan(plan))
            lines.append("")
    return ToolResult.success("\n".join(lines))


def get_plan_detail(args: dict) -> ToolResult:
    """获取计划详情。"""
    plan_id = args.get("plan_id", "")
    if not plan_id:
        return ToolResult.invalid_param("缺少 'plan_id' 参数")
    plan = planner.get_plan(plan_id)
    if not plan:
        return ToolResult.not_found(f"找不到计划 {plan_id}")
    return ToolResult.success(planner.format_plan(plan))


def activate_plan_tool(args: dict) -> ToolResult:
    """激活计划，开始执行。"""
    plan_id = args.get("plan_id", "")
    if not plan_id:
        return ToolResult.invalid_param("缺少 'plan_id' 参数")
    plan = planner.activate_plan(plan_id)
    if not plan:
        return ToolResult.not_found(f"找不到计划 {plan_id}")
    return ToolResult.success(f"计划已激活！\n\n{planner.format_plan(plan)}")


def cancel_plan_tool(args: dict) -> ToolResult:
    """取消计划。"""
    plan_id = args.get("plan_id", "")
    if not plan_id:
        return ToolResult.invalid_param("缺少 'plan_id' 参数")
    plan = planner.cancel_plan(plan_id)
    if not plan:
        return ToolResult.not_found(f"找不到计划 {plan_id}")
    return ToolResult.success(f"计划已取消。\n\n{planner.format_plan(plan)}")


def schedule_step(args: dict) -> ToolResult:
    """安排某个步骤的自动执行时间。"""
    from app.services.scheduler import add_schedule

    plan_id = args.get("plan_id", "")
    step_id = args.get("step_id", "")
    scheduled_at = args.get("scheduled_at", "")
    if not plan_id or not step_id or not scheduled_at:
        return ToolResult.invalid_param("需要 plan_id, step_id, scheduled_at 参数")

    # 自动注入当前租户 ID，让 scheduler 知道该用哪个租户上下文
    tenant_id = ""
    try:
        from app.tenant.context import get_current_tenant
        tenant_id = get_current_tenant().tenant_id
    except Exception:
        pass

    ok = add_schedule(
        plan_id=plan_id,
        step_id=step_id,
        scheduled_at=scheduled_at,
        notify_user_id=args.get("notify_user_id", ""),
        notify_user_name=args.get("notify_user_name", ""),
        tenant_id=tenant_id,
    )
    if ok:
        return ToolResult.success(f"已安排 {step_id} 在 {scheduled_at} 自动执行。届时会通知用户。")
    return ToolResult.error("调度写入失败。", code="api_error")


# ── 工具定义（Anthropic 格式）──


TOOL_DEFINITIONS = [
    {
        "name": "save_memory",
        "description": (
            "把重要的事记下来，下次还能想起来。"
            "比如做完了什么、用户的偏好习惯、或者用户让你记住的文档/资料。"
            "文档类的记得把标题、链接或ID也带上，方便以后翻。不用什么都记，有价值的才存。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    "description": "要记住什么（简洁描述，如「帮张三修了碰撞检测 bug」）",
                },
                "outcome": {
                    "type": "string",
                    "description": "结果如何（可选，如「PR #42 已合并」）",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签，便于后续检索（如 [\"代码\", \"bug修复\"]）",
                },
                "user_id": {"type": "string", "description": "相关用户 ID（可选）"},
                "user_name": {"type": "string", "description": "相关用户名（可选）"},
                "solution": {
                    "type": "boolean",
                    "description": "这条记忆是否是可复用的解决方案（修 bug、排错、配置等）。"
                                   "标记为 true 后，组织内其他用户遇到类似问题时可以检索到。",
                    "default": False,
                },
            },
            "required": ["what"],
        },
    },
    {
        "name": "recall_org_memory",
        "description": (
            "搜索组织内其他成员遇到过的类似问题和解决方案。"
            "当用户遇到技术问题、配置问题、或常见错误时，先搜搜同事有没有解决过。"
            "只返回标记为「解决方案」的记忆，不会泄露其他用户的私人对话。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "按标签过滤（如 [\"代码\", \"部署\"]，可选）",
                },
                "keyword": {
                    "type": "string",
                    "description": "按关键词搜索（如 bug 名、错误信息、技术名词）",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回几条，默认 5",
                    "default": 5,
                },
                "exclude_user_id": {
                    "type": "string",
                    "description": "排除某个用户的记忆（通常传当前用户 ID）",
                },
            },
        },
    },
    {
        "name": "recall_memory",
        "description": (
            "回忆过去的交互和知识。"
            "用户提到「上次」「之前」「你还记得」等时先用这个搜一下，别凭印象编。"
            "也可以主动了解之前和某个用户做过什么。标签和关键词都能搜。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "只查某个用户的记忆（可选，不填则查所有人）",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "按标签过滤（如 [\"代码\"]，可选）",
                },
                "keyword": {
                    "type": "string",
                    "description": "按关键词搜索记忆内容（在摘要/详情/结果中匹配，可选）",
                },
                "query_text": {
                    "type": "string",
                    "description": "当前用户这轮原始问题（可选）。用于语义召回避免拉出无关旧话题。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回几条，默认 10",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "create_plan",
        "description": (
            "创建多步骤执行计划。"
            "适用场景：用户提出需要多天/多步骤才能完成的大任务时。"
            "创建后会返回计划详情，需要用户确认后再 activate_plan 开始执行。"
            "例如「重构认证系统」→ 拆成分析、实现、测试 3 步。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "计划标题（简洁，如「重构认证系统」）",
                },
                "summary": {
                    "type": "string",
                    "description": "计划概述",
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "步骤标题"},
                            "description": {"type": "string", "description": "步骤详细描述"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "依赖的步骤 ID（如 [\"step_1\"]）",
                            },
                        },
                        "required": ["title"],
                    },
                    "description": "步骤列表",
                },
                "estimated_days": {
                    "type": "integer",
                    "description": "预计完成天数",
                },
                "created_by": {
                    "type": "string",
                    "description": "创建者名字",
                },
            },
            "required": ["title", "steps"],
        },
    },
    {
        "name": "activate_plan",
        "description": "激活计划，开始执行第一步。创建计划后需要用户确认才调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "计划 ID"},
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "update_plan_step",
        "description": (
            "更新计划中某个步骤的状态和结果。"
            "完成一个步骤后调用，会自动推进下一步。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "计划 ID"},
                "step_id": {"type": "string", "description": "步骤 ID（如 step_1）"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked"],
                    "description": "新状态",
                },
                "outcome": {
                    "type": "string",
                    "description": "步骤完成的结果描述",
                },
            },
            "required": ["plan_id", "step_id"],
        },
    },
    {
        "name": "list_plans",
        "description": "查看当前活跃的计划列表。用于了解有哪些正在进行的大任务。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "只看某个用户的计划（可选）",
                },
            },
        },
    },
    {
        "name": "get_plan_detail",
        "description": "获取某个计划的完整详情。",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "计划 ID"},
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "cancel_plan",
        "description": "取消计划。当用户说不做了/换方案时调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "计划 ID"},
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "schedule_step",
        "description": (
            "安排计划步骤的自动执行时间。到时间后 bot 会自动执行该步骤并通知用户。"
            "用于跨天/跨小时的大计划：你可以根据步骤复杂度合理安排时间。"
            "例如复杂步骤安排明天上午，简单步骤安排 30 分钟后。"
            "不需要问用户什么时候执行，你自己判断即可，用户会收到执行通知。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "计划 ID"},
                "step_id": {"type": "string", "description": "步骤 ID"},
                "scheduled_at": {
                    "type": "string",
                    "description": "执行时间（ISO 格式，如 2026-02-21T09:30:00+08:00）",
                },
                "notify_user_id": {
                    "type": "string",
                    "description": "完成后通知谁（open_id）",
                },
                "notify_user_name": {
                    "type": "string",
                    "description": "通知的用户名",
                },
            },
            "required": ["plan_id", "step_id", "scheduled_at"],
        },
    },
]

TOOL_MAP = {
    "save_memory": save_memory,
    "recall_memory": recall_memory,
    "recall_org_memory": recall_org_memory,
    "create_plan": create_plan,
    "activate_plan": activate_plan_tool,
    "update_plan_step": update_plan_step,
    "list_plans": list_plans,
    "get_plan_detail": get_plan_detail,
    "cancel_plan": cancel_plan_tool,
    "schedule_step": schedule_step,
}
