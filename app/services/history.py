"""对话历史管理

按 {tenant_id}:{sender_id} 隔离，每个人在每个租户下独立的对话历史。
避免 A 的编码任务上下文污染 B 的对话，也防止同容器 co-tenant 间历史串流。

持久化：写入 Redis（write-through），重启后自动恢复。
Redis key 格式: {tenant_id}:chat:{sender_id}
内存 key 格式: {tenant_id}:{sender_id}（防止共享单例跨租户泄露）
TTL: 与 expire_seconds 一致，Redis 自动清理过期会话。
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 工具调用摘要上下文（provider → route_message 传递） ──
# provider 在 agent loop 结束后将工具调用摘要写入此 contextvar，
# route_message 读取后附加到 history，让下一轮对话有工具上下文。
last_tool_summary: contextvars.ContextVar[str] = contextvars.ContextVar(
    "last_tool_summary", default=""
)


@dataclass
class Message:
    role: str  # "user" 或 "assistant"
    content: str
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {"role": self.role, "content": self.content, "ts": self.timestamp},
            ensure_ascii=False,
        )

    @staticmethod
    def from_json(raw: str) -> "Message":
        d = json.loads(raw)
        return Message(role=d["role"], content=d["content"], timestamp=d.get("ts", 0))


def _tenant_id() -> str:
    """获取当前租户 ID"""
    try:
        from app.tenant.context import get_current_tenant
        return get_current_tenant().tenant_id
    except Exception:
        return "default"


def _redis_key(sender_id: str) -> str:
    """构建 Redis key（租户隔离）"""
    return f"{_tenant_id()}:chat:{sender_id}"


def _store_key(sender_id: str) -> str:
    """构建内存缓存 key（租户隔离）。

    同容器内 co-tenant 共享 ChatHistory 单例，
    必须用 {tenant_id}:{sender_id} 防止跨租户历史泄露。
    """
    return f"{_tenant_id()}:{sender_id}"


def _save_to_redis(sender_id: str, messages: list[Message], ttl: int) -> None:
    """保存到 Redis（不阻塞主流程）"""
    try:
        from app.services import redis_client as redis
        if not redis.available():
            return
        key = _redis_key(sender_id)
        data = [{"role": m.role, "content": m.content, "ts": m.timestamp} for m in messages]
        redis.execute("SET", key, json.dumps(data, ensure_ascii=False), "EX", str(ttl))
    except Exception:
        logger.debug("chat history redis save failed for %s", sender_id[:12], exc_info=True)


def _load_from_redis(sender_id: str) -> list[Message]:
    """从 Redis 恢复对话历史"""
    try:
        from app.services import redis_client as redis
        if not redis.available():
            return []
        key = _redis_key(sender_id)
        raw = redis.execute("GET", key)
        if not raw:
            return []
        data = json.loads(raw)
        return [Message(role=m["role"], content=m["content"], timestamp=m.get("ts", 0)) for m in data]
    except Exception:
        logger.debug("chat history redis load failed for %s", sender_id[:12], exc_info=True)
        return []


def _backfill_from_platform(sender_id: str, max_rounds: int) -> list[Message]:
    """平台感知的上下文回填调度器。

    当 Redis chat_history 为空（TTL 过期或从未缓存）时，
    根据当前租户平台选择回填策略：
    - 飞书：从飞书 /im/v1/messages API 拉取最近聊天记录
    - 企微客服：从 Redis 消息归档（kf_archive:*）恢复
    """
    try:
        from app.tenant.context import get_current_tenant
        platform = get_current_tenant().platform
    except Exception:
        return []

    if platform == "feishu":
        return _backfill_from_feishu(sender_id, max_rounds)
    elif platform == "wecom_kf":
        return _backfill_from_wecom_kf(sender_id, max_rounds)
    return []


def _backfill_from_wecom_kf(sender_id: str, max_rounds: int) -> list[Message]:
    """从 Redis 消息归档恢复企微客服对话上下文。

    wecom_kf_handler 在每次对话时将用户消息和 bot 回复归档到
    kf_archive:{tenant_id}:{external_userid}（Redis LIST，TTL 7 天）。
    当 chat_history TTL 过期后，从归档恢复最近 N 轮对话，
    让 bot 重新获得上下文，不再"失忆"。
    """
    try:
        from app.tenant.context import get_current_tenant
        from app.services import redis_client as redis

        tenant = get_current_tenant()
        if not redis.available():
            return []

        key = f"kf_archive:{tenant.tenant_id}:{sender_id}"
        count = max_rounds * 2
        raw_list = redis.execute("LRANGE", key, "0", str(count - 1))
        if not raw_list or not isinstance(raw_list, list):
            return []

        # LPUSH 存储最新在前，反转为时间正序
        messages = []
        for raw in reversed(raw_list):
            try:
                d = json.loads(raw) if isinstance(raw, str) else raw
                messages.append(Message(
                    role=d["role"],
                    content=d["content"],
                    timestamp=d.get("ts", 0),
                ))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

        if messages:
            logger.info(
                "backfill from wecom_kf archive: %s → %d messages restored",
                sender_id[:15], len(messages),
            )
        return messages

    except Exception:
        logger.warning("backfill from wecom_kf failed for %s", sender_id[:15], exc_info=True)
        return []


def _backfill_from_feishu(sender_id: str, max_rounds: int) -> list[Message]:
    """当 Redis 缓存为空时，从飞书 API 拉取最近聊天记录回填。

    仅对飞书平台租户有效。
    返回 Message 列表（按时间正序），失败时返回空列表。
    """
    # sender_id 格式：ou_xxx（私聊）或 oc_xxx（群聊）
    if not sender_id.startswith(("ou_", "oc_")):
        return []

    try:
        from app.tools.feishu_api import feishu_get
        from app.services import user_registry

        chat_id = sender_id
        container_id_type = "chat"

        # 私聊：需要先查到 p2p chat_id
        if sender_id.startswith("ou_"):
            real_chat_id = user_registry.get_p2p_chat_id(sender_id)
            if not real_chat_id:
                logger.debug("backfill skip: no p2p chat_id for %s", sender_id[:15])
                return []
            chat_id = real_chat_id
            container_id_type = "p2p" if not real_chat_id.startswith("oc_") else "chat"

        # 拉取消息数 = max_rounds * 2（每轮 1 user + 1 assistant）
        count = max_rounds * 2
        _PAGE_MAX = 50
        items: list[dict] = []
        page_token = ""
        remaining = count

        while remaining > 0:
            page_size = min(remaining, _PAGE_MAX)
            params: dict = {
                "container_id": chat_id,
                "container_id_type": container_id_type,
                "page_size": page_size,
                "sort_type": "ByCreateTimeDesc",
            }
            if page_token:
                params["page_token"] = page_token

            data = feishu_get("/im/v1/messages", params=params)
            if isinstance(data, str):
                logger.warning("backfill feishu API failed: %s", data[:120])
                break

            page_items = data.get("data", {}).get("items", [])
            items.extend(page_items)
            remaining -= len(page_items)

            has_more = data.get("data", {}).get("has_more", False)
            page_token = data.get("data", {}).get("page_token", "")
            if not has_more or not page_token or not page_items:
                break

        if not items:
            return []

        # items 是倒序（最新在前），反转为时间正序
        items.reverse()

        messages: list[Message] = []

        for msg in items:
            sender = msg.get("sender", {})
            sender_type = sender.get("sender_type", "")
            msg_type = msg.get("msg_type", "")
            body = msg.get("body", {})
            content_raw = body.get("content", "")
            create_time = msg.get("create_time", "")

            # 跳过系统消息
            if msg_type == "system":
                continue

            # 解析时间戳（飞书返回毫秒字符串）
            try:
                ts = int(create_time) / 1000.0 if create_time else time.time()
            except (ValueError, TypeError):
                ts = time.time()

            # 判断角色：app = assistant，其他 = user
            role = "assistant" if sender_type == "app" else "user"

            # 解析消息内容
            text = ""
            if msg_type == "text":
                try:
                    text = json.loads(content_raw).get("text", content_raw)
                except (ValueError, TypeError):
                    text = content_raw
            elif msg_type == "post":
                # 简化处理：提取纯文本
                try:
                    from app.tools.message_ops import _extract_post_text
                    text = _extract_post_text(content_raw)
                except Exception:
                    text = "[富文本]"
            elif msg_type == "image":
                text = "[图片]"
            elif msg_type in ("file", "audio", "video", "sticker"):
                text = f"[{msg_type}]"
            else:
                text = f"[{msg_type}消息]"

            if text:
                # 群聊中 user 消息带上发送者名字
                if sender_type != "app" and chat_id.startswith("oc_"):
                    sender_open_id = sender.get("id", "")
                    name = user_registry.get_name(sender_open_id) or sender_open_id[:12]
                    text = f"[{name}]: {text}"
                messages.append(Message(role=role, content=text, timestamp=ts))

        if messages:
            logger.info(
                "backfill from feishu: %s → %d messages restored",
                sender_id[:15], len(messages),
            )

        return messages

    except Exception:
        logger.warning("backfill from feishu failed for %s", sender_id[:15], exc_info=True)
        return []


def _get_tenant_memory_config() -> tuple[int, int]:
    """获取当前租户的对话历史配置。返回 (max_rounds, expire_seconds)。"""
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        rounds = getattr(tenant, "memory_chat_rounds", 0)
        ttl = getattr(tenant, "memory_chat_ttl", 0)
        return (rounds if rounds > 0 else 5, ttl if ttl > 0 else 3600)
    except Exception:
        return (5, 3600)


class ChatHistory:
    def __init__(self, max_rounds: int = 10, expire_seconds: int = 3600) -> None:
        self._store: dict[str, list[Message]] = defaultdict(list)
        self._loaded_from_redis: set[str] = set()  # 标记已从 Redis 恢复的 key
        self._default_max_rounds = max_rounds
        self._default_expire = expire_seconds

    def _get_config(self) -> tuple[int, int]:
        """获取当前租户的 (max_rounds, expire_seconds)，回退到实例默认值。"""
        rounds, ttl = _get_tenant_memory_config()
        return (
            rounds if rounds != 5 else self._default_max_rounds,
            ttl if ttl != 3600 else self._default_expire,
        )

    def _ensure_loaded(self, sender_id: str) -> None:
        """确保已从 Redis 恢复（lazy load，仅首次访问时触发）。

        三级加载：内存缓存 → Redis → 平台回填。
        平台回填仅在 Redis 也为空时触发（TTL 过期或首次对话）：
        - 飞书：从 /im/v1/messages API 拉取最近聊天记录
        - 企微客服：从 Redis 消息归档（kf_archive:*）恢复
        回填后写入 Redis 缓存，避免重复拉取。
        """
        sk = _store_key(sender_id)
        if sk in self._loaded_from_redis:
            return
        self._loaded_from_redis.add(sk)
        if not self._store[sk]:
            restored = _load_from_redis(sender_id)
            if restored:
                self._store[sk] = restored
                logger.debug("chat history restored from redis: %s (%d msgs)", sk[:24], len(restored))
            else:
                # Redis 为空（TTL 过期或从未缓存），尝试从平台回填
                max_rounds, ttl = self._get_config()
                backfilled = _backfill_from_platform(sender_id, max_rounds)
                if backfilled:
                    self._store[sk] = backfilled
                    # 回填成功后写入 Redis，避免重复拉取
                    _save_to_redis(sender_id, backfilled, ttl)

    def get(self, sender_id: str) -> list[dict]:
        """获取某用户的历史消息（OpenAI messages 格式）"""
        self._ensure_loaded(sender_id)
        sk = _store_key(sender_id)
        self._cleanup(sk)
        return [
            {"role": m.role, "content": m.content}
            for m in self._store[sk]
        ]

    def add_user(self, sender_id: str, text: str) -> None:
        self._ensure_loaded(sender_id)
        sk = _store_key(sender_id)
        self._store[sk].append(Message(role="user", content=text))
        self._trim(sk)
        _, ttl = self._get_config()
        _save_to_redis(sender_id, self._store[sk], ttl)

    def add_assistant(self, sender_id: str, text: str) -> None:
        self._ensure_loaded(sender_id)
        sk = _store_key(sender_id)
        self._store[sk].append(Message(role="assistant", content=text))
        self._trim(sk)
        _, ttl = self._get_config()
        _save_to_redis(sender_id, self._store[sk], ttl)

    def _trim(self, sk: str) -> None:
        """保留最近 max_rounds 轮（每轮 = 1 user + 1 assistant = 2 条）"""
        max_rounds, _ = self._get_config()
        msgs = self._store[sk]
        max_msgs = max_rounds * 2
        if len(msgs) > max_msgs:
            self._store[sk] = msgs[-max_msgs:]

    def clear(self, sender_id: str) -> None:
        """清除某用户的对话历史（内存 + Redis）。

        用于"重新开始"场景：用户要求重做时，
        清除污染的上下文，让 bot 从干净状态开始。
        """
        sk = _store_key(sender_id)
        self._store[sk] = []
        self._loaded_from_redis.discard(sk)
        try:
            from app.services import redis_client as redis
            if redis.available():
                redis.execute("DEL", _redis_key(sender_id))
        except Exception:
            logger.debug("chat history redis clear failed for %s",
                         sender_id[:12], exc_info=True)
        logger.info("chat history cleared: %s (fresh start)", sender_id[:12])

    def _cleanup(self, sk: str) -> None:
        """清除过期消息"""
        _, expire = self._get_config()
        now = time.time()
        self._store[sk] = [
            m for m in self._store[sk]
            if now - m.timestamp < expire
        ]


# 全局实例（默认 5 轮 = 10 条消息，实际配置从 tenant config 读取）
chat_history = ChatHistory(max_rounds=5)
