"""定时提醒工具 —— 让 bot 能定时提醒用户

存储：Redis Sorted Set（per-user 隔离）
  key:   reminders:{tenant_id}:{user_id}   — 每个用户独立 Sorted Set
  score: 下次触发 unix timestamp
  member: JSON payload

  辅助索引:
  reminder_users:{tenant_id}                — SET，记录有活跃提醒的 user_id

触发：scheduler.py 的 _reminder_loop 动态 sleep（无提醒时 30 分钟，
      有即将到期的提醒时精确 sleep 到触发时刻）。

支持：
- 一次性提醒（remind_at 一个时间点）
- 重复提醒（daily / weekly / monthly）
- 触发时可执行 LLM 动作（action 非空时调 agent）
- 用户管理（list / cancel / update）
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import redis_client
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

_REDIS_KEY_PREFIX = "reminders"
_USER_INDEX_PREFIX = "reminder_users"


# ── 内部辅助 ──


def _key(tenant_id: str, user_id: str) -> str:
    """Per-user reminder key: reminders:{tenant_id}:{user_id}"""
    return f"{_REDIS_KEY_PREFIX}:{tenant_id}:{user_id}"


def _old_key(tenant_id: str) -> str:
    """Legacy key (pre-migration): reminders:{tenant_id}"""
    return f"{_REDIS_KEY_PREFIX}:{tenant_id}"


def _user_index_key(tenant_id: str) -> str:
    """Index of user_ids with active reminders: reminder_users:{tenant_id}"""
    return f"{_USER_INDEX_PREFIX}:{tenant_id}"


def _register_user(tenant_id: str, user_id: str) -> None:
    """Add user_id to the tenant's reminder user index."""
    if user_id:
        redis_client.execute("SADD", _user_index_key(tenant_id), user_id)


def _unregister_user_if_empty(tenant_id: str, user_id: str) -> None:
    """Remove user_id from index if they have no more reminders."""
    if not user_id:
        return
    count = redis_client.execute("ZCARD", _key(tenant_id, user_id))
    if not count or count == 0:
        redis_client.execute("SREM", _user_index_key(tenant_id), user_id)


def get_active_user_ids(tenant_id: str) -> list[str]:
    """Get all user_ids that have active reminders for this tenant."""
    raw = redis_client.execute("SMEMBERS", _user_index_key(tenant_id))
    if not raw or not isinstance(raw, list):
        return []
    return [uid for uid in raw if isinstance(uid, str)]


def migrate_legacy_key(tenant_id: str) -> int:
    """Migrate reminders from old key (reminders:{tid}) to per-user keys.

    Returns number of reminders migrated. Safe to call multiple times.
    """
    old = _old_key(tenant_id)
    raw = redis_client.execute("ZRANGEBYSCORE", old, "0", "+inf")
    if not raw or not isinstance(raw, list):
        return 0

    migrated = 0
    for item in raw:
        try:
            data = json.loads(item)
            uid = data.get("user_id", "")
            if not uid:
                continue
            trigger_dt = _parse_time(data.get("next_trigger", ""))
            if not trigger_dt:
                continue
            score = trigger_dt.timestamp()
            redis_client.execute("ZADD", _key(tenant_id, uid), str(score), item)
            _register_user(tenant_id, uid)
            migrated += 1
        except (json.JSONDecodeError, TypeError):
            pass

    if migrated > 0:
        redis_client.execute("DEL", old)
        logger.info("reminder: migrated %d reminders from legacy key %s", migrated, old)
    return migrated


def _gen_id() -> str:
    return f"rem_{secrets.token_hex(4)}"


def _get_tenant_id() -> str:
    try:
        from app.tenant.context import get_current_tenant
        return get_current_tenant().tenant_id
    except Exception:
        return ""


def _get_timezone() -> ZoneInfo:
    try:
        from app.tenant.context import get_current_tenant
        tz = get_current_tenant().scheduler_timezone
        if tz:
            return ZoneInfo(tz)
    except Exception:
        pass
    return ZoneInfo("Asia/Shanghai")


def _parse_time(s: str, tz_name: str = "") -> datetime | None:
    """解析时间字符串。

    如果提供了 tz_name（IANA 时区名如 America/Los_Angeles），
    将时间解释为该时区的本地时间（自动处理夏令时）。
    这比固定 UTC offset 更准确——LLM 传 -08:00 不会自动变成 -07:00。
    """
    try:
        dt = datetime.fromisoformat(s)
        if tz_name:
            try:
                target_tz = ZoneInfo(tz_name)
                # 用 IANA 时区替换字面 offset，让 DST 自动生效
                # 例如：2026-03-11T09:00:00-08:00 + tz=America/Los_Angeles
                # → 解释为旧金山当地 9:00（实际是 PDT -07:00）
                naive = dt.replace(tzinfo=None)
                dt = naive.replace(tzinfo=target_tz)
            except Exception:
                pass  # 无效时区名，保持原样
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_get_timezone())
        return dt
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(_get_timezone())


def calc_next_trigger(recurrence: dict, after: datetime | None = None) -> datetime | None:
    """根据 recurrence 配置计算下一次触发时间。

    recurrence: {"type": "daily|weekly|monthly|interval", "time": "HH:MM", "days": [int], "interval_days": int}
    after: 从哪个时间之后算起（默认 now）
    """
    rtype = recurrence.get("type", "none")
    if rtype == "none":
        return None

    time_str = recurrence.get("time", "09:00")
    try:
        hh, mm = int(time_str.split(":")[0]), int(time_str.split(":")[1])
    except (ValueError, IndexError):
        hh, mm = 9, 0

    tz = _get_timezone()
    base = after or _now()
    # 确保 base 有时区
    if base.tzinfo is None:
        base = base.replace(tzinfo=tz)

    if rtype == "daily":
        # 明天同一时间
        candidate = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate

    if rtype == "weekly":
        days = recurrence.get("days", [1])  # 默认周一
        if not days:
            days = [1]
        # 找最近的 weekday（1=周一 ... 7=周日）
        current_weekday = base.isoweekday()  # 1-7
        candidates = []
        for d in days:
            diff = d - current_weekday
            if diff < 0:
                diff += 7
            cand = base + timedelta(days=diff)
            cand = cand.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= base:
                cand += timedelta(weeks=1)
            candidates.append(cand)
        return min(candidates)

    if rtype == "monthly":
        days = recurrence.get("days", [1])  # 默认 1 号
        if not days:
            days = [1]
        day = min(days)
        # 本月或下月
        try:
            candidate = base.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            # 日期不合法（如 2 月 30 号），用当月最后一天
            import calendar
            last_day = calendar.monthrange(base.year, base.month)[1]
            candidate = base.replace(day=last_day, hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= base:
            # 下月
            if base.month == 12:
                next_month = base.replace(year=base.year + 1, month=1, day=1)
            else:
                next_month = base.replace(month=base.month + 1, day=1)
            try:
                candidate = next_month.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
            except ValueError:
                import calendar
                last_day = calendar.monthrange(next_month.year, next_month.month)[1]
                candidate = next_month.replace(day=last_day, hour=hh, minute=mm, second=0, microsecond=0)
        return candidate

    if rtype == "interval":
        # 统一换算成分钟，支持任意粒度（分钟/小时/天）
        interval_minutes = recurrence.get("interval_minutes", 0)
        interval_hours = recurrence.get("interval_hours", 0)
        interval_days = recurrence.get("interval_days", 0)
        total_minutes = (
            int(interval_minutes)
            + int(interval_hours) * 60
            + int(interval_days) * 1440
        )
        if total_minutes < 1:
            total_minutes = 1440  # fallback: 1 天
        if total_minutes < 1440:
            # 小时/分钟级间隔：从当前触发时间直接累加，不重置 hour/minute
            candidate = base + timedelta(minutes=total_minutes)
        else:
            # 天级间隔：累加后重置到指定时间（保持每天同一时刻触发）
            candidate = base + timedelta(minutes=total_minutes)
            candidate = candidate.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return candidate

    return None


# ── Redis 操作 ──


def _save_reminder(tenant_id: str, reminder: dict) -> bool:
    """ZADD reminder to per-user sorted set."""
    user_id = reminder.get("user_id", "")
    if not user_id:
        logger.warning("reminder: cannot save without user_id")
        return False
    trigger_dt = _parse_time(reminder["next_trigger"])
    if not trigger_dt:
        return False
    score = trigger_dt.timestamp()
    member = json.dumps(reminder, ensure_ascii=False)
    result = redis_client.execute("ZADD", _key(tenant_id, user_id), str(score), member)
    if result is not None:
        _register_user(tenant_id, user_id)
    return result is not None


def _remove_reminder_by_member(tenant_id: str, user_id: str, member_json: str) -> bool:
    result = redis_client.execute("ZREM", _key(tenant_id, user_id), member_json)
    ok = result is not None and result > 0
    if ok:
        _unregister_user_if_empty(tenant_id, user_id)
    return ok


def _get_all_reminders_for_user(tenant_id: str, user_id: str) -> list[dict]:
    """获取该用户的所有提醒。"""
    raw = redis_client.execute("ZRANGEBYSCORE", _key(tenant_id, user_id), "0", "+inf")
    if not raw or not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        try:
            result.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def _find_reminder_by_id(tenant_id: str, user_id: str, reminder_id: str) -> tuple[dict | None, str | None]:
    """查找提醒，返回 (parsed_dict, raw_json_member)。"""
    raw = redis_client.execute("ZRANGEBYSCORE", _key(tenant_id, user_id), "0", "+inf")
    if not raw or not isinstance(raw, list):
        return None, None
    for item in raw:
        try:
            data = json.loads(item)
            if data.get("id") == reminder_id:
                return data, item
        except (json.JSONDecodeError, TypeError):
            pass
    return None, None


def get_due_reminders_for_user(tenant_id: str, user_id: str) -> list[tuple[dict, str]]:
    """获取该用户所有到期的提醒。返回 [(parsed, raw_member), ...]。"""
    now_ts = str(time.time())
    raw = redis_client.execute("ZRANGEBYSCORE", _key(tenant_id, user_id), "0", now_ts)
    if not raw or not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        try:
            result.append((json.loads(item), item))
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def get_due_reminders(tenant_id: str) -> list[tuple[dict, str]]:
    """获取该租户所有用户的到期提醒。遍历 user index。"""
    all_due = []
    for uid in get_active_user_ids(tenant_id):
        all_due.extend(get_due_reminders_for_user(tenant_id, uid))
    return all_due


def get_nearest_trigger_ts(tenant_id: str) -> float | None:
    """获取该租户最近一条提醒的 trigger timestamp（跨所有用户）。"""
    nearest = None
    for uid in get_active_user_ids(tenant_id):
        raw = redis_client.execute(
            "ZRANGEBYSCORE", _key(tenant_id, uid), "0", "+inf", "LIMIT", "0", "1",
        )
        if not raw or not isinstance(raw, list) or len(raw) == 0:
            continue
        try:
            data = json.loads(raw[0])
            dt = _parse_time(data.get("next_trigger", ""))
            if dt:
                ts = dt.timestamp()
                if nearest is None or ts < nearest:
                    nearest = ts
        except Exception:
            pass
    return nearest


def _describe_recurrence(rtype: str, recurrence: dict) -> str:
    """格式化重复间隔描述。"""
    if rtype == "daily":
        return "每天"
    if rtype == "weekly":
        return "每周"
    if rtype == "monthly":
        return "每月"
    if rtype == "interval":
        parts = []
        d = recurrence.get("interval_days", 0)
        h = recurrence.get("interval_hours", 0)
        m = recurrence.get("interval_minutes", 0)
        if d:
            parts.append(f"{d}天")
        if h:
            parts.append(f"{h}小时")
        if m:
            parts.append(f"{m}分钟")
        return "每" + "".join(parts) if parts else "每天"
    return rtype


# ── 工具处理函数 ──


def set_reminder(args: dict) -> ToolResult:
    """创建提醒。"""
    text = args.get("text", "").strip()
    if not text:
        return ToolResult.invalid_param("text（提醒内容）不能为空")

    remind_at = args.get("remind_at", "").strip()
    tz_name = args.get("timezone", "").strip()
    recurrence = args.get("recurrence") or {}
    rtype = recurrence.get("type", "none") if isinstance(recurrence, dict) else "none"

    # 一次性提醒必须有 remind_at
    if rtype == "none" and not remind_at:
        return ToolResult.invalid_param("一次性提醒需要 remind_at 参数（ISO 格式时间）")

    # 计算 next_trigger
    if rtype != "none" and isinstance(recurrence, dict):
        next_dt = calc_next_trigger(recurrence)
        if not next_dt:
            return ToolResult.invalid_param(f"无法计算重复提醒的下次触发时间: {recurrence}")
    else:
        next_dt = _parse_time(remind_at, tz_name=tz_name)
        if not next_dt:
            return ToolResult.invalid_param(f"无法解析时间: {remind_at}")
        if next_dt <= _now():
            return ToolResult.invalid_param("提醒时间必须在未来")
        recurrence = {"type": "none"}

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取租户上下文", code="internal")

    user_id = args.get("user_id", "")
    user_name = args.get("user_name", "")

    reminder = {
        "id": _gen_id(),
        "text": text,
        "action": args.get("action", ""),
        "user_id": user_id,
        "user_name": user_name,
        "created_at": _now().isoformat(),
        "recurrence": recurrence,
        "next_trigger": next_dt.isoformat(),
        "tenant_id": tenant_id,
    }

    if not _save_reminder(tenant_id, reminder):
        return ToolResult.api_error("保存提醒到 Redis 失败")

    # 格式化回复
    time_str = next_dt.strftime("%Y-%m-%d %H:%M")
    if rtype != "none":
        recurrence_desc = _describe_recurrence(rtype, recurrence)
        msg = f"已设置重复提醒（{recurrence_desc}）\n内容：{text}\n下次提醒：{time_str}\nID：{reminder['id']}"
    else:
        msg = f"已设置提醒\n内容：{text}\n时间：{time_str}\nID：{reminder['id']}"

    if reminder.get("action"):
        msg += f"\n触发时执行：{reminder['action']}"

    return ToolResult.success(msg)


def list_reminders(args: dict) -> ToolResult:
    """列出用户的所有提醒。"""
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取租户上下文", code="internal")

    user_id = args.get("user_id", "")
    if not user_id:
        return ToolResult.invalid_param("user_id 不能为空")

    # 直接从 per-user key 读取，无需 app 层过滤
    reminders = _get_all_reminders_for_user(tenant_id, user_id)

    if not reminders:
        return ToolResult.success("当前没有设置任何提醒。")

    lines = [f"共 {len(reminders)} 个提醒：\n"]
    for r in reminders:
        rtype = r.get("recurrence", {}).get("type", "none")
        recurrence_tag = ""
        if rtype != "none":
            recurrence_tag = f" [{_describe_recurrence(rtype, r.get('recurrence', {}))}]"

        trigger = r.get("next_trigger", "?")
        try:
            trigger = datetime.fromisoformat(trigger).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        line = f"• {r.get('id', '?')} | {trigger}{recurrence_tag} | {r.get('text', '')}"
        if r.get("action"):
            line += f" (动作: {r['action'][:30]})"
        lines.append(line)

    return ToolResult.success("\n".join(lines))


def cancel_reminder(args: dict) -> ToolResult:
    """取消提醒。"""
    reminder_id = args.get("reminder_id", "").strip()
    if not reminder_id:
        return ToolResult.invalid_param("需要 reminder_id 参数")

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取租户上下文", code="internal")

    user_id = args.get("user_id", "").strip()
    if not user_id:
        return ToolResult.invalid_param("需要 user_id 参数")

    reminder, raw_member = _find_reminder_by_id(tenant_id, user_id, reminder_id)
    if not reminder or not raw_member:
        return ToolResult.not_found(f"找不到提醒 {reminder_id}")

    if _remove_reminder_by_member(tenant_id, user_id, raw_member):
        return ToolResult.success(f"已取消提醒：{reminder.get('text', '')}")
    return ToolResult.api_error("取消提醒失败")


def update_reminder(args: dict) -> ToolResult:
    """修改提醒的时间、内容或重复配置。"""
    reminder_id = args.get("reminder_id", "").strip()
    if not reminder_id:
        return ToolResult.invalid_param("需要 reminder_id 参数")

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return ToolResult.error("无法获取租户上下文", code="internal")

    user_id = args.get("user_id", "").strip()
    if not user_id:
        return ToolResult.invalid_param("需要 user_id 参数")

    reminder, raw_member = _find_reminder_by_id(tenant_id, user_id, reminder_id)
    if not reminder or not raw_member:
        return ToolResult.not_found(f"找不到提醒 {reminder_id}")

    # 更新字段
    if args.get("text"):
        reminder["text"] = args["text"]
    if args.get("action") is not None:
        reminder["action"] = args["action"]

    new_recurrence = args.get("recurrence")
    new_remind_at = args.get("remind_at", "").strip()
    tz_name = args.get("timezone", "").strip()

    if new_recurrence and isinstance(new_recurrence, dict):
        reminder["recurrence"] = new_recurrence
        next_dt = calc_next_trigger(new_recurrence)
        if not next_dt:
            return ToolResult.invalid_param("无法计算新的触发时间")
        reminder["next_trigger"] = next_dt.isoformat()
    elif new_remind_at:
        next_dt = _parse_time(new_remind_at, tz_name=tz_name)
        if not next_dt:
            return ToolResult.invalid_param(f"无法解析时间: {new_remind_at}")
        if next_dt <= _now():
            return ToolResult.invalid_param("提醒时间必须在未来")
        reminder["next_trigger"] = next_dt.isoformat()
        reminder["recurrence"] = {"type": "none"}

    # 先删旧的，再存新的（score 可能变了）
    _remove_reminder_by_member(tenant_id, user_id, raw_member)
    if not _save_reminder(tenant_id, reminder):
        return ToolResult.api_error("更新提醒失败")

    trigger_str = reminder["next_trigger"]
    try:
        trigger_str = datetime.fromisoformat(trigger_str).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass

    return ToolResult.success(f"已更新提醒 {reminder_id}\n内容：{reminder.get('text', '')}\n下次提醒：{trigger_str}")


# ── 工具定义 ──

TOOL_DEFINITIONS = [
    {
        "name": "set_reminder",
        "description": (
            "设置定时提醒。支持一次性和重复提醒（每天/每周/每月/每隔N天/每隔N小时/每隔N分钟）。"
            "可以只发消息提醒，也可以触发时让 bot 执行动作（如查天气、发报告、发小红书帖子）。"
            "例如：「下周三上午9点提醒我交报告」「每天早上9点提醒我看数据」"
            "「每隔2天帮我发一篇小红书帖子」「每周五下午3点帮我汇总本周进度」"
            "「每小时提醒我一次」「每30分钟提醒我喝水」"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "提醒内容（简要描述提醒什么事）",
                },
                "remind_at": {
                    "type": "string",
                    "description": "提醒时间（ISO 格式，如 2026-03-11T09:00:00）。一次性提醒必填，重复提醒可不填。建议配合 timezone 使用，避免夏令时偏差。",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA 时区名（如 America/Los_Angeles、Asia/Shanghai）。设置后会自动处理夏令时，比固定 UTC offset 更准确。强烈建议：涉及非本地时区时务必填写。",
                },
                "recurrence": {
                    "type": "object",
                    "description": "重复配置（不填=一次性提醒）",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["daily", "weekly", "monthly", "interval", "none"],
                            "description": "重复类型。interval=自定义间隔（可组合 interval_days/interval_hours/interval_minutes）",
                        },
                        "time": {
                            "type": "string",
                            "description": "触发时间 HH:MM（如 09:00）。用于 daily/weekly/monthly 和天级 interval。",
                        },
                        "days": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "weekly: 周几（1=周一...7=周日）；monthly: 几号（1-31）",
                        },
                        "interval_days": {
                            "type": "integer",
                            "description": "interval 类型：间隔天数。可与 interval_hours/interval_minutes 叠加。",
                        },
                        "interval_hours": {
                            "type": "integer",
                            "description": "interval 类型：间隔小时数（每小时=1，每2小时=2）。",
                        },
                        "interval_minutes": {
                            "type": "integer",
                            "description": "interval 类型：间隔分钟数（每30分钟=30，每45分钟=45）。",
                        },
                    },
                },
                "action": {
                    "type": "string",
                    "description": "触发时让 bot 执行的动作指令（可选）。如「帮我查今天天气」「汇总本周工作进度」。为空则只发文本提醒。",
                },
                "user_id": {
                    "type": "string",
                    "description": "提醒谁（用户 ID）",
                },
                "user_name": {
                    "type": "string",
                    "description": "用户名（显示用）",
                },
            },
            "required": ["text", "user_id"],
        },
    },
    {
        "name": "list_reminders",
        "description": "查看当前用户设置的所有提醒（包括一次性和重复的）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "用户 ID，查看该用户的提醒",
                },
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "cancel_reminder",
        "description": "取消一个已设置的提醒。需要 reminder_id（可通过 list_reminders 获取）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "string",
                    "description": "提醒 ID（如 rem_abc123）",
                },
                "user_id": {
                    "type": "string",
                    "description": "用户 ID（提醒所属用户）",
                },
            },
            "required": ["reminder_id", "user_id"],
        },
    },
    {
        "name": "update_reminder",
        "description": "修改已有提醒的内容、时间或重复配置。",
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "string",
                    "description": "提醒 ID",
                },
                "user_id": {
                    "type": "string",
                    "description": "用户 ID（提醒所属用户）",
                },
                "text": {
                    "type": "string",
                    "description": "新的提醒内容（不填则不改）",
                },
                "remind_at": {
                    "type": "string",
                    "description": "新的提醒时间 ISO 格式（不填则不改）",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA 时区名（如 America/Los_Angeles），自动处理夏令时",
                },
                "recurrence": {
                    "type": "object",
                    "description": "新的重复配置（不填则不改）",
                    "properties": {
                        "type": {"type": "string", "enum": ["daily", "weekly", "monthly", "interval", "none"]},
                        "time": {"type": "string"},
                        "days": {"type": "array", "items": {"type": "integer"}},
                        "interval_days": {"type": "integer", "description": "间隔天数"},
                        "interval_hours": {"type": "integer", "description": "间隔小时数"},
                        "interval_minutes": {"type": "integer", "description": "间隔分钟数"},
                    },
                },
                "action": {
                    "type": "string",
                    "description": "新的触发动作（空字符串=清除动作）",
                },
            },
            "required": ["reminder_id", "user_id"],
        },
    },
]

TOOL_MAP = {
    "set_reminder": lambda args: set_reminder(args),
    "list_reminders": lambda args: list_reminders(args),
    "cancel_reminder": lambda args: cancel_reminder(args),
    "update_reminder": lambda args: update_reminder(args),
}
