"""超级管理员身份系统

跨平台、跨租户的超级管理员身份识别。

Steven（吴天骄）在不同平台有不同 user_id：
- 飞书: ou_xxx (code-bot), ou_yyy (pm-bot)
- 微信客服: wmXXX (kf-steven-ai)
所有这些 ID 都映射到同一个超级管理员。

身份来源：
- Redis `superadmin:identities` — 精确的平台 ID 映射（通过 Admin Dashboard 管理）
- 不再支持按名字自动注册（微信昵称可伪造，安全风险高）

首次使用：在 Admin Dashboard 手动添加 identity（user_id + platform + tenant_id）。
获取 user_id：给 bot 发一条消息后从容器日志中查找 external_userid。

Redis 数据结构：
    superadmin:identities → JSON {
        "name": "吴天骄",
        "identities": [
            {"platform": "wecom_kf", "user_id": "wmXXX", "tenant_id": "kf-steven-ai"},
            {"platform": "feishu", "user_id": "ou_XXX", "tenant_id": "code-bot"},
        ]
    }

fail-closed 策略：Redis 不可用时，无法判定超管身份，返回 False。
"""

from __future__ import annotations

import json
import logging
import os

from app.services import redis_client as redis

logger = logging.getLogger(__name__)

_REDIS_KEY = "superadmin:identities"
_SUPER_ADMIN_NAME = os.getenv("SUPER_ADMIN_NAME", "Admin")

# 内存缓存（避免每次请求都查 Redis）
_cache: dict | None = None
_cache_ts: float = 0
_CACHE_TTL = 60  # 秒


def _load_config() -> dict:
    """从 Redis 加载超管配置，带缓存。"""
    global _cache, _cache_ts
    import time
    now = time.time()
    if _cache is not None and now - _cache_ts < _CACHE_TTL:
        return _cache

    if not redis.available():
        return {"name": _SUPER_ADMIN_NAME, "identities": []}

    try:
        raw = redis.execute("GET", _REDIS_KEY)
        if raw and isinstance(raw, str):
            _cache = json.loads(raw)
            _cache_ts = now
            return _cache
    except Exception:
        logger.debug("superadmin config load failed", exc_info=True)

    return {"name": _SUPER_ADMIN_NAME, "identities": []}


def _save_config(config: dict) -> bool:
    """保存超管配置到 Redis。"""
    global _cache, _cache_ts
    if not redis.available():
        return False
    try:
        redis.execute("SET", _REDIS_KEY, json.dumps(config, ensure_ascii=False))
        _cache = config
        _cache_ts = __import__("time").time()
        return True
    except Exception:
        logger.warning("superadmin config save failed", exc_info=True)
        return False


def invalidate_cache():
    """清除缓存（dashboard 修改后调用）。"""
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0


# ── 身份查询 ──


def is_super_admin(sender_id: str, sender_name: str = "", tenant_id: str = "") -> bool:
    """判断当前发送者是否为超级管理员。

    仅通过 Redis identities 中的精确 ID 匹配判定。
    不再支持按名字自动注册（微信昵称可伪造，安全风险高）。
    首次使用需在 Admin Dashboard 手动添加 identity。
    """
    if not sender_id:
        return False

    config = _load_config()

    # 精确 ID 匹配（唯一判定方式）
    for ident in config.get("identities", []):
        if ident.get("user_id") == sender_id:
            return True

    return False


# ── 通知目标 ──


def get_notification_targets() -> list[dict]:
    """获取所有可以联系到超管的平台入口。

    Returns:
        [{"platform": "feishu", "user_id": "ou_xxx", "tenant_id": "code-bot"}, ...]
    """
    config = _load_config()
    return list(config.get("identities", []))


async def notify_super_admin(message: str) -> bool:
    """主动通知超管（优先飞书，微信客服受 48h 窗口限制）。

    策略：
    1. 找到一个 feishu 平台的身份 → 用该租户的飞书 client 发消息
    2. 找不到飞书 → 尝试 wecom_kf（受 48h 限制，可能失败）
    3. 都没有 → 只存 Redis 等被动提醒

    Returns:
        True if notification sent, False otherwise
    """
    targets = get_notification_targets()
    if not targets:
        logger.info("notify_super_admin: no targets configured")
        return False

    # 优先飞书
    feishu_targets = [t for t in targets if t.get("platform") == "feishu"]
    for target in feishu_targets:
        try:
            tenant_id = target.get("tenant_id", "")
            user_id = target.get("user_id", "")
            if not tenant_id or not user_id:
                continue

            from app.tenant.registry import tenant_registry
            tenant = tenant_registry.get(tenant_id)
            if not tenant:
                continue

            from app.services.feishu import FeishuClient
            client = FeishuClient(tenant.app_id, tenant.app_secret)
            result = await client.send_to_chat(user_id, message)
            # send_to_chat uses chat_id type; for open_id we need direct API
            if result.get("code") == 0:
                logger.info("notify_super_admin: sent via feishu/%s to %s",
                            tenant_id, user_id[:16])
                return True
            # open_id needs receive_id_type=open_id, fallback to direct API
            import httpx
            token = await client._get_token()
            async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
                resp = await http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    json={
                        "receive_id": user_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": message}),
                    },
                    headers={"Authorization": f"Bearer {token}"},
                    params={"receive_id_type": "open_id"},
                )
                r = resp.json()
                if r.get("code") == 0:
                    logger.info("notify_super_admin: sent via feishu/%s (open_id) to %s",
                                tenant_id, user_id[:16])
                    return True
                logger.warning("notify_super_admin: feishu send failed: %s", r)
        except Exception:
            logger.debug("notify_super_admin: feishu attempt failed", exc_info=True)

    # 尝试微信客服（48h 窗口限制）
    kf_targets = [t for t in targets if t.get("platform") == "wecom_kf"]
    for target in kf_targets:
        try:
            tenant_id = target.get("tenant_id", "")
            user_id = target.get("user_id", "")
            if not tenant_id or not user_id:
                continue

            from app.tenant.registry import tenant_registry
            from app.tenant.context import set_current_tenant
            tenant = tenant_registry.get(tenant_id)
            if not tenant:
                continue

            set_current_tenant(tenant)
            from app.services.wecom_kf import wecom_kf_client
            result = await wecom_kf_client.send_text(user_id, message)
            if result.get("errcode", -1) == 0:
                logger.info("notify_super_admin: sent via wecom_kf/%s to %s",
                            tenant_id, user_id[:16])
                return True
            logger.info("notify_super_admin: wecom_kf failed (probably 48h window): %s",
                        result.get("errmsg", ""))
        except Exception:
            logger.debug("notify_super_admin: wecom_kf attempt failed", exc_info=True)

    logger.info("notify_super_admin: all channels failed, will rely on passive reminder")
    return False


# ── Dashboard 管理 API ──


def get_config() -> dict:
    """获取完整超管配置（供 dashboard 展示）。"""
    config = _load_config()
    # 确保有默认结构
    config.setdefault("name", _SUPER_ADMIN_NAME)
    config.setdefault("identities", [])
    return config


def set_config(name: str, identities: list[dict]) -> bool:
    """设置超管配置（供 dashboard 修改）。"""
    config = {
        "name": name,
        "identities": [
            {
                "platform": i.get("platform", ""),
                "user_id": i.get("user_id", ""),
                "tenant_id": i.get("tenant_id", ""),
                "label": i.get("label", ""),
            }
            for i in identities
            if i.get("user_id")
        ],
    }
    ok = _save_config(config)
    if ok:
        invalidate_cache()
    return ok


def add_identity(platform: str, user_id: str, tenant_id: str = "",
                 label: str = "") -> bool:
    """添加一个平台身份。"""
    if not user_id:
        return False
    config = _load_config()
    # 去重
    for ident in config.get("identities", []):
        if ident.get("user_id") == user_id:
            return True  # 已存在
    config.setdefault("identities", []).append({
        "platform": platform,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "label": label,
    })
    return _save_config(config)


def remove_identity(user_id: str) -> bool:
    """移除一个平台身份。"""
    config = _load_config()
    before = len(config.get("identities", []))
    config["identities"] = [
        i for i in config.get("identities", [])
        if i.get("user_id") != user_id
    ]
    if len(config["identities"]) < before:
        return _save_config(config)
    return False
