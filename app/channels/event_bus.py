"""跨 Channel 事件总线

OpenClaw 的多 channel 架构缺失的关键能力：channel 间的数据流转。
OpenClaw 的 session 是 per-channel 隔离的，无法跨 channel 协作。

EventBus 解决这个问题：
  Discord channel 收到玩家反馈 → 发布 Event
  → 飞书 channel 的 subscriber 收到 → 调用飞书 API 写反馈文档

架构:
  Event = 跨 channel 消息单元（who/what/where/payload）
  EventBus = 发布-订阅中心（进程内 asyncio + 可扩展 Redis pub/sub）
  EventHandler = 订阅者回调（async callable）

使用方式:
  # 发布事件（在 Discord webhook handler 中）
  await event_bus.publish(ChannelEvent(
      event_type="player_feedback",
      source_channel="discord",
      source_chat_id="guild:123:channel:456",
      sender_id="user_789",
      payload={"text": "游戏太卡了", "category": "performance"},
  ))

  # 订阅事件（在 app 启动时注册）
  async def handle_feedback(event: ChannelEvent):
      # 用飞书 API 写到反馈文档
      await feishu_api.append_to_doc(doc_token, event.payload["text"])

  event_bus.subscribe("player_feedback", handle_feedback)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# 事件处理器类型
EventHandler = Callable[["ChannelEvent"], Awaitable[None]]


@dataclass
class ChannelEvent:
    """跨 channel 事件"""

    # 事件类型（订阅的 key）
    # 约定的事件类型：
    #   "player_feedback"  — 玩家反馈
    #   "message_received" — 收到消息（通用）
    #   "doc_created"      — 文档已创建
    #   "task_completed"   — 任务完成
    #   "user_registered"  — 新用户注册
    #   自定义类型随意，只要 publisher 和 subscriber 约定好
    event_type: str

    # 来源 channel 信息
    source_channel: str = ""         # "feishu" / "discord" / "wecom" 等
    source_chat_id: str = ""         # 来源聊天 ID
    source_tenant_id: str = ""       # 来源租户 ID

    # 发送者
    sender_id: str = ""
    sender_name: str = ""

    # 事件数据（自由结构）
    payload: dict[str, Any] = field(default_factory=dict)

    # 元数据
    timestamp: float = field(default_factory=time.time)
    event_id: str = ""               # 去重 ID（空 = 不去重）

    # 目标（可选 — 指定哪个 channel/tenant 处理）
    target_channel: str = ""         # 空 = 广播给所有订阅者
    target_tenant_id: str = ""       # 空 = 所有租户


class EventBus:
    """进程内事件总线

    支持:
    - 按 event_type 订阅
    - 通配符 "*" 订阅所有事件
    - 按 target_channel 过滤（handler 可声明只关心特定 channel 的事件）
    - 异步非阻塞分发（publish 不等待 handler 完成）
    - 事件去重（event_id 非空时 10 分钟窗口去重）
    """

    def __init__(self) -> None:
        # event_type → list of (handler, filter_channel)
        self._subscribers: dict[str, list[tuple[EventHandler, str]]] = defaultdict(list)
        # 去重缓存: event_id → timestamp
        self._seen_events: dict[str, float] = {}
        self._dedup_ttl = 600  # 10 分钟

    def subscribe(
        self,
        event_type: str,
        handler: EventHandler,
        *,
        source_channel: str = "",
    ) -> None:
        """订阅事件。

        Args:
            event_type: 要订阅的事件类型。"*" = 订阅所有。
            handler: 异步回调。
            source_channel: 只接收来自此 channel 的事件。空 = 不过滤。
        """
        self._subscribers[event_type].append((handler, source_channel))
        logger.info(
            "event_bus: subscribed %s to '%s' (filter_channel=%s)",
            handler.__name__, event_type, source_channel or "*",
        )

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """取消订阅。"""
        subs = self._subscribers.get(event_type, [])
        self._subscribers[event_type] = [
            (h, ch) for h, ch in subs if h is not handler
        ]

    async def publish(self, event: ChannelEvent) -> int:
        """发布事件。

        非阻塞 — 所有 handler 在后台 task 中执行。
        Returns: 匹配的 handler 数量。
        """
        # 去重
        if event.event_id:
            self._cleanup_seen()
            if event.event_id in self._seen_events:
                logger.debug("event_bus: dedup skip event_id=%s", event.event_id)
                return 0
            self._seen_events[event.event_id] = time.time()

        # 收集匹配的 handlers
        handlers: list[EventHandler] = []

        for event_type_key in (event.event_type, "*"):
            for handler, filter_channel in self._subscribers.get(event_type_key, []):
                # 按 source_channel 过滤
                if filter_channel and filter_channel != event.source_channel:
                    continue
                handlers.append(handler)

        if not handlers:
            logger.debug("event_bus: no handlers for '%s'", event.event_type)
            return 0

        # 后台分发
        for handler in handlers:
            asyncio.create_task(self._safe_dispatch(handler, event))

        logger.info(
            "event_bus: published '%s' from %s → %d handlers",
            event.event_type, event.source_channel, len(handlers),
        )
        return len(handlers)

    async def _safe_dispatch(self, handler: EventHandler, event: ChannelEvent) -> None:
        """安全调用 handler，捕获异常。"""
        try:
            await handler(event)
        except Exception:
            logger.exception(
                "event_bus: handler %s failed for event '%s'",
                handler.__name__, event.event_type,
            )

    def _cleanup_seen(self) -> None:
        """清理过期的去重条目。"""
        now = time.time()
        cutoff = now - self._dedup_ttl
        expired = [eid for eid, ts in self._seen_events.items() if ts < cutoff]
        for eid in expired:
            del self._seen_events[eid]


# ── 全局单例 ──

event_bus = EventBus()
