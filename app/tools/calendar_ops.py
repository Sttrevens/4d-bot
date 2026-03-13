"""飞书日历操作工具

通过飞书 Calendar v4 API 管理日程：
- 查看 bot 可访问的所有日历（含共享日历）
- 创建日程（可指定日历）
- 查询日程列表（可指定日历）
- 删除日程
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from app.tools._fuzzy import fuzzy_filter
from zoneinfo import ZoneInfo

from app.tools.feishu_api import (
    feishu_get, feishu_post, feishu_patch, feishu_delete,
    has_user_token, _current_user_open_id,
)
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

_FALLBACK_TZ = ZoneInfo("Asia/Shanghai")

# 每用户时区缓存: open_id → ZoneInfo
_user_tz_cache: dict[str, ZoneInfo] = {}

# 无 open_id 时的短期缓存（避免同一请求内重复调 HTTP）
_anon_tz_cache: ZoneInfo | None = None
_anon_tz_expire: float = 0
_ANON_TZ_TTL = 120  # 秒


# 常见中国城市 → 时区映射不需要（全是 Asia/Shanghai）
# 这里只列国际城市的映射
_CITY_TZ_MAP: dict[str, str] = {
    # 东亚
    "东京": "Asia/Tokyo", "tokyo": "Asia/Tokyo",
    "大阪": "Asia/Tokyo", "osaka": "Asia/Tokyo",
    "首尔": "Asia/Seoul", "seoul": "Asia/Seoul",
    # 东南亚
    "新加坡": "Asia/Singapore", "singapore": "Asia/Singapore",
    "曼谷": "Asia/Bangkok", "bangkok": "Asia/Bangkok",
    "吉隆坡": "Asia/Kuala_Lumpur", "kuala lumpur": "Asia/Kuala_Lumpur",
    "雅加达": "Asia/Jakarta", "jakarta": "Asia/Jakarta",
    "马尼拉": "Asia/Manila", "manila": "Asia/Manila",
    "胡志明": "Asia/Ho_Chi_Minh", "ho chi minh": "Asia/Ho_Chi_Minh",
    # 南亚
    "孟买": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
    "新德里": "Asia/Kolkata", "new delhi": "Asia/Kolkata",
    "班加罗尔": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    # 中东
    "迪拜": "Asia/Dubai", "dubai": "Asia/Dubai",
    # 欧洲
    "伦敦": "Europe/London", "london": "Europe/London",
    "巴黎": "Europe/Paris", "paris": "Europe/Paris",
    "柏林": "Europe/Berlin", "berlin": "Europe/Berlin",
    "阿姆斯特丹": "Europe/Amsterdam", "amsterdam": "Europe/Amsterdam",
    "莫斯科": "Europe/Moscow", "moscow": "Europe/Moscow",
    # 北美
    "纽约": "America/New_York", "new york": "America/New_York",
    "旧金山": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "洛杉矶": "America/Los_Angeles", "los angeles": "America/Los_Angeles",
    "西雅图": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "芝加哥": "America/Chicago", "chicago": "America/Chicago",
    "多伦多": "America/Toronto", "toronto": "America/Toronto",
    "温哥华": "America/Vancouver", "vancouver": "America/Vancouver",
    # 大洋洲
    "悉尼": "Australia/Sydney", "sydney": "Australia/Sydney",
    "墨尔本": "Australia/Melbourne", "melbourne": "Australia/Melbourne",
    "奥克兰": "Pacific/Auckland", "auckland": "Pacific/Auckland",
}


def _tz_from_city(city: str) -> ZoneInfo | None:
    """尝试从城市名推断时区。中国城市统一返回 Asia/Shanghai。"""
    if not city:
        return None
    city_lower = city.strip().lower()
    tz_name = _CITY_TZ_MAP.get(city_lower)
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (KeyError, Exception):
            pass
    # 中国城市（包括港澳台以外的中文城市名）→ 默认 Asia/Shanghai
    # 不在映射表里的非空 city 也返回 None，让后续 fallback 处理
    return None


def _tz_from_contact(open_id: str) -> ZoneInfo | None:
    """通过 Contact API 查用户 city 字段推断时区。

    用 tenant_access_token（不需要用户授权），需要 contact:user.employee_info:readonly 权限。
    """
    if not open_id:
        return None
    try:
        data = feishu_get(
            f"/contact/v3/users/{open_id}",
            params={"user_id_type": "open_id"},
            use_user_token=False,
        )
        if isinstance(data, str):
            return None
        user = data.get("data", {}).get("user", {})
        city = user.get("city", "")
        if city:
            tz = _tz_from_city(city)
            if tz:
                logger.info("User %s city=%s → tz=%s (from contact API)",
                            open_id[:10], city, tz)
                return tz
    except Exception:
        pass
    return None


def _get_user_tz() -> ZoneInfo:
    """获取当前用户的时区（多层 fallback，缓存）。

    优先级：缓存 → contact API (city) → calendar API (主日历) → Asia/Shanghai
    缓存策略:
    - 有 open_id: 永久缓存（直到重启）
    - 无 open_id: 短期 TTL 缓存，避免同一请求内多次 HTTP 调用
    """
    global _anon_tz_cache, _anon_tz_expire

    open_id = _current_user_open_id.get("")
    if open_id and open_id in _user_tz_cache:
        return _user_tz_cache[open_id]

    # 无 open_id 时用短期缓存
    if not open_id and _anon_tz_cache and time.time() < _anon_tz_expire:
        return _anon_tz_cache

    tz: ZoneInfo | None = None

    # 层 1：Contact API → city → timezone
    if open_id:
        tz = _tz_from_contact(open_id)

    # 层 2：Calendar API → 主日历 time_zone（仅飞书租户）
    if not tz:
        try:
            from app.tenant.context import get_current_tenant
            _platform = (get_current_tenant() or type("_", (), {"platform": "feishu"})).platform
        except Exception:
            _platform = "feishu"
        cals = _fetch_calendars() if _platform == "feishu" else []
        if not isinstance(cals, str):
            for cal in cals:
                if cal.get("type") == "primary":
                    tz_name = cal.get("time_zone", "")
                    if tz_name:
                        try:
                            tz = ZoneInfo(tz_name)
                        except (KeyError, Exception):
                            pass
                    break
            if not tz:
                for cal in cals:
                    if cal.get("role") == "owner":
                        tz_name = cal.get("time_zone", "")
                        if tz_name:
                            try:
                                tz = ZoneInfo(tz_name)
                            except (KeyError, Exception):
                                pass
                        break

    # 缓存结果
    result = tz or _FALLBACK_TZ
    if open_id:
        _user_tz_cache[open_id] = result
    else:
        _anon_tz_cache = result
        _anon_tz_expire = time.time() + _ANON_TZ_TTL
    if tz:
        logger.info("User %s timezone: %s", open_id[:10] if open_id else "?", result)
    return result


def _use_user() -> bool:
    """有用户 token 就用用户身份，否则用 bot 身份"""
    return has_user_token()


def _check_reauth_needed() -> str:
    """检查当前用户是否需要重新授权。

    返回空字符串表示无需重新授权；否则返回提示信息。
    用于在 token 失效后避免静默降级到 tenant_token（tenant_token 没日历权限，
    会返回误导性的 'field validation failed'）。
    """
    from app.tools.feishu_api import _current_user_open_id
    open_id = _current_user_open_id.get("")
    if not open_id:
        return ""
    from app.services.oauth_store import needs_reauth
    if needs_reauth(open_id):
        return (
            "用户的飞书授权已失效（token 过期或被吊销），需要重新授权才能访问日历。"
            "请告诉用户发 /auth 重新授权。这不是代码 bug，不需要自我修复。"
        )
    return ""


# 日历列表 TTL 缓存（避免每次操作都调 GET /calendars）
# 日历列表缓存：按 token 类型分别缓存（user vs bot 返回的日历列表不同）
_cal_list_cache: dict[bool, list[dict]] = {}  # key=use_user_token
_cal_list_expire: dict[bool, float] = {}
_CAL_LIST_TTL = 300  # 5 分钟

# 负缓存：API 返回错误时缓存错误结果，避免反复请求
_cal_list_neg_cache: dict[bool, str] = {}
_cal_list_neg_expire: dict[bool, float] = {}
_CAL_LIST_NEG_TTL = 60  # 1 分钟（短 TTL，避免 re-auth 后长时间卡住）


def invalidate_calendar_cache() -> None:
    """清除日历列表缓存（正缓存+负缓存）。
    在用户重新 OAuth 授权后调用，确保下次 API 调用会真正请求飞书。
    """
    global _cal_list_cache, _cal_list_expire
    global _cal_list_neg_cache, _cal_list_neg_expire
    _cal_list_cache = None
    _cal_list_expire = 0
    _cal_list_neg_cache = None
    _cal_list_neg_expire = 0
    logger.debug("calendar cache invalidated")


def _fetch_calendars() -> list[dict] | str:
    """获取可访问的所有日历（有用户 token 用用户身份，否则用 bot 身份）。

    结果缓存 5 分钟。错误结果缓存 1 分钟（负缓存），
    避免权限未开通时反复请求 API 刷屏。"""
    # 授权失效时直接报错，不要静默降级到 tenant_token
    reauth_msg = _check_reauth_needed()
    if reauth_msg:
        return reauth_msg

    now = time.time()
    use_user = _use_user()

    # 按 token 类型分别缓存（user token 和 bot token 返回的日历列表不同）
    if use_user in _cal_list_cache and now < _cal_list_expire.get(use_user, 0):
        return _cal_list_cache[use_user]
    # 负缓存命中：直接返回上次的错误，不再请求
    if use_user in _cal_list_neg_cache and now < _cal_list_neg_expire.get(use_user, 0):
        return _cal_list_neg_cache[use_user]

    data = feishu_get("/calendar/v4/calendars", params={"page_size": 50}, use_user_token=use_user)
    if isinstance(data, str):
        _cal_list_neg_cache[use_user] = data
        _cal_list_neg_expire[use_user] = now + _CAL_LIST_NEG_TTL
        logger.warning("calendar list API failed (user_token=%s), negative-cached for %ds: %s",
                        use_user, _CAL_LIST_NEG_TTL, data[:200])
        return data
    # 成功：清除该 token 类型的负缓存
    _cal_list_neg_cache.pop(use_user, None)
    _cal_list_neg_expire.pop(use_user, None)
    result = data.get("data", {}).get("calendar_list", [])
    _cal_list_cache[use_user] = result
    _cal_list_expire[use_user] = now + _CAL_LIST_TTL
    return result


# bot 共享日历缓存（进程内）
_bot_calendar_id: str = ""


def _ensure_bot_calendar() -> str:
    """确保 bot 拥有一个共享日历，没有就创建一个。返回 calendar_id 或错误。"""
    global _bot_calendar_id
    if _bot_calendar_id:
        return _bot_calendar_id

    # 先找 bot 已有的共享日历（用 tenant_token）
    data = feishu_get("/calendar/v4/calendars", params={"page_size": 50}, use_user_token=False)
    if not isinstance(data, str):
        cals = data.get("data", {}).get("calendar_list", [])
        for cal in cals:
            if cal.get("role") == "owner" and cal.get("type") in ("primary", "shared"):
                _bot_calendar_id = cal["calendar_id"]
                logger.info("found bot calendar: %s (%s)", cal.get("summary", ""), _bot_calendar_id[:20])
                return _bot_calendar_id

    # 没有就创建一个共享日历
    result = feishu_post(
        "/calendar/v4/calendars",
        json={
            "summary": "四哥助手日历",
            "description": "Bot 创建的团队共享日历",
            "permissions": "show_only_free_busy",
        },
        use_user_token=False,
    )
    if isinstance(result, str):
        logger.error("failed to create bot calendar: %s", result)
        return f"[ERROR] 无法创建 bot 日历: {result}"

    cal = result.get("data", {}).get("calendar", {})
    _bot_calendar_id = cal.get("calendar_id", "")
    logger.info("created bot calendar: %s", _bot_calendar_id[:20])
    return _bot_calendar_id


def _get_primary_calendar_id() -> str:
    """获取可写日历 ID。有用户 token 找用户有写权限的日历，否则用 bot 共享日历。"""
    use_user = _use_user()
    if use_user:
        cals = _fetch_calendars()
        if isinstance(cals, str):
            logger.warning("calendar fetch failed with user token, falling back to bot calendar: %s", cals[:100])
            return _ensure_bot_calendar()

        writable_roles = {"owner", "writer"}

        # 优先：用户主日历（有写权限）
        for cal in cals:
            if cal.get("type") == "primary" and cal.get("role") in writable_roles:
                logger.info("using user's primary calendar: %s (role=%s)", cal["calendar_id"][:20], cal["role"])
                return cal["calendar_id"]

        # 其次：任何有写权限的日历
        for cal in cals:
            if cal.get("role") in writable_roles:
                logger.info("using user's writable calendar: %s (role=%s)", cal["calendar_id"][:20], cal["role"])
                return cal["calendar_id"]

        # 用户有 token 但没有可写日历 → 列出所有日历的 role 帮助诊断
        cal_roles = [(c.get("summary", "?"), c.get("type", "?"), c.get("role", "?")) for c in cals[:5]]
        logger.warning("user has token but no writable calendar (roles: %s), falling back to bot calendar", cal_roles)
    else:
        logger.info("no user token available, using bot calendar")

    # bot 身份：用 bot 自己的日历
    return _ensure_bot_calendar()


def _resolve_calendar_id(calendar_id: str = "") -> str:
    """有指定就用指定的，否则用主日历"""
    if calendar_id:
        return calendar_id
    return _get_primary_calendar_id()


def _get_all_calendar_ids() -> list[str]:
    """获取所有可访问的日历 ID 列表（用于 event 查找的 fallback）"""
    cals = _fetch_calendars()
    if isinstance(cals, str):
        return []
    ids = []
    # 优先主日历
    for cal in cals:
        if cal.get("type") == "primary":
            ids.append(cal["calendar_id"])
    # 再加其他有读权限的日历
    for cal in cals:
        cid = cal["calendar_id"]
        if cid not in ids:
            ids.append(cid)
    return ids


def _find_event_calendar(event_id: str, calendar_id: str = "") -> tuple[str, dict | None, str]:
    """在多个日历中查找 event 所在的日历。

    Returns (encoded_cal_id, event_data, error_msg)。
    找到返回 (id, data, "")，找不到返回 ("", None, error)。
    指定了 calendar_id 时只查该日历。

    优先查主日历（与 create_event 用同一个 _resolve_calendar_id），
    找不到再 fallback 到其他日历。避免在共享/只读日历上找到 event
    但后续 PATCH 没权限（193001/193002 错误）。
    """
    use_user = _use_user()
    if calendar_id:
        cal_ids = [calendar_id]
    else:
        # 优先主日历（与 create_event 一致）
        primary = _get_primary_calendar_id()
        if primary.startswith("[ERROR]"):
            return "", None, primary
        cal_ids = [primary]
        # fallback: 其他日历（去重）
        all_cals = _get_all_calendar_ids()
        for cid in all_cals:
            if cid not in cal_ids:
                cal_ids.append(cid)

    last_error = ""
    for cid in cal_ids:
        encoded = quote(cid, safe="")
        result = feishu_get(
            f"/calendar/v4/calendars/{encoded}/events/{event_id}",
            params={"user_id_type": "open_id"},
            use_user_token=use_user,
        )
        if not isinstance(result, str):
            return encoded, result, ""
        last_error = result

    return "", None, last_error or f"[ERROR] 在所有日历中都找不到 event_id={event_id}"


def _parse_time(time_str: str) -> str:
    """将用户友好的时间字符串转为 Unix 时间戳字符串（使用用户时区）"""
    time_str = time_str.strip()
    if time_str.isdigit() and len(time_str) >= 10:
        return time_str

    # 检测并提取尾部的 IANA 时区名（如 "America/Los_Angeles"、"Asia/Shanghai"）
    explicit_tz = None
    import re as _re
    tz_match = _re.search(r'\s+([A-Z][a-z]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)$', time_str)
    if tz_match:
        try:
            explicit_tz = ZoneInfo(tz_match.group(1))
            time_str = time_str[:tz_match.start()].strip()
        except (KeyError, ValueError):
            pass  # 不是合法时区名，当作普通文本处理

    tz = explicit_tz or _get_user_tz()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(time_str, fmt).replace(tzinfo=tz)
            return str(int(dt.timestamp()))
        except ValueError:
            continue
    return ""


def _ts_to_display(ts: str) -> str:
    if not ts:
        return "?"
    try:
        tz = _get_user_tz()
        dt = datetime.fromtimestamp(int(ts), tz=tz)
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, OSError):
        return ts


# ======================== 工具实现 ========================

def list_calendars() -> ToolResult:
    """列出可访问的所有日历（有 auth 看用户日历，无 auth 看 bot 日历）"""
    cals = _fetch_calendars()
    if isinstance(cals, str):
        return ToolResult.api_error(cals)

    if not cals:
        return ToolResult.not_found("没有找到可访问的日历。请确认 bot 的日历权限已开启。")

    lines = [f"找到 {len(cals)} 个日历：\n"]
    for cal in cals:
        name = cal.get("summary", "(未命名)")
        cal_id = cal.get("calendar_id", "")
        cal_type = cal.get("type", "")
        role = cal.get("role", "")
        desc = cal.get("description", "")
        tz_name = cal.get("time_zone", "")

        type_label = {
            "primary": "主日历",
            "shared": "共享日历",
            "google": "Google",
            "resource": "会议室",
            "exchange": "Exchange",
        }.get(cal_type, cal_type)

        line = f"  - {name} [{type_label}, {role}]"
        line += f"\n    calendar_id: {cal_id}"
        if tz_name:
            line += f"\n    时区: {tz_name}"
        if desc:
            line += f"\n    描述: {desc}"
        lines.append(line)

    return ToolResult.success("\n".join(lines))


def _add_attendees(
    encoded_cal_id: str,
    event_id: str,
    open_ids: list[str] | None,
    emails: list[str] | None,
    chat_ids: list[str] | None = None,
) -> list[str]:
    """添加参与人（内部用户 / 外部邮箱 / 整群），返回结果行"""
    attendees: list[dict] = []
    if chat_ids:
        attendees += [{"type": "chat", "chat_id": cid} for cid in chat_ids]
    if open_ids:
        attendees += [{"type": "user", "user_id": uid} for uid in open_ids]
    if emails:
        attendees += [{"type": "third_party", "third_party_email": e} for e in emails]
    if not attendees:
        return []

    desc_parts = []
    if chat_ids:
        desc_parts.append(f"{len(chat_ids)} 个群")
    if open_ids:
        desc_parts.append(f"{len(open_ids)} 位用户")
    if emails:
        desc_parts.append(f"{len(emails)} 个邮箱")

    att_data = feishu_post(
        f"/calendar/v4/calendars/{encoded_cal_id}/events/{event_id}/attendees",
        json={"attendees": attendees, "need_notification": True},
        params={"user_id_type": "open_id"},
        use_user_token=_use_user(),
    )
    if isinstance(att_data, str):
        return [f"添加参与人失败: {att_data}"]
    return [f"已邀请参与人: {', '.join(desc_parts)}"]


def create_event(
    summary: str,
    start_time: str,
    end_time: str = "",
    description: str = "",
    location: str = "",
    attendee_open_ids: list[str] | None = None,
    attendee_emails: list[str] | None = None,
    attendee_chat_ids: list[str] | None = None,
    attendee_ability: str = "",
    calendar_id: str = "",
    timezone: str = "",
) -> ToolResult:
    """创建飞书日程（有 auth 在用户日历创建，无 auth 在 bot 日历创建并邀请）"""
    cal_id = _resolve_calendar_id(calendar_id)
    if cal_id.startswith("[ERROR]"):
        return ToolResult.api_error(cal_id)

    # 确定时区：优先用显式传入的 timezone 参数
    explicit_tz = None
    if timezone:
        try:
            explicit_tz = ZoneInfo(timezone)
            logger.info("create_event: using explicit timezone=%s", timezone)
        except (KeyError, ValueError):
            # 尝试城市名映射
            explicit_tz = _tz_from_city(timezone)
            if explicit_tz:
                logger.info("create_event: timezone '%s' mapped to %s via city", timezone, explicit_tz)
            else:
                logger.warning("create_event: invalid timezone '%s', falling back to user tz", timezone)

    # 如果有显式时区，追加到时间字符串末尾让 _parse_time 处理
    _tz_for_parse = explicit_tz
    if _tz_for_parse:
        # 直接将时区附加到时间字符串，_parse_time 已支持 IANA 时区名
        if not any(start_time.rstrip().endswith(z) for z in (str(_tz_for_parse),)):
            start_time = f"{start_time} {_tz_for_parse}"
        if end_time and not any(end_time.rstrip().endswith(z) for z in (str(_tz_for_parse),)):
            end_time = f"{end_time} {_tz_for_parse}"

    start_ts = _parse_time(start_time)
    if not start_ts:
        return ToolResult.invalid_param(f"无法解析开始时间: {start_time}，请使用格式: YYYY-MM-DD HH:MM")

    if end_time:
        end_ts = _parse_time(end_time)
        if not end_ts:
            return ToolResult.invalid_param(f"无法解析结束时间: {end_time}")
    else:
        end_ts = str(int(start_ts) + 3600)

    # 过去日期警告：如果开始时间在过去 24 小时以前，很可能是年份错误
    try:
        _start_epoch = int(start_ts)
        _now_epoch = int(time.time())
        _days_ago = (_now_epoch - _start_epoch) / 86400
        if _days_ago > 1:
            _when = f"{int(_days_ago)} 天前" if _days_ago < 365 else f"{_days_ago/365:.1f} 年前"
            return ToolResult.error(
                f"⚠️ 开始时间 {start_time} 是过去的日期（{_when}）。"
                "这很可能是年份错误（例如活动页面显示的是去年的日期）。"
                "请和用户确认正确的日期和年份后重试。"
                "如果用户确实要创建过去的日程，请在 description 中注明。"
            )
    except (ValueError, TypeError):
        pass

    tz = explicit_tz or _get_user_tz()
    tz_name = str(tz)

    body: dict = {
        "summary": summary,
        "start_time": {"timestamp": start_ts, "timezone": tz_name},
        "end_time": {"timestamp": end_ts, "timezone": tz_name},
        "need_notification": True,
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = {"name": location}
    if attendee_ability:
        body["attendee_ability"] = attendee_ability
    else:
        # 默认允许参与人邀请他人，方便团队协作
        body["attendee_ability"] = "can_invite_others"
        
    encoded_cal_id = quote(cal_id, safe="")
    data = feishu_post(f"/calendar/v4/calendars/{encoded_cal_id}/events", json=body, use_user_token=_use_user())

    # 如果用户日历 403/191002，自动降级到 bot 日历重试
    if isinstance(data, str) and not calendar_id:
        if "403" in data or "191002" in data or "access_role" in data:
            logger.warning("user calendar failed (%s), retrying with bot calendar", data[:80])
            bot_cal = _ensure_bot_calendar()
            if not bot_cal.startswith("[ERROR]"):
                encoded_bot = quote(bot_cal, safe="")
                data = feishu_post(f"/calendar/v4/calendars/{encoded_bot}/events", json=body, use_user_token=False)
                if not isinstance(data, str):
                    cal_id = bot_cal
                    encoded_cal_id = encoded_bot

    if isinstance(data, str):
        return ToolResult.api_error(data)

    event = data.get("data", {}).get("event", {})
    event_id = event.get("event_id", "")
    result_lines = [f"日程已创建: {summary}"]

    if event_id:
        result_lines += _add_attendees(encoded_cal_id, event_id, attendee_open_ids, attendee_emails, attendee_chat_ids)

    return ToolResult.success("\n".join(result_lines))


def _format_events(items: list[dict]) -> str:
    """格式化日程列表"""
    if not items:
        return "该时间段内没有日程。"

    lines = [f"找到 {len(items)} 个日程：\n"]
    for ev in items:
        summary = ev.get("summary", "(无标题)")
        start = ev.get("start_time", {})
        end = ev.get("end_time", {})
        start_display = _ts_to_display(start.get("timestamp", ""))
        end_display = _ts_to_display(end.get("timestamp", ""))
        loc = ev.get("location", {}).get("name", "")
        loc_str = f"  地点: {loc}" if loc else ""
        event_id = ev.get("event_id", "")

        lines.append(f"  - {summary} (event_id: {event_id})")
        lines.append(f"    时间: {start_display} ~ {end_display}{loc_str}")

    return "\n".join(lines)


def list_events(
    start_date: str = "",
    end_date: str = "",
    max_results: int = 20,
    calendar_id: str = "",
    keyword: str = "",
) -> ToolResult:
    """查询日程列表（有 auth 查用户日历，无 auth 查 bot 日历），支持 keyword 模糊过滤"""
    # 检查 token 是否刚失效——比静默降级到 tenant_token 更有用
    reauth_msg = _check_reauth_needed()
    if reauth_msg:
        return ToolResult.api_error(reauth_msg)

    cal_id = _resolve_calendar_id(calendar_id)
    if cal_id.startswith("[ERROR]"):
        return ToolResult.api_error(cal_id)

    tz = _get_user_tz()
    tz_name = str(tz)
    now = time.time()

    start_ts = _parse_time(start_date + " 00:00") if start_date else ""
    if not start_ts:
        start_ts = str(int(now))

    end_ts = _parse_time(end_date + " 23:59") if end_date else ""
    if not end_ts:
        end_ts = str(int(now + 7 * 86400))

    encoded_cal_id = quote(cal_id, safe="")
    use_user = _use_user()

    # search 端点只支持 user_access_token，bot 身份直接走 list
    if use_user:
        body = {
            "query": keyword,  # 服务端搜索（空字符串=不过滤）
            "filter": {
                "start_time": {"timestamp": start_ts, "timezone": tz_name},
                "end_time": {"timestamp": end_ts, "timezone": tz_name},
            },
        }
        data = feishu_post(
            f"/calendar/v4/calendars/{encoded_cal_id}/events/search",
            json=body,
            params={"page_size": max_results, "user_id_type": "open_id"},
            use_user_token=True,
        )
        if not isinstance(data, str):
            items = data.get("data", {}).get("items", [])
            # keyword 已传给 API，但仍做客户端二次过滤确保精确
            if keyword:
                items = fuzzy_filter(items, keyword, ["summary"])
            return ToolResult.success(_format_events(items))
        logger.warning("search endpoint failed (%s), falling back to list", data)

    # list 端点（GET）— tenant_token 和 user_token 都能用
    # 飞书 list events API 有时间范围上限（~90天），超出返回 field validation failed
    # 所以大范围查询需要拆成多个小窗口
    _MAX_WINDOW = 80 * 86400  # 80天（秒），留点余量
    # keyword 搜索时拉取更多事件再客户端过滤（list 端点不支持服务端搜索）
    fetch_size = 50 if keyword else max_results
    start_int, end_int = int(start_ts), int(end_ts)
    all_items: list[dict] = []

    window_start = start_int
    while window_start < end_int:
        window_end = min(window_start + _MAX_WINDOW, end_int)
        page_token = ""
        while True:
            params: dict[str, str | int] = {
                "start_time": str(window_start),
                "end_time": str(window_end),
                "page_size": fetch_size,
                "user_id_type": "open_id",
            }
            if page_token:
                params["page_token"] = page_token
            data = feishu_get(
                f"/calendar/v4/calendars/{encoded_cal_id}/events",
                params=params,
                use_user_token=use_user,
            )
            if isinstance(data, str):
                logger.warning("list events failed for window %s-%s: %s", window_start, window_end, data)
                break
            all_items.extend(data.get("data", {}).get("items", []))
            # 分页：有更多数据且在做 keyword 搜索时继续拉取
            page_token = data.get("data", {}).get("page_token", "")
            has_more = data.get("data", {}).get("has_more", False)
            if not (keyword and has_more and page_token):
                break
        window_start = window_end

    # 按 event_id 去重（窗口边界可能重叠）
    seen = set()
    items = []
    for item in all_items:
        eid = item.get("event_id", "")
        if eid and eid in seen:
            continue
        seen.add(eid)
        items.append(item)

    if keyword:
        items = fuzzy_filter(items, keyword, ["summary"])
    return ToolResult.success(_format_events(items))


def update_event(
    event_id: str,
    summary: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    location: str = "",
    calendar_id: str = "",
    attendee_open_ids: list[str] | None = None,
    attendee_emails: list[str] | None = None,
    attendee_chat_ids: list[str] | None = None,
    attendee_ability: str = "",
    timezone: str = "",
) -> ToolResult:
    """修改飞书日程（只更新传入的字段，有 auth 用用户身份，无 auth 用 bot 身份）"""
    # 在多个日历中查找 event
    encoded_cal_id, _, err = _find_event_calendar(event_id, calendar_id)
    if err:
        return ToolResult.api_error(err)

    # 确定时区：优先用显式传入的 timezone 参数（与 create_event 一致）
    explicit_tz = None
    if timezone:
        try:
            explicit_tz = ZoneInfo(timezone)
        except (KeyError, ValueError):
            explicit_tz = _tz_from_city(timezone)
            if not explicit_tz:
                logger.warning("update_event: invalid timezone '%s', falling back to user tz", timezone)

    tz = explicit_tz or _get_user_tz()
    tz_name = str(tz)

    # 如果有显式时区，追加到时间字符串让 _parse_time 处理
    if explicit_tz:
        if start_time and not start_time.rstrip().endswith(str(explicit_tz)):
            start_time = f"{start_time} {explicit_tz}"
        if end_time and not end_time.rstrip().endswith(str(explicit_tz)):
            end_time = f"{end_time} {explicit_tz}"

    body: dict = {}
    if summary:
        body["summary"] = summary
    if start_time:
        ts = _parse_time(start_time)
        if not ts:
            return ToolResult.invalid_param(f"无法解析开始时间: {start_time}")
        body["start_time"] = {"timestamp": ts, "timezone": tz_name}
    if end_time:
        ts = _parse_time(end_time)
        if not ts:
            return ToolResult.invalid_param(f"无法解析结束时间: {end_time}")
        body["end_time"] = {"timestamp": ts, "timezone": tz_name}
    if description:
        body["description"] = description
    if location:
        body["location"] = {"name": location}
    if attendee_ability:
        body["attendee_ability"] = attendee_ability
    result_lines: list[str] = []

    if body:
        data = feishu_patch(
            f"/calendar/v4/calendars/{encoded_cal_id}/events/{event_id}",
            json=body,
            use_user_token=_use_user(),
        )
        if isinstance(data, str):
            return ToolResult.api_error(data)
        updated = data.get("data", {}).get("event", {})
        name = updated.get("summary", summary or event_id)
        result_lines.append(f"日程已更新: {name}")

    # 添加参与人（通过独立的 attendees API）
    if attendee_open_ids or attendee_emails or attendee_chat_ids:
        result_lines += _add_attendees(encoded_cal_id, event_id, attendee_open_ids, attendee_emails, attendee_chat_ids)

    if not result_lines:
        return ToolResult.invalid_param("没有要更新的内容")
    return ToolResult.success("\n".join(result_lines))


def delete_event(event_id: str, calendar_id: str = "") -> ToolResult:
    """删除日程（有 auth 用用户身份，无 auth 用 bot 身份；用户身份失败自动回退 bot 身份）"""
    encoded_cal_id, _, err = _find_event_calendar(event_id, calendar_id)
    if err:
        return ToolResult.api_error(err)

    use_user = _use_user()
    data = feishu_delete(f"/calendar/v4/calendars/{encoded_cal_id}/events/{event_id}", use_user_token=use_user)
    if isinstance(data, str):
        if use_user and "193002" in data:
            # 用户身份无权删除（可能是 bot 创建的事件，用户只是 attendee）
            # 回退到 bot 身份，在 bot 日历上尝试删除
            bot_cal = _ensure_bot_calendar()
            if bot_cal and not bot_cal.startswith("[ERROR]"):
                bot_encoded = quote(bot_cal, safe="")
                # 先确认 event 在 bot 日历上存在
                check = feishu_get(
                    f"/calendar/v4/calendars/{bot_encoded}/events/{event_id}",
                    params={"user_id_type": "open_id"},
                    use_user_token=False,
                )
                if not isinstance(check, str):
                    data2 = feishu_delete(
                        f"/calendar/v4/calendars/{bot_encoded}/events/{event_id}",
                        use_user_token=False,
                    )
                    if not isinstance(data2, str):
                        logger.info("delete_event: user token failed (193002), succeeded with bot token on bot calendar")
                        return ToolResult.success(f"日程 {event_id} 已删除")
                    data = data2  # 使用 bot 删除的错误信息
        return ToolResult.api_error(data)
    return ToolResult.success(f"日程 {event_id} 已删除")


def get_event_detail(event_id: str, calendar_id: str = "") -> ToolResult:
    """获取日程详细信息，包括标题、时间、地点、描述、参与人列表"""
    use_user = _use_user()

    # 在多个日历中查找该 event（同时拿到详情数据，避免重复请求）
    encoded_cal_id, data, err = _find_event_calendar(event_id, calendar_id)
    if err:
        return ToolResult.api_error(err)

    event = data.get("data", {}).get("event", {})
    summary = event.get("summary", "(无标题)")
    description = event.get("description", "")
    start = event.get("start_time", {})
    end = event.get("end_time", {})
    start_display = _ts_to_display(start.get("timestamp", ""))
    end_display = _ts_to_display(end.get("timestamp", ""))
    loc = event.get("location", {}).get("name", "")
    status = event.get("status", "")
    ability = event.get("attendee_ability", "")

    lines = [
        f"日程: {summary}",
        f"event_id: {event_id}",
        f"时间: {start_display} ~ {end_display}",
    ]
    if loc:
        lines.append(f"地点: {loc}")
    if description:
        lines.append(f"描述: {description[:500]}")
    if status:
        lines.append(f"状态: {status}")
    if ability:
        lines.append(f"参与人权限: {ability}")

    # 获取参与人列表
    att_data = feishu_get(
        f"/calendar/v4/calendars/{encoded_cal_id}/events/{event_id}/attendees",
        params={"page_size": 50, "user_id_type": "open_id"},
        use_user_token=use_user,
    )
    if not isinstance(att_data, str):
        attendees = att_data.get("data", {}).get("items", [])
        if attendees:
            lines.append(f"\n参与人 ({len(attendees)} 人):")
            for att in attendees:
                att_type = att.get("type", "")
                name = att.get("attendee_name", "")
                rsvp = att.get("rsvp_status", "")
                rsvp_label = {
                    "needs_action": "待回复",
                    "accept": "已接受",
                    "tentative": "暂定",
                    "decline": "已拒绝",
                    "removed": "已移除",
                }.get(rsvp, rsvp)

                if att_type == "user":
                    uid = att.get("user_id", "")
                    lines.append(f"  - {name} (user, open_id: {uid}) [{rsvp_label}]")
                elif att_type == "chat":
                    cid = att.get("chat_id", "")
                    lines.append(f"  - {name} (群, chat_id: {cid}) [{rsvp_label}]")
                elif att_type == "third_party":
                    email = att.get("third_party_email", "")
                    lines.append(f"  - {name or email} (邮箱) [{rsvp_label}]")
                else:
                    lines.append(f"  - {name} ({att_type}) [{rsvp_label}]")
        else:
            lines.append("\n暂无参与人")
    else:
        lines.append(f"\n获取参与人失败: {att_data}")

    return ToolResult.success("\n".join(lines))


def remove_event_attendees(
    event_id: str,
    attendee_open_ids: list[str] | None = None,
    attendee_emails: list[str] | None = None,
    attendee_chat_ids: list[str] | None = None,
    calendar_id: str = "",
) -> ToolResult:
    """从日程中移除参与人"""
    encoded_cal_id, _, err = _find_event_calendar(event_id, calendar_id)
    if err:
        return ToolResult.api_error(err)

    use_user = _use_user()

    # 先获取当前参与人列表，找到要移除的 attendee_id
    att_data = feishu_get(
        f"/calendar/v4/calendars/{encoded_cal_id}/events/{event_id}/attendees",
        params={"page_size": 50, "user_id_type": "open_id"},
        use_user_token=use_user,
    )
    if isinstance(att_data, str):
        return ToolResult.api_error(f"获取参与人列表失败: {att_data}")

    current = att_data.get("data", {}).get("items", [])
    if not current:
        return ToolResult.not_found("该日程没有参与人")

    # 收集要移除的 open_ids / emails / chat_ids
    remove_ids: set[str] = set()
    if attendee_open_ids:
        remove_ids.update(attendee_open_ids)
    remove_emails: set[str] = set()
    if attendee_emails:
        remove_emails.update(e.lower() for e in attendee_emails)
    remove_chats: set[str] = set()
    if attendee_chat_ids:
        remove_chats.update(attendee_chat_ids)

    # 匹配要移除的参与人
    to_remove: list[dict] = []
    for att in current:
        att_type = att.get("type", "")
        att_id = att.get("attendee_id", "")
        matched = False
        if att_type == "user" and att.get("user_id", "") in remove_ids:
            matched = True
        elif att_type == "third_party" and att.get("third_party_email", "").lower() in remove_emails:
            matched = True
        elif att_type == "chat" and att.get("chat_id", "") in remove_chats:
            matched = True
        if matched and att_id:
            to_remove.append({"type": att_type, "attendee_id": att_id})

    if not to_remove:
        names = [a.get("attendee_name", a.get("user_id", "?")) for a in current]
        return ToolResult.not_found(
            f"没有找到匹配的参与人。当前参与人: {', '.join(names)}\n"
            "请使用 get_event_detail 查看完整参与人列表，确认 open_id / email / chat_id 后重试。"
        )

    # 批量删除参与人
    del_data = feishu_post(
        f"/calendar/v4/calendars/{encoded_cal_id}/events/{event_id}/attendees/batch_delete",
        json={
            "attendee_ids": [a["attendee_id"] for a in to_remove],
            "need_notification": True,
        },
        use_user_token=use_user,
    )
    if isinstance(del_data, str):
        return ToolResult.api_error(f"移除参与人失败: {del_data}")
    return ToolResult.success(f"已从日程中移除 {len(to_remove)} 位参与人")


# --------------- Tool definitions & map ---------------

TOOL_DEFINITIONS = [
    {
        "name": "list_calendars",
        "description": "列出 bot 可访问的所有飞书日历（含用户共享给 bot 的日历）。返回 calendar_id，供其他日历工具使用。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "在飞书日历上创建日程/会议。可指定标题、时间、地点、参与人。"
            "重要：如果活动发生在非用户所在时区的城市（如用户在上海但活动在旧金山），"
            "必须设置 timezone 参数为活动所在地时区，否则时间会按用户本地时区处理。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "日程标题",
                },
                "start_time": {
                    "type": "string",
                    "description": "开始时间，格式: 'YYYY-MM-DD HH:MM' 或 Unix 时间戳",
                },
                "end_time": {
                    "type": "string",
                    "description": "结束时间，格式同上。不填则默认 1 小时",
                    "default": "",
                },
                "timezone": {
                    "type": "string",
                    "description": (
                        "活动所在地时区（IANA 格式）。当活动在用户所在时区以外的城市时必须设置。"
                        "例如: 'America/Los_Angeles'（旧金山/洛杉矶）, 'America/New_York'（纽约）, "
                        "'Europe/London'（伦敦）, 'Asia/Tokyo'（东京）。"
                        "不填则自动检测用户时区（通常是 Asia/Shanghai）。"
                    ),
                    "default": "",
                },
                "description": {
                    "type": "string",
                    "description": "日程描述",
                    "default": "",
                },
                "location": {
                    "type": "string",
                    "description": "地点",
                    "default": "",
                },
                "attendee_open_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "组织内参与人的 open_id 列表",
                },
                "attendee_emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "外部参与人的邮箱列表（跨组织邀请用邮箱）",
                },
                "attendee_chat_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "飞书群 chat_id 列表，整群拉入日程（比逐个拉人更准确高效）",
                },
                "attendee_ability": {
                    "type": "string",
                    "description": "参与人权限: none | can_see_others | can_invite_others | can_modify_event（任何人可修改日程）",
                    "default": "",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "日历 ID。不填则用主日历。可先用 list_calendars 查看可用日历。",
                    "default": "",
                },
            },
            "required": ["summary", "start_time"],
        },
    },
    {
        "name": "list_calendar_events",
        "description": "搜索飞书日历上的日程。默认查主日历未来 7 天，可指定日期范围。支持 keyword 模糊过滤标题。时间按用户时区自动处理。",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "起始日期，格式: 'YYYY-MM-DD'。不填则从今天开始",
                    "default": "",
                },
                "end_date": {
                    "type": "string",
                    "description": "结束日期，格式同上。不填则到 7 天后",
                    "default": "",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回数量，默认 10",
                    "default": 10,
                },
                "calendar_id": {
                    "type": "string",
                    "description": "日历 ID。不填则用主日历。可先用 list_calendars 查看可用日历。",
                    "default": "",
                },
                "keyword": {
                    "type": "string",
                    "description": "按日程标题模糊过滤（支持子串、多词、去标点匹配）",
                    "default": "",
                },
            },
        },
    },
    {
        "name": "get_event_detail",
        "description": "获取飞书日程的详细信息，包括标题、时间、地点、描述、参与人列表及其回复状态。需要 event_id，可先用 list_calendar_events 查到。",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "日程 ID",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "日历 ID。不填则用主日历。",
                    "default": "",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "update_calendar_event",
        "description": "修改飞书日历上已有的日程。可更新标题、时间、地点、描述、参与人。只传需要改的字段即可。需要 event_id，可先用 list_calendar_events 查到。",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "要修改的日程 ID",
                },
                "summary": {
                    "type": "string",
                    "description": "新标题",
                    "default": "",
                },
                "start_time": {
                    "type": "string",
                    "description": "新的开始时间，格式: 'YYYY-MM-DD HH:MM'",
                    "default": "",
                },
                "end_time": {
                    "type": "string",
                    "description": "新的结束时间",
                    "default": "",
                },
                "description": {
                    "type": "string",
                    "description": "新描述",
                    "default": "",
                },
                "location": {
                    "type": "string",
                    "description": "新地点",
                    "default": "",
                },
                "attendee_open_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "组织内参与人的 open_id 列表",
                },
                "attendee_emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "外部参与人的邮箱列表（跨组织邀请用邮箱）",
                },
                "attendee_chat_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "飞书群 chat_id 列表，整群拉入日程（比逐个拉人更准确高效）",
                },
                "attendee_ability": {
                    "type": "string",
                    "description": "参与人权限: none | can_see_others | can_invite_others | can_modify_event（任何人可修改日程）",
                    "default": "",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "日历 ID。不填则用主日历。",
                    "default": "",
                },
                "timezone": {
                    "type": "string",
                    "description": "时区（IANA 格式如 America/Los_Angeles）。修改跨时区活动时间时设置，不填则用用户默认时区。",
                    "default": "",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "remove_event_attendees",
        "description": "从飞书日程中移除参与人（踢人）。先用 get_event_detail 查看当前参与人的 open_id/chat_id，再传给此工具移除。",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "日程 ID",
                },
                "attendee_open_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要移除的用户 open_id 列表",
                },
                "attendee_emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要移除的外部邮箱列表",
                },
                "attendee_chat_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要移除的群 chat_id 列表",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "日历 ID。不填则用主日历。",
                    "default": "",
                },
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "删除飞书日历上的某个日程。需要 event_id，可先用 list_calendar_events 查到。",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "要删除的日程 ID",
                },
                "calendar_id": {
                    "type": "string",
                    "description": "日历 ID。不填则用主日历。",
                    "default": "",
                },
            },
            "required": ["event_id"],
        },
    },
]

TOOL_MAP = {
    "list_calendars": lambda args: list_calendars(),
    "create_calendar_event": lambda args: create_event(
        summary=args["summary"],
        start_time=args["start_time"],
        end_time=args.get("end_time", ""),
        description=args.get("description", ""),
        location=args.get("location", ""),
        attendee_open_ids=args.get("attendee_open_ids"),
        attendee_emails=args.get("attendee_emails"),
        attendee_chat_ids=args.get("attendee_chat_ids"),
        attendee_ability=args.get("attendee_ability", ""),
        calendar_id=args.get("calendar_id", ""),
        timezone=args.get("timezone", ""),
    ),
    "list_calendar_events": lambda args: list_events(
        start_date=args.get("start_date", ""),
        end_date=args.get("end_date", ""),
        max_results=args.get("max_results", 10),
        calendar_id=args.get("calendar_id", ""),
        keyword=args.get("keyword", ""),
    ),
    "get_event_detail": lambda args: get_event_detail(
        event_id=args["event_id"],
        calendar_id=args.get("calendar_id", ""),
    ),
    "update_calendar_event": lambda args: update_event(
        event_id=args["event_id"],
        summary=args.get("summary", ""),
        start_time=args.get("start_time", ""),
        end_time=args.get("end_time", ""),
        description=args.get("description", ""),
        location=args.get("location", ""),
        calendar_id=args.get("calendar_id", ""),
        attendee_open_ids=args.get("attendee_open_ids"),
        attendee_emails=args.get("attendee_emails"),
        attendee_chat_ids=args.get("attendee_chat_ids"),
        attendee_ability=args.get("attendee_ability", ""),
    ),
    "remove_event_attendees": lambda args: remove_event_attendees(
        event_id=args["event_id"],
        attendee_open_ids=args.get("attendee_open_ids"),
        attendee_emails=args.get("attendee_emails"),
        attendee_chat_ids=args.get("attendee_chat_ids"),
        calendar_id=args.get("calendar_id", ""),
    ),
    "delete_calendar_event": lambda args: delete_event(
        event_id=args["event_id"],
        calendar_id=args.get("calendar_id", ""),
    ),
}
