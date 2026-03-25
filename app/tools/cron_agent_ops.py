"""Cron Agent 工具 —— 管理定时 AI Agent 任务

借鉴 NanoClaw 的 Scheduled Tasks：不只是定时提醒，
而是定时运行完整的 AI Agent 任务（有工具访问权限）。

用例：
- "每天早上 9 点审查 GitHub PR 并发飞书汇总"
- "每周五下午 5 点生成本周工作报告"
- "每天中午搜索竞品动态，有更新就通知我"
"""

from __future__ import annotations

import json
from app.tools.tool_result import ToolResult

# ── Tool Definitions ──

TOOL_DEFINITIONS = [
    {
        "name": "create_cron_agent",
        "description": (
            "创建一个定时 AI Agent 任务。任务会按 cron 表达式定时执行，"
            "运行完整的 AI Agent（可以使用工具），并把结果发送给指定用户。\n\n"
            "cron 表达式格式: 分 时 日 月 星期\n"
            "例: '0 9 * * 1-5' = 工作日上午 9 点\n"
            "例: '30 17 * * 5' = 每周五下午 5:30\n"
            "例: '0 */2 * * *' = 每 2 小时"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "任务名称（如 '每日代码审查'）"
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron 表达式（分 时 日 月 星期）"
                },
                "prompt": {
                    "type": "string",
                    "description": "Agent 执行的指令（如 '审查今天的 PR，生成摘要并发到群里'）"
                },
                "tool_groups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent 可用的工具组: core/feishu_collab/code_dev/research/content/admin"
                },
                "notify_chat_id": {
                    "type": "string",
                    "description": "执行结果发送到的群聊 ID（可选）"
                },
                "timezone": {
                    "type": "string",
                    "description": "时区（默认 Asia/Shanghai）"
                },
            },
            "required": ["name", "cron_expr", "prompt"],
        },
    },
    {
        "name": "list_cron_agents",
        "description": "列出当前租户的所有定时 Agent 任务",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "delete_cron_agent",
        "description": "删除一个定时 Agent 任务",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "要删除的任务 ID"
                },
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "toggle_cron_agent",
        "description": "启用或暂停一个定时 Agent 任务",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "任务 ID"
                },
                "enabled": {
                    "type": "boolean",
                    "description": "true=启用, false=暂停"
                },
            },
            "required": ["agent_id", "enabled"],
        },
    },
    {
        "name": "get_cron_agent_log",
        "description": "查看定时任务的执行日志",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回最近几条日志（默认 10）"
                },
            },
        },
    },
]


# ── Tool Handlers ──

def _create_cron_agent(args: dict) -> str:
    from app.tenant.context import get_current_tenant, get_current_sender
    from app.services.cron_agent import CronAgentConfig, save_cron_agent, cron_matches

    tenant = get_current_tenant()
    if not getattr(tenant, "cron_agent_enabled", False):
        return ToolResult(False, "定时 Agent 任务未启用。请在租户配置中设置 cron_agent_enabled: true").to_str()

    # 验证 cron 表达式
    cron_expr = args.get("cron_expr", "").strip()
    parts = cron_expr.split()
    if len(parts) != 5:
        return ToolResult(
            False, "cron 表达式格式错误，需要 5 个字段: 分 时 日 月 星期",
            retry_hint="例: '0 9 * * 1-5' = 工作日上午 9 点"
        ).to_str()

    sender = get_current_sender()
    config = CronAgentConfig(
        name=args.get("name", ""),
        cron_expr=cron_expr,
        prompt=args.get("prompt", ""),
        tool_groups=args.get("tool_groups", ["core"]),
        notify_user_id=sender.sender_id,
        notify_chat_id=args.get("notify_chat_id", ""),
        timezone=args.get("timezone", tenant.scheduler_timezone or "Asia/Shanghai"),
        created_by=sender.sender_id,
    )

    if save_cron_agent(tenant.tenant_id, config):
        return ToolResult(True, json.dumps({
            "agent_id": config.agent_id,
            "name": config.name,
            "cron_expr": config.cron_expr,
            "status": "已创建",
            "message": f"定时 Agent 任务 [{config.name}] 已创建，cron: {config.cron_expr}"
        }, ensure_ascii=False)).to_str()
    return ToolResult(False, "创建失败，请重试").to_str()


def _list_cron_agents(args: dict) -> str:
    from app.tenant.context import get_current_tenant
    from app.services.cron_agent import list_cron_agents

    tenant = get_current_tenant()
    agents = list_cron_agents(tenant.tenant_id)
    if not agents:
        return ToolResult(True, "当前没有定时 Agent 任务").to_str()

    from datetime import datetime
    result = []
    for a in agents:
        last_run = datetime.fromtimestamp(a.last_run).strftime("%Y-%m-%d %H:%M") if a.last_run else "未执行"
        result.append({
            "agent_id": a.agent_id,
            "name": a.name,
            "cron": a.cron_expr,
            "enabled": a.enabled,
            "last_run": last_run,
            "prompt_preview": a.prompt[:80],
        })
    return ToolResult(True, json.dumps(result, ensure_ascii=False)).to_str()


def _delete_cron_agent(args: dict) -> str:
    from app.tenant.context import get_current_tenant
    from app.services.cron_agent import delete_cron_agent

    tenant = get_current_tenant()
    agent_id = args.get("agent_id", "")
    if delete_cron_agent(tenant.tenant_id, agent_id):
        return ToolResult(True, f"定时任务 {agent_id} 已删除").to_str()
    return ToolResult(False, f"找不到任务 {agent_id}").to_str()


def _toggle_cron_agent(args: dict) -> str:
    from app.tenant.context import get_current_tenant
    from app.services.cron_agent import get_cron_agent, save_cron_agent

    tenant = get_current_tenant()
    agent_id = args.get("agent_id", "")
    enabled = args.get("enabled", True)

    config = get_cron_agent(tenant.tenant_id, agent_id)
    if not config:
        return ToolResult(False, f"找不到任务 {agent_id}").to_str()

    config.enabled = enabled
    save_cron_agent(tenant.tenant_id, config)
    status = "已启用" if enabled else "已暂停"
    return ToolResult(True, f"定时任务 [{config.name}] {status}").to_str()


def _get_cron_agent_log(args: dict) -> str:
    from app.tenant.context import get_current_tenant
    from app.services.cron_agent import get_execution_log

    tenant = get_current_tenant()
    limit = args.get("limit", 10)
    log = get_execution_log(tenant.tenant_id, limit)
    if not log:
        return ToolResult(True, "暂无执行日志").to_str()
    return ToolResult(True, json.dumps(log, ensure_ascii=False)).to_str()


# ── Tool Map ──

TOOL_MAP = {
    "create_cron_agent": _create_cron_agent,
    "list_cron_agents": _list_cron_agents,
    "delete_cron_agent": _delete_cron_agent,
    "toggle_cron_agent": _toggle_cron_agent,
    "get_cron_agent_log": _get_cron_agent_log,
}
