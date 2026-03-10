"""用户查询与忙闲工具

- lookup_user: 通过名字查 open_id（从已知用户表中）
- list_known_users: 列出所有和 bot 对话过的用户
- check_availability: 查询用户忙闲状态（freebusy API）
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from app.services import user_registry
from app.tools.feishu_api import feishu_post
from app.tools._fuzzy import fuzzy_filter
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)


def _parse_time(time_str: str) -> str:
    """时间字符串 → Unix 时间戳"""
    time_str = time_str.strip()
    if time_str.isdigit() and len(time_str) >= 10:
        return time_str
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(time_str, fmt)
            return str(int(dt.timestamp()))
        except ValueError:
            continue
    return ""


def _ts_to_display(ts: str) -> str:
    if not ts:
        return "?"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except (ValueError, OSError):
        return ts


def lookup_user(name: str) -> ToolResult:
    """通过名字查找用户的 open_id"""
    oid = user_registry.find_by_name(name)
    if oid:
        real_name = user_registry.get_name(oid)
        return ToolResult.success(f"找到用户: {real_name} (open_id: {oid})")

    # 本地没找到 → 尝试从通讯录同步一次再查
    try:
        added = user_registry.sync_org_contacts()
        if added > 0:
            oid = user_registry.find_by_name(name)
            if oid:
                real_name = user_registry.get_name(oid)
                return ToolResult.success(f"找到用户: {real_name} (open_id: {oid})")
    except Exception:
        logger.warning("sync_org_contacts failed during lookup", exc_info=True)

    # 列出所有已知用户供参考
    all_users = user_registry.all_users()
    if all_users:
        names = [f"{n} ({oid[:12]}...)" for oid, n in all_users.items()]
        return ToolResult.success(f"未找到「{name}」，已知用户: {', '.join(names)}")
    return ToolResult.success(f"未找到「{name}」，目前还没有用户和 bot 对话过。")


def list_known_users(keyword: str = "") -> ToolResult:
    """列出所有已知用户，支持 keyword 模糊过滤姓名"""
    all_users = user_registry.all_users()
    if not all_users:
        return ToolResult.success("目前还没有用户和 bot 对话过。")

    items = [{"name": name, "open_id": oid} for oid, name in all_users.items()]
    if keyword:
        items = fuzzy_filter(items, keyword, ["name"])
    if not items:
        return ToolResult.success(f"没有找到匹配「{keyword}」的用户。")

    lines = [f"找到 {len(items)} 位用户：\n"]
    for u in items:
        lines.append(f"  - {u['name']} (open_id: {u['open_id']})")
    return ToolResult.success("\n".join(lines))


def check_availability(
    user_open_id: str,
    start_time: str,
    end_time: str = "",
) -> ToolResult:
    """通过 freebusy API 查询用户忙闲"""
    start_ts = _parse_time(start_time)
    if not start_ts:
        return ToolResult.invalid_param(f"无法解析开始时间: {start_time}")

    if end_time:
        end_ts = _parse_time(end_time)
        if not end_ts:
            return ToolResult.invalid_param(f"无法解析结束时间: {end_time}")
    else:
        # 默认查一天
        end_ts = str(int(start_ts) + 86400)

    data = feishu_post(
        "/calendar/v4/freebusy/list",
        json={
            "time_min": start_ts,
            "time_max": end_ts,
            "user_id": user_open_id,
        },
        params={"user_id_type": "open_id"},
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    freebusy_list = data.get("data", {}).get("freebusy_list", [])
    if not freebusy_list:
        name = user_registry.get_name(user_open_id) or user_open_id
        return ToolResult.success(f"{name} 在该时间段没有日程安排，是空闲的。")

    name = user_registry.get_name(user_open_id) or user_open_id
    lines = [f"{name} 的忙碌时段：\n"]
    for slot in freebusy_list:
        s = _ts_to_display(slot.get("start_time", ""))
        e = _ts_to_display(slot.get("end_time", ""))
        lines.append(f"  - {s} ~ {e}")

    return ToolResult.success("\n".join(lines))


# --------------- Tool definitions & map ---------------

TOOL_DEFINITIONS = [
    {
        "name": "lookup_user",
        "description": "通过名字查找飞书用户的 open_id。用于创建日程、分配任务、发私信时指定参与人。支持查找组织内所有成员。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "用户名字（支持模糊匹配）",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_known_users",
        "description": "列出所有已知的组织成员（名字 + open_id）。支持 keyword 模糊过滤姓名。包括通讯录同步的和对话过的。",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "按姓名模糊过滤",
                    "default": "",
                },
            },
        },
    },
    {
        "name": "check_availability",
        "description": "查询某个用户在指定时间段是否有空（飞书忙闲查询）。不需要对方共享日历。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_open_id": {
                    "type": "string",
                    "description": "用户的 open_id。可先用 lookup_user 按名字查。",
                },
                "start_time": {
                    "type": "string",
                    "description": "查询起始时间，格式: 'YYYY-MM-DD HH:MM'",
                },
                "end_time": {
                    "type": "string",
                    "description": "查询结束时间。不填则查一整天。",
                    "default": "",
                },
            },
            "required": ["user_open_id", "start_time"],
        },
    },
]

TOOL_MAP = {
    "lookup_user": lambda args: lookup_user(name=args["name"]),
    "list_known_users": lambda args: list_known_users(keyword=args.get("keyword", "")),
    "check_availability": lambda args: check_availability(
        user_open_id=args["user_open_id"],
        start_time=args["start_time"],
        end_time=args.get("end_time", ""),
    ),
}
