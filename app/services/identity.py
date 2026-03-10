"""跨平台用户身份关联系统

实现 bot 级别的统一用户身份：同一个人在飞书、企微、微信客服等多平台的 ID
可以关联到同一个 identity，共享记忆和对话上下文。

关联方式:
1. 智能关联 — bot 在记忆中搜索名字匹配，通过已知 channel 主动发验证码确认
2. 手动绑定 — admin dashboard 直接关联
3. 用户自助 — 用户在一个平台说 "绑定"，bot 生成验证码，用户到另一个平台输入

Redis 结构:
  identity:{uuid}                    → HASH {name, email, phone, created_at, notes}
  identity_link:{platform}:{pid}     → identity_uuid  (反向索引：平台用户ID → identity)
  identity_verify:{code}             → HASH {identity_id, from_platform, from_pid,
                                              target_platform, target_pid, created_at}
                                              TTL 600 秒
"""

from __future__ import annotations

import json
import logging
import uuid
import random
import string
from datetime import datetime, timezone
from typing import Optional

from app.services import redis_client as redis

logger = logging.getLogger(__name__)


# ── Redis key builders ──

def _identity_key(identity_id: str) -> str:
    return f"identity:{identity_id}"


def _link_key(platform: str, platform_user_id: str) -> str:
    return f"identity_link:{platform}:{platform_user_id}"


def _verify_key(code: str) -> str:
    return f"identity_verify:{code}"


def _bot_links_key(bot_id: str, identity_id: str) -> str:
    """Per-bot identity links（记录这个 identity 在各平台的 ID）"""
    return f"identity_bot:{bot_id}:{identity_id}"


# ── 核心操作 ──

def create_identity(
    name: str,
    platform: str,
    platform_user_id: str,
    bot_id: str = "",
    email: str = "",
    phone: str = "",
    notes: str = "",
) -> Optional[str]:
    """创建新的统一身份并关联第一个平台 ID。

    Returns: identity_id (uuid) 或 None（Redis 不可用）
    """
    if not redis.available():
        return None

    # 检查是否已存在关联
    existing = find_identity(platform, platform_user_id)
    if existing:
        logger.info("identity: already linked %s:%s → %s", platform, platform_user_id[:12], existing)
        return existing

    identity_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        cmds = [
            ["HSET", _identity_key(identity_id),
             "name", name,
             "email", email,
             "phone", phone,
             "created_at", now,
             "notes", notes],
            # 反向索引
            ["SET", _link_key(platform, platform_user_id), identity_id],
        ]
        # Per-bot links
        if bot_id:
            cmds.append(["HSET", _bot_links_key(bot_id, identity_id), platform, platform_user_id])

        redis.pipeline(cmds)
        logger.info("identity: created %s name=%s %s:%s",
                     identity_id[:8], name, platform, platform_user_id[:12])
        return identity_id
    except Exception:
        logger.warning("identity: create failed", exc_info=True)
        return None


def link_identity(
    identity_id: str,
    platform: str,
    platform_user_id: str,
    bot_id: str = "",
    verified: bool = True,
) -> bool:
    """给已有 identity 添加一个新平台链接。"""
    if not redis.available():
        return False

    try:
        cmds = [
            ["SET", _link_key(platform, platform_user_id), identity_id],
        ]
        if bot_id:
            cmds.append(["HSET", _bot_links_key(bot_id, identity_id), platform, platform_user_id])
        # 更新 identity 元数据
        now = datetime.now(timezone.utc).isoformat()
        cmds.append(["HSET", _identity_key(identity_id),
                      f"linked_{platform}", platform_user_id,
                      "last_linked_at", now])
        redis.pipeline(cmds)
        logger.info("identity: linked %s → %s:%s verified=%s",
                     identity_id[:8], platform, platform_user_id[:12], verified)
        return True
    except Exception:
        logger.warning("identity: link failed", exc_info=True)
        return False


def find_identity(platform: str, platform_user_id: str) -> Optional[str]:
    """通过平台用户 ID 查找统一身份。返回 identity_id 或 None。"""
    if not redis.available():
        return None
    try:
        result = redis.execute("GET", _link_key(platform, platform_user_id))
        return result if result else None
    except Exception:
        return None


def get_identity(identity_id: str) -> Optional[dict]:
    """获取身份详情。"""
    if not redis.available():
        return None
    try:
        data = redis.execute("HGETALL", _identity_key(identity_id))
        if not data:
            return None
        # HGETALL 返回 flat list [k1, v1, k2, v2, ...]
        if isinstance(data, list):
            it = iter(data)
            result = dict(zip(it, it))
        else:
            result = data
        result["identity_id"] = identity_id
        return result
    except Exception:
        return None


def get_linked_platforms(identity_id: str, bot_id: str = "") -> dict:
    """获取一个 identity 在各平台的用户 ID。

    Returns: {platform: platform_user_id, ...}
    """
    if not redis.available():
        return {}
    try:
        if bot_id:
            data = redis.execute("HGETALL", _bot_links_key(bot_id, identity_id))
        else:
            # 从 identity 元数据中提取 linked_* 字段
            data = redis.execute("HGETALL", _identity_key(identity_id))

        if not data:
            return {}
        if isinstance(data, list):
            it = iter(data)
            pairs = dict(zip(it, it))
        else:
            pairs = data

        if bot_id:
            return pairs  # {platform: user_id}
        else:
            # 从 identity hash 提取 linked_feishu, linked_wecom 等
            return {k.replace("linked_", ""): v for k, v in pairs.items() if k.startswith("linked_")}
    except Exception:
        return {}


def resolve_sender(
    bot_id: str,
    platform: str,
    sender_id: str,
) -> tuple[Optional[str], dict]:
    """解析发送者的统一身份。

    Returns: (identity_id, linked_platforms_dict) 或 (None, {})
    """
    identity_id = find_identity(platform, sender_id)
    if not identity_id:
        return None, {}
    linked = get_linked_platforms(identity_id, bot_id)
    return identity_id, linked


# ── 验证码系统 ──

def _generate_code(length: int = 8) -> str:
    """生成字母数字验证码（8 位，36^8 ≈ 28 亿种组合，防暴力破解）。"""
    chars = string.digits + string.ascii_uppercase
    return "".join(random.choices(chars, k=length))


def initiate_verification(
    identity_id: str,
    from_platform: str,
    from_pid: str,
    target_platform: str,
    target_pid: str,
) -> Optional[str]:
    """发起跨平台身份验证。

    生成验证码，存入 Redis（TTL 600秒）。
    调用方负责通过 target_platform 的 channel 发送验证码给 target_pid。

    Returns: 验证码字符串，或 None
    """
    if not redis.available():
        return None

    code = _generate_code()
    now = datetime.now(timezone.utc).isoformat()

    try:
        key = _verify_key(code)
        data = json.dumps({
            "identity_id": identity_id,
            "from_platform": from_platform,
            "from_pid": from_pid,
            "target_platform": target_platform,
            "target_pid": target_pid,
            "created_at": now,
        })
        # SET with 600s TTL
        redis.pipeline([
            ["SET", key, data],
            ["EXPIRE", key, "600"],
        ])
        logger.info("identity: verify code=%s for %s:%s → %s:%s",
                     code, from_platform, from_pid[:12], target_platform, target_pid[:12])
        return code
    except Exception:
        logger.warning("identity: initiate_verification failed", exc_info=True)
        return None


def verify_code(
    code: str,
    sender_platform: str,
    sender_id: str,
    bot_id: str = "",
) -> Optional[dict]:
    """验证码确认。

    验证成功后自动创建跨平台链接。

    Returns: {"ok": True, "identity_id": ..., "linked": {...}} 或 None
    """
    if not redis.available():
        return None

    try:
        # ── 防暴力破解：检查错误次数 ──
        attempt_key = f"identity_verify_attempts:{sender_platform}:{sender_id}"
        attempts_raw = redis.execute("GET", attempt_key)
        attempts = int(attempts_raw) if attempts_raw else 0
        if attempts >= 5:
            logger.warning("identity: verify brute-force blocked %s:%s (%d attempts)",
                           sender_platform, sender_id[:12], attempts)
            return None

        key = _verify_key(code)
        # 原子 GET+DELETE：避免 TOCTOU 竞争（并发请求重复使用同一验证码）
        raw = redis.execute("GETDEL", key)
        if not raw:
            # 验证码不存在或已被消费 → 记录失败次数
            redis.pipeline([
                ["INCR", attempt_key],
                ["EXPIRE", attempt_key, "600"],  # 10 分钟后重置
            ])
            return None

        data = json.loads(raw)

        identity_id = data["identity_id"]

        # 链接当前发送者
        link_identity(identity_id, sender_platform, sender_id, bot_id=bot_id)

        # 验证成功，清除失败计数
        redis.execute("DEL", attempt_key)

        linked = get_linked_platforms(identity_id, bot_id)
        logger.info("identity: verified code=%s → linked %s:%s to %s",
                     code, sender_platform, sender_id[:12], identity_id[:8])
        return {"ok": True, "identity_id": identity_id, "linked": linked}
    except Exception:
        logger.warning("identity: verify_code failed", exc_info=True)
        return None


# ── 智能匹配（供 LLM agent 调用）──

def search_identity_by_name(
    name: str,
    bot_id: str = "",
    limit: int = 5,
) -> list[dict]:
    """按名字搜索已有 identity（用于 bot 智能关联）。

    扫描 identity:* keys 匹配名字。实际生产中可能需要索引优化。
    简单实现：用 SCAN 遍历。
    """
    if not redis.available():
        return []

    results = []
    try:
        # 使用 SCAN 搜索（小规模场景可行，大规模需要索引）
        cursor = "0"
        name_lower = name.lower()
        while True:
            scan_result = redis.execute("SCAN", cursor, "MATCH", "identity:*", "COUNT", "100")
            if not scan_result or not isinstance(scan_result, list) or len(scan_result) < 2:
                break
            cursor = scan_result[0]
            keys = scan_result[1] if isinstance(scan_result[1], list) else []
            for key in keys:
                if not isinstance(key, str) or not key.startswith("identity:"):
                    continue
                iid = key.replace("identity:", "")
                info = get_identity(iid)
                if info and name_lower in (info.get("name", "") or "").lower():
                    if bot_id:
                        info["linked_platforms"] = get_linked_platforms(iid, bot_id)
                    results.append(info)
                    if len(results) >= limit:
                        return results
            if cursor == "0":
                break
    except Exception:
        logger.warning("identity: search_by_name failed", exc_info=True)
    return results


# ── Admin 操作 ──

def unlink_identity(platform: str, platform_user_id: str, bot_id: str = "") -> bool:
    """解除平台用户的身份关联。"""
    if not redis.available():
        return False
    try:
        identity_id = find_identity(platform, platform_user_id)
        if not identity_id:
            return False
        cmds = [
            ["DEL", _link_key(platform, platform_user_id)],
            ["HDEL", _identity_key(identity_id), f"linked_{platform}"],
        ]
        if bot_id:
            cmds.append(["HDEL", _bot_links_key(bot_id, identity_id), platform])
        redis.pipeline(cmds)
        logger.info("identity: unlinked %s:%s from %s", platform, platform_user_id[:12], identity_id[:8])
        return True
    except Exception:
        return False


def list_identities(bot_id: str = "", limit: int = 50) -> list[dict]:
    """列出所有 identity（admin 用）。"""
    if not redis.available():
        return []
    results = []
    try:
        cursor = "0"
        while True:
            scan_result = redis.execute("SCAN", cursor, "MATCH", "identity:*", "COUNT", "100")
            if not scan_result or not isinstance(scan_result, list) or len(scan_result) < 2:
                break
            cursor = scan_result[0]
            keys = scan_result[1] if isinstance(scan_result[1], list) else []
            for key in keys:
                if not isinstance(key, str) or not key.startswith("identity:"):
                    continue
                # 排除 identity_link:* 和 identity_bot:* 等子 key
                parts = key.split(":")
                if len(parts) != 2:
                    continue
                iid = parts[1]
                info = get_identity(iid)
                if info:
                    if bot_id:
                        info["linked_platforms"] = get_linked_platforms(iid, bot_id)
                    results.append(info)
                    if len(results) >= limit:
                        return results
            if cursor == "0":
                break
    except Exception:
        logger.warning("identity: list failed", exc_info=True)
    return results
