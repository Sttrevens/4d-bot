"""试用期管理 + 每用户 6 小时 token 限额

功能：
1. 试用期：新用户首次消息自动注册，trial_duration_hours 小时后过期
2. 过期后需管理员手动 approve 才能继续使用
3. approved 有 approval_duration_days 有效期，到期自动回到 expired
4. 每用户每 6 小时 token 限额（滑动窗口）

Redis 数据结构：
- trial:{tenant_id}:user:{user_id} → HASH
    first_seen:      ISO 时间戳（首次消息时间）
    status:          trial | expired | approved | blocked
    message_count:   总消息数
    last_active:     最近活跃 ISO 时间戳
    display_name:    用户显示名（微信昵称/飞书姓名）
    approved_at:     审批时间（approved 才有）
    approved_until:  审批到期时间（approved 才有，空=永久）
    approved_by:     审批人（admin 标识）
    notes:           管理员备注

- tquota:6h:{tenant_id}:{user_id} → ZSET (滑动窗口，score=timestamp, member=ts:tokens:uuid)

fail-open：Redis 不可用时放行。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.services import redis_client as redis

logger = logging.getLogger(__name__)

_6H_SECONDS = 6 * 3600


def _user_key(tenant_id: str, user_id: str) -> str:
    return f"trial:{tenant_id}:user:{user_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 试用期检查 ──

def check_trial(tenant_id: str, user_id: str, duration_hours: int = 48,
                display_name: str = "") -> tuple[bool, str]:
    """检查用户试用状态。首次见到的用户自动注册。

    Args:
        display_name: 用户显示名（微信昵称/飞书姓名），存入 Redis 供 dashboard 展示

    Returns:
        (allowed, reason): allowed=True 表示放行
    """
    if not redis.available() or not tenant_id or not user_id:
        return True, ""  # fail-open

    key = _user_key(tenant_id, user_id)

    try:
        data = redis.execute("HGETALL", key)

        if not data or not isinstance(data, list) or len(data) < 2:
            # 新用户 → 自动注册 trial
            _register_user(key, tenant_id, user_id, display_name)
            return True, "trial_new"

        info = _parse_hash(data)
        status = info.get("status", "trial")

        if status == "approved":
            # 检查审批是否过期
            approved_until = info.get("approved_until", "")
            if approved_until and _is_approval_expired(approved_until):
                redis.pipeline([
                    ["HSET", key, "status", "expired"],
                    ["EXPIRE", key, str(90 * 86400)],
                ])
                return False, (
                    "您的使用授权已到期，请联系管理员重新审批。"
                )
            # 已审批且未过期，更新活跃时间
            _touch(key, display_name)
            return True, "approved"

        if status == "blocked":
            return False, "您的账号已被暂停使用，如有疑问请联系管理员。"

        # trial 状态 → 检查是否过期
        first_seen = info.get("first_seen", "")
        if first_seen and _is_expired(first_seen, duration_hours):
            # 过期 → 更新状态
            redis.pipeline([
                ["HSET", key, "status", "expired"],
                ["EXPIRE", key, str(90 * 86400)],
            ])
            return False, (
                f"您的 {duration_hours} 小时免费试用已结束。"
                "如需继续使用，请联系管理员开通正式账号。"
            )

        # 仍在试用期
        _touch(key, display_name)
        return True, "trial_active"

    except Exception:
        logger.warning("trial check failed", exc_info=True)
        return True, ""  # fail-open


def _register_user(key: str, tenant_id: str, user_id: str, display_name: str = "") -> None:
    """注册新试用用户"""
    now = _now_iso()
    cmds = [
        ["HSET", key, "first_seen", now],
        ["HSET", key, "status", "trial"],
        ["HSET", key, "message_count", "0"],
        ["HSET", key, "last_active", now],
        # 保留 90 天（即使过期也保留记录，方便 dashboard 查看）
        ["EXPIRE", key, str(90 * 86400)],
    ]
    if display_name:
        cmds.insert(0, ["HSET", key, "display_name", display_name])
    redis.pipeline(cmds)
    logger.info("trial: new user registered tenant=%s user=%s name=%s",
                tenant_id, user_id[:16], display_name or "(unknown)")


def _touch(key: str, display_name: str = "") -> None:
    """更新活跃时间 + 消息计数 + 刷新显示名"""
    cmds = [
        ["HSET", key, "last_active", _now_iso()],
        ["HINCRBY", key, "message_count", "1"],
    ]
    # 每次活跃时刷新名字（用户可能改名）
    if display_name:
        cmds.append(["HSET", key, "display_name", display_name])
    cmds.append(["EXPIRE", key, str(90 * 86400)])
    redis.pipeline(cmds)


def _is_expired(first_seen_iso: str, duration_hours: int) -> bool:
    """检查试用是否过期"""
    try:
        first_seen = datetime.fromisoformat(first_seen_iso)
        elapsed_hours = (datetime.now(timezone.utc) - first_seen).total_seconds() / 3600
        return elapsed_hours >= duration_hours
    except (ValueError, TypeError):
        return False


def _is_approval_expired(approved_until_iso: str) -> bool:
    """检查审批是否过期"""
    try:
        until = datetime.fromisoformat(approved_until_iso)
        return datetime.now(timezone.utc) >= until
    except (ValueError, TypeError):
        return False


# ── 每用户 6 小时 token 限额 ──

def _token_quota_key(tenant_id: str, user_id: str) -> str:
    return f"tquota:6h:{tenant_id}:{user_id}"


def check_user_token_quota(
    tenant_id: str,
    user_id: str,
    limit: int,
) -> tuple[bool, str]:
    """检查用户 6 小时内 token 用量是否超限（pre-check）。

    Args:
        limit: 6 小时内最大 token 数（input + output），0 = 不限

    Returns:
        (allowed, reason)
    """
    if limit <= 0 or not redis.available() or not tenant_id or not user_id:
        return True, ""

    now = time.time()
    window_start = now - _6H_SECONDS
    key = _token_quota_key(tenant_id, user_id)

    try:
        # 清理过期条目 + 读取当前窗口内所有成员
        redis.execute("ZREMRANGEBYSCORE", key, "-inf", str(window_start))
        members = redis.execute("ZRANGE", key, "0", "-1")

        if not members or not isinstance(members, list):
            return True, ""

        # 成员格式: "{timestamp}:{tokens}:{uuid}" — 解析并求和
        total_tokens = 0
        for m in members:
            if isinstance(m, str):
                parts = m.split(":")
                if len(parts) >= 2:
                    try:
                        total_tokens += int(parts[1])
                    except (ValueError, TypeError):
                        pass

        if total_tokens >= limit:
            return False, (
                f"您已达到每 6 小时 {_format_tokens(limit)} token 的使用限额，"
                "请稍后再试。"
            )

        return True, ""

    except Exception:
        logger.warning("token quota check failed", exc_info=True)
        return True, ""  # fail-open


def record_user_tokens(
    tenant_id: str,
    user_id: str,
    tokens: int,
) -> None:
    """LLM 调用完成后记录 token 用量到滑动窗口（post-record）。"""
    if tokens <= 0 or not redis.available() or not tenant_id or not user_id:
        return

    now = time.time()
    key = _token_quota_key(tenant_id, user_id)
    member = f"{now:.6f}:{tokens}:{uuid.uuid4().hex[:8]}"

    try:
        redis.pipeline([
            ["ZADD", key, str(now), member],
            ["EXPIRE", key, str(_6H_SECONDS + 60)],
        ])
    except Exception:
        logger.warning("record_user_tokens failed", exc_info=True)


def _format_tokens(n: int) -> str:
    """格式化 token 数为可读字符串"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


# ── 管理操作（供 admin API 调用）──

def approve_user(tenant_id: str, user_id: str, approved_by: str = "admin",
                 duration_days: int = 0) -> bool:
    """审批用户，允许继续使用。

    Args:
        duration_days: 审批有效期（天），0=永久
    """
    if not redis.available():
        return False
    key = _user_key(tenant_id, user_id)
    try:
        now = _now_iso()
        cmds = [
            ["HSET", key, "status", "approved"],
            ["HSET", key, "approved_at", now],
            ["HSET", key, "approved_by", approved_by],
        ]
        if duration_days > 0:
            until = (datetime.now(timezone.utc) + timedelta(days=duration_days)).isoformat()
            cmds.append(["HSET", key, "approved_until", until])
        else:
            # 永久：清除之前可能存在的到期时间
            cmds.append(["HDEL", key, "approved_until"])
        cmds.append(["EXPIRE", key, str(90 * 86400)])
        redis.pipeline(cmds)
        logger.info("trial: approved user=%s tenant=%s by=%s days=%s",
                    user_id[:16], tenant_id, approved_by, duration_days or "permanent")
        return True
    except Exception:
        logger.warning("trial: approve failed", exc_info=True)
        return False


def block_user(tenant_id: str, user_id: str) -> bool:
    """封禁用户"""
    if not redis.available():
        return False
    key = _user_key(tenant_id, user_id)
    try:
        redis.pipeline([
            ["HSET", key, "status", "blocked"],
            ["EXPIRE", key, str(90 * 86400)],
        ])
        logger.info("trial: blocked user=%s tenant=%s", user_id[:16], tenant_id)
        return True
    except Exception:
        logger.warning("trial: block failed", exc_info=True)
        return False


def reset_user(tenant_id: str, user_id: str) -> bool:
    """重置用户为新试用状态"""
    if not redis.available():
        return False
    key = _user_key(tenant_id, user_id)
    try:
        redis.execute("DEL", key)
        logger.info("trial: reset user=%s tenant=%s", user_id[:16], tenant_id)
        return True
    except Exception:
        return False


def set_user_notes(tenant_id: str, user_id: str, notes: str) -> bool:
    """设置用户备注"""
    if not redis.available():
        return False
    key = _user_key(tenant_id, user_id)
    try:
        redis.pipeline([
            ["HSET", key, "notes", notes],
            ["EXPIRE", key, str(90 * 86400)],
        ])
        return True
    except Exception:
        return False


def get_user_info(tenant_id: str, user_id: str) -> dict:
    """获取单个用户的试用信息"""
    if not redis.available():
        return {}
    key = _user_key(tenant_id, user_id)
    try:
        data = redis.execute("HGETALL", key)
        if not data or not isinstance(data, list):
            return {}
        info = _parse_hash(data)
        info["user_id"] = user_id
        info["tenant_id"] = tenant_id
        return info
    except Exception:
        return {}


def list_trial_users(tenant_id: str) -> list[dict]:
    """列出租户的所有试用用户（SCAN + pipeline 批量查询）

    注意：Upstash REST API 的 SCAN 返回格式是 [cursor, [key1, key2, ...]]
    """
    if not redis.available():
        return []

    prefix = f"trial:{tenant_id}:user:"
    all_keys = []
    cursor = "0"

    try:
        # Phase 1: SCAN 收集所有 key
        for _ in range(100):
            result = redis.execute("SCAN", cursor, "MATCH", f"{prefix}*", "COUNT", "50")
            if not result or not isinstance(result, list) or len(result) < 2:
                break
            cursor = str(result[0])
            keys = result[1] if isinstance(result[1], list) else []
            for key in keys:
                if isinstance(key, str) and key.startswith(prefix):
                    all_keys.append(key)
            if cursor == "0":
                break

        if not all_keys:
            return []

        # Phase 2: pipeline 批量 HGETALL（1 次 HTTP 代替 N 次）
        commands = [["HGETALL", key] for key in all_keys]
        responses = redis.pipeline(commands)

        users = []
        for key, data in zip(all_keys, responses):
            user_id = key[len(prefix):]
            if not user_id:
                continue
            if data and isinstance(data, list) and len(data) >= 2:
                info = _parse_hash(data)
                info["user_id"] = user_id
                info["tenant_id"] = tenant_id
                users.append(info)

        # 按 last_active 降序排列
        users.sort(key=lambda u: u.get("last_active", ""), reverse=True)
        return users

    except Exception:
        logger.warning("list_trial_users failed", exc_info=True)
        return []


# ── 内部工具 ──

def _parse_hash(data: list) -> dict:
    """将 Redis HGETALL 返回的 [k1, v1, k2, v2, ...] 转为 dict"""
    result = {}
    for i in range(0, len(data) - 1, 2):
        result[data[i]] = data[i + 1]
    return result
