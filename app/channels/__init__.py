"""多平台 Channel 抽象层

支持飞书、企业微信等不同消息平台的统一接口。
"""

from __future__ import annotations

from app.channels.base import Channel

__all__ = ["Channel", "get_channel"]

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
