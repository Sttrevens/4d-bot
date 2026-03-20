"""多平台 Channel 抽象层

支持飞书、企业微信等不同消息平台的统一接口。

借鉴 OpenClaw 的多 channel 架构，增加：
- ChannelCapabilities: 声明每个平台的能力（文档/日历/reactions 等）
- EventBus: 跨 channel 事件发布-订阅
- AgentProfile routing: 按 channel/chat/user 路由到不同 agent 配置
- chunk_markdown: Markdown-aware 消息分块
"""

from __future__ import annotations

from app.channels.base import Channel, ChannelCapabilities
from app.channels.event_bus import event_bus, ChannelEvent
from app.channels.routing import (
    AgentProfile,
    AgentBinding,
    resolve_agent_profile,
)
from app.channels.chunking import chunk_markdown

__all__ = [
    "Channel",
    "ChannelCapabilities",
    "event_bus",
    "ChannelEvent",
    "AgentProfile",
    "AgentBinding",
    "resolve_agent_profile",
    "chunk_markdown",
    "get_channel",
]

# 按 platform 缓存 Channel 实例（每个平台只需一个）
_channel_cache: dict[str, Channel] = {}


def get_channel(platform: str = "") -> Channel:
    """根据平台类型获取 Channel 实例。

    不传 platform 时从当前 tenant 上下文推断。
    """
    if not platform:
        from app.tenant.context import get_current_tenant
        platform = get_current_tenant().platform or "feishu"

    if platform in _channel_cache:
        return _channel_cache[platform]

    if platform == "feishu":
        from app.channels.feishu import FeishuChannel
        from app.services.feishu import feishu_client
        ch = FeishuChannel(client=feishu_client)
    elif platform == "qq":
        from app.channels.qq import QQChannel
        ch = QQChannel()
    # elif platform == "wecom":
    #     from app.channels.wecom import WeComChannel
    #     ch = WeComChannel()
    else:
        raise ValueError(f"unsupported platform: {platform}")

    _channel_cache[platform] = ch
    return ch
