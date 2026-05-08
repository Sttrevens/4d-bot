"""跨容器租户配置同步 — 基于 Redis 持久化 + 消息队列。

架构：
- 每个 dashboard 添加/编辑的租户 → 持久化到 Redis key `tenant_cfg:{tid}`
- 同时发消息到 `tenant_sync:queue` → 各容器实时轮询 hot-load
- 容器启动时 → `load_persisted_tenants()` 从 Redis 加载所有 `tenant_cfg:*`
- 容器内 /app/tenants.json 是只读挂载（:ro），不依赖文件写入

Redis 数据结构：
  tenant_cfg:{tenant_id}      → JSON 完整租户配置（持久化，无 TTL）
  tenant_sync:queue            → LIST 实时通知消息（LTRIM 保留最近 100 条）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from app.services import redis_client as redis

logger = logging.getLogger(__name__)

_QUEUE_KEY = "tenant_sync:queue"
_CFG_PREFIX = "tenant_cfg:"
_ADMIN_PREFIX = "admin:tenant:"
_MAX_QUEUE_LEN = 100
_POLL_INTERVAL = 60  # seconds (was 15→60; saves ~4320 cmds/day per container on Upstash free tier)

# 每个容器维护自己的 last_processed_ts
_last_processed_ts: float = 0.0


def _scan_keys(prefix: str) -> list[str]:
    """Return Redis keys for a prefix using SCAN, bounded for Upstash safety."""
    keys: list[str] = []
    cursor = "0"
    for _ in range(50):
        result = redis.execute("SCAN", cursor, "MATCH", f"{prefix}*", "COUNT", "50")
        if not result or not isinstance(result, list) or len(result) < 2:
            break
        cursor = str(result[0])
        batch = result[1] if isinstance(result[1], list) else []
        keys.extend(k for k in batch if isinstance(k, str))
        if cursor == "0":
            break
    return keys


def _load_json_key(key: str) -> dict:
    raw = redis.execute("GET", key)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge_channel_overlay(tenant, channels_raw: object) -> bool:
    """Merge persisted channel overlays into a local tenants.json tenant."""
    if not isinstance(channels_raw, list) or not channels_raw:
        return False

    from dataclasses import asdict
    from app.tenant.config import ChannelConfig

    ch_fields = {f.name for f in ChannelConfig.__dataclass_fields__.values()}

    def coerce(item) -> ChannelConfig | None:
        if isinstance(item, ChannelConfig):
            ch = item
        elif isinstance(item, dict):
            filtered = {k: v for k, v in item.items() if k in ch_fields}
            if not filtered.get("platform"):
                return None
            if not filtered.get("channel_id"):
                filtered["channel_id"] = f"{tenant.tenant_id}-{filtered['platform']}"
            ch = ChannelConfig(**filtered)
        else:
            return None
        if not ch.channel_id:
            ch.channel_id = f"{tenant.tenant_id}-{ch.platform}"
        return ch

    base_channels = list(getattr(tenant, "channels", []) or [])
    if not base_channels:
        base_channels = [tenant._build_primary_channel()]

    before = [asdict(ch) for ch in base_channels]
    by_id = {ch.channel_id: ch for ch in base_channels}
    order = [ch.channel_id for ch in base_channels]

    for raw in channels_raw:
        ch = coerce(raw)
        if not ch:
            continue
        if ch.channel_id not in by_id:
            order.append(ch.channel_id)
        by_id[ch.channel_id] = ch

    merged = [by_id[channel_id] for channel_id in order if channel_id in by_id]
    after = [asdict(ch) for ch in merged]
    if before == after:
        return False

    tenant.channels = merged
    return True


def hydrate_persisted_channels_for_tenant(tenant) -> bool:
    """Load persisted channel overlays for an existing local tenant."""
    if not redis.available() or not tenant or not getattr(tenant, "tenant_id", ""):
        return False

    tid = tenant.tenant_id
    merged = False
    for key in (f"{_CFG_PREFIX}{tid}", f"{_ADMIN_PREFIX}{tid}"):
        config = _load_json_key(key)
        if config and _merge_channel_overlay(tenant, config.get("channels")):
            merged = True
            logger.info("tenant_sync: merged persisted channels for local tenant %s from %s", tid, key)
    return merged


# =====================================================================
#  发布（admin 侧调用）
# =====================================================================

def publish_tenant_update(action: str, tenant_config: dict) -> bool:
    """发布租户配置变更：持久化到 Redis + 发实时通知。

    Args:
        action: "add" | "update" | "remove"
        tenant_config: 完整租户配置 dict（remove 时只需 tenant_id）

    Returns:
        True if published successfully
    """
    if not redis.available():
        logger.warning("tenant_sync: Redis not available, cannot publish")
        return False

    tid = tenant_config.get("tenant_id", "")
    if not tid:
        return False

    try:
        # 1) 持久化完整配置到独立 key（重启后仍在）
        if action == "add":
            # 新租户：总是写入
            redis.execute("SET", f"{_CFG_PREFIX}{tid}",
                          json.dumps(tenant_config, ensure_ascii=False))
        elif action == "update":
            # 编辑：只更新已存在的 tenant_cfg 条目（dashboard 添加的租户）
            # 不为 tenants.json 原生租户创建 tenant_cfg（避免泄露 ${VAR} 解析后的密钥）
            existing = redis.execute("GET", f"{_CFG_PREFIX}{tid}")
            if existing:
                try:
                    base = json.loads(existing)
                    base.update(tenant_config)
                    redis.execute("SET", f"{_CFG_PREFIX}{tid}",
                                  json.dumps(base, ensure_ascii=False))
                except (json.JSONDecodeError, TypeError):
                    redis.execute("SET", f"{_CFG_PREFIX}{tid}",
                                  json.dumps(tenant_config, ensure_ascii=False))
        elif action == "remove":
            redis.execute("DEL", f"{_CFG_PREFIX}{tid}")

        # 2) 发实时通知到队列（在线容器 5 秒内 hot-load）
        # update 动作只发可编辑字段到队列，避免 ${VAR} 解析值泄露到 Redis
        queue_config = tenant_config
        if action == "update" and "app_secret" in tenant_config:
            # 有密钥字段 → 来自 asdict()，只保留安全字段
            _SAFE_QUEUE_FIELDS = {
                "tenant_id", "name", "platform", "llm_system_prompt", "custom_persona",
                "trial_enabled", "trial_duration_hours", "approval_duration_days",
                "quota_user_tokens_6h", "quota_monthly_api_calls", "quota_monthly_tokens",
                "rate_limit_rpm", "rate_limit_user_rpm", "deploy_free_quota",
                "memory_diary_enabled", "memory_context_enabled", "memory_journal_max",
                "memory_chat_rounds", "memory_chat_ttl", "admin_names",
                "tools_enabled", "capability_modules",
                "self_iteration_enabled", "instance_management_enabled",
                "wecom_kf_open_kfid", "channels",
            }
            queue_config = {k: v for k, v in tenant_config.items() if k in _SAFE_QUEUE_FIELDS}
        msg = json.dumps({
            "action": action,
            "tenant_config": queue_config,
            "source_port": int(os.environ.get("PORT", "8000")),
            "ts": time.time(),
        }, ensure_ascii=False)
        redis.execute("RPUSH", _QUEUE_KEY, msg)
        redis.execute("LTRIM", _QUEUE_KEY, -_MAX_QUEUE_LEN, -1)

        logger.info("tenant_sync: published %s for %s (persisted + queued)", action, tid)
        return True
    except Exception:
        logger.warning("tenant_sync: publish failed", exc_info=True)
        return False


# =====================================================================
#  启动时加载（每个容器 startup 调用）
# =====================================================================

def load_persisted_tenants() -> int:
    """从 Redis 加载所有 dashboard 添加的租户配置到本地 registry。

    在 main.py startup 中调用，确保重启后 dashboard 添加的租户不丢失。

    Returns:
        成功加载的租户数量
    """
    if not redis.available():
        return 0

    from app.tenant.registry import tenant_registry

    loaded = 0
    try:
        for key in _scan_keys(_CFG_PREFIX):
            tid = key.replace(_CFG_PREFIX, "", 1)
            if not tid:
                continue

            config = _load_json_key(key)
            if not config:
                continue

            existing = tenant_registry.get(tid)
            if existing:
                if _merge_channel_overlay(existing, config.get("channels")):
                    loaded += 1
                    logger.info("tenant_sync: merged persisted channels for local tenant %s", tid)
                continue

            if config.get("_overlay_only"):
                continue

            try:
                tenant_registry.register_from_dict(config)
                loaded += 1
                logger.info("tenant_sync: loaded persisted tenant %s from Redis", tid)
            except Exception as e:
                logger.warning("tenant_sync: failed to load %s: %s", tid, e)

        for key in _scan_keys(_ADMIN_PREFIX):
            tid = key.replace(_ADMIN_PREFIX, "", 1)
            tenant = tenant_registry.get(tid)
            if not tenant:
                continue
            config = _load_json_key(key)
            if config and _merge_channel_overlay(tenant, config.get("channels")):
                loaded += 1
                logger.info("tenant_sync: merged admin channel overlay for local tenant %s", tid)
    except Exception:
        logger.warning("tenant_sync: load_persisted_tenants failed", exc_info=True)

    return loaded


# =====================================================================
#  实时轮询（后台任务）
# =====================================================================

def _process_message(msg_str: str) -> bool:
    """处理单条同步消息。返回 True 表示处理成功。"""
    global _last_processed_ts

    try:
        msg = json.loads(msg_str)
    except (json.JSONDecodeError, TypeError):
        return False

    ts = msg.get("ts", 0)
    if ts <= _last_processed_ts:
        return False  # 已处理过

    # 跳过自己发的消息
    my_port = int(os.environ.get("PORT", "8000"))
    if msg.get("source_port") == my_port:
        _last_processed_ts = ts
        return False

    action = msg.get("action", "")
    tenant_config = msg.get("tenant_config", {})
    tid = tenant_config.get("tenant_id", "")

    if not tid:
        _last_processed_ts = ts
        return False

    from app.tenant.registry import tenant_registry

    if action == "add":
        try:
            tenant_registry.register_from_dict(tenant_config)
            logger.info("tenant_sync: add tenant %s in registry", tid)
        except Exception as e:
            logger.warning("tenant_sync: register %s failed: %s", tid, e)

    elif action == "update":
        try:
            # 合并到已有 registry 条目（保留凭证等原有字段）
            from dataclasses import asdict
            existing = tenant_registry.get(tid)
            if existing:
                merged = asdict(existing)
                merged.update(tenant_config)
                tenant_registry.register_from_dict(merged)
            else:
                tenant_registry.register_from_dict(tenant_config)
            logger.info("tenant_sync: update tenant %s in registry", tid)
        except Exception as e:
            logger.warning("tenant_sync: update %s failed: %s", tid, e)

    elif action == "remove":
        if hasattr(tenant_registry, "unregister"):
            try:
                tenant_registry.unregister(tid)
                logger.info("tenant_sync: removed tenant %s from registry", tid)
            except Exception as e:
                logger.debug("tenant_sync: unregister %s: %s", tid, e)

    _last_processed_ts = ts
    return True


async def _poll_loop():
    """后台轮询 Redis 队列，处理新消息。"""
    global _last_processed_ts
    _last_processed_ts = time.time()  # 只处理启动后的消息

    logger.info("tenant_sync: poll loop started (interval=%ds)", _POLL_INTERVAL)

    while True:
        await asyncio.sleep(_POLL_INTERVAL)

        if not redis.available():
            continue

        try:
            messages = redis.execute("LRANGE", _QUEUE_KEY, 0, -1)
            if not messages or not isinstance(messages, list):
                continue

            processed = 0
            for msg_str in messages:
                if isinstance(msg_str, str) and _process_message(msg_str):
                    processed += 1

            if processed:
                logger.info("tenant_sync: processed %d message(s)", processed)

        except Exception:
            logger.debug("tenant_sync: poll error", exc_info=True)


def start_sync_listener():
    """启动后台同步监听任务。在 main.py startup 中调用。"""
    asyncio.create_task(_poll_loop())
    logger.info("tenant_sync: listener started")
