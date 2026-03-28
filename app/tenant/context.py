"""租户上下文管理

使用 contextvars 在请求生命周期内传递当前租户配置、Channel 和发送者身份。
所有需要租户配置的模块统一通过 get_current_tenant() 获取。
需要平台凭证的模块通过 get_current_channel() 获取当前 channel。
需要验证发送者身份的模块通过 get_current_sender() 获取。

如果未设置当前租户（例如后台任务），自动回退到从环境变量构建的默认租户。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.tenant.config import TenantConfig, ChannelConfig

_current_tenant: contextvars.ContextVar[TenantConfig | None] = contextvars.ContextVar(
    "_current_tenant", default=None
)

_current_channel: contextvars.ContextVar[ChannelConfig | None] = contextvars.ContextVar(
    "_current_channel", default=None
)


@dataclass
class SenderContext:
    """当前请求的发送者信息（由 webhook handler 设置，工具层读取）"""
    sender_id: str = ""
    sender_name: str = ""
    is_super_admin: bool = False
    # ── 跨平台身份（新架构）──
    identity_id: str = ""             # 统一身份 UUID（跨 channel）
    channel_platform: str = ""        # 当前消息来自哪个平台
    linked_platforms: dict = field(default_factory=dict)  # {platform: platform_user_id}


_current_sender: contextvars.ContextVar[SenderContext | None] = contextvars.ContextVar(
    "_current_sender", default=None
)

# 当前请求的 chat_id（由 webhook handler 设置，工具层读取）
_current_chat_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_chat_id", default=""
)


def set_current_tenant(tenant: TenantConfig) -> None:
    """设置当前请求的租户上下文"""
    _current_tenant.set(tenant)


def get_current_tenant() -> TenantConfig:
    """获取当前租户配置。未设置时回退到默认租户。"""
    tenant = _current_tenant.get()
    if tenant is not None:
        return tenant
    # 回退：从全局 registry 获取默认租户
    from app.tenant.registry import tenant_registry
    return tenant_registry.get_default()


def set_current_channel(channel: ChannelConfig) -> None:
    """设置当前请求的 Channel 上下文。

    webhook handler 在确定当前请求对应的 channel 后调用。
    下游代码通过 get_current_channel() 读取平台凭证。
    """
    _current_channel.set(channel)


def get_current_channel() -> Optional[ChannelConfig]:
    """获取当前 Channel 配置。

    如果未设置（旧代码路径），返回 None，调用方应 fallback 到 tenant 顶层字段。
    """
    return _current_channel.get()


def get_current_channel_or_primary() -> ChannelConfig:
    """获取当前 Channel，如果未设置则返回 tenant 的 primary channel。

    这是向后兼容的便利方法：确保总是返回一个 ChannelConfig。
    """
    ch = _current_channel.get()
    if ch is not None:
        return ch
    tenant = get_current_tenant()
    channels = tenant.get_channels()
    return channels[0] if channels else tenant._build_primary_channel()


def set_current_sender(sender_id_or_ctx: str | SenderContext = "", sender_name: str = "") -> SenderContext:
    """设置当前请求的发送者上下文。

    支持两种调用方式：
    1. set_current_sender("sender_id", "name") — 从 ID 构建，自动判定超管
    2. set_current_sender(SenderContext(...)) — 传入已构建的 context（含 identity 等）
    """
    if isinstance(sender_id_or_ctx, SenderContext):
        ctx = sender_id_or_ctx
        # 补充超管判定（如果调用方未设置）
        if not ctx.is_super_admin and ctx.sender_id:
            from app.services.super_admin import is_super_admin
            tenant = get_current_tenant()
            ctx.is_super_admin = is_super_admin(ctx.sender_id, ctx.sender_name, tenant.tenant_id)
        _current_sender.set(ctx)
        return ctx

    sender_id = sender_id_or_ctx
    from app.services.super_admin import is_super_admin
    tenant = get_current_tenant()
    channel = get_current_channel()
    ctx = SenderContext(
        sender_id=sender_id,
        sender_name=sender_name,
        is_super_admin=is_super_admin(sender_id, sender_name, tenant.tenant_id),
        channel_platform=channel.platform if channel else tenant.platform,
    )
    _current_sender.set(ctx)
    return ctx


def get_current_sender() -> SenderContext:
    """获取当前发送者上下文。未设置时返回空 SenderContext。"""
    ctx = _current_sender.get()
    return ctx if ctx is not None else SenderContext()


def set_current_chat_id(chat_id: str) -> None:
    """设置当前请求的 chat_id（由 webhook handler 调用）"""
    _current_chat_id.set(chat_id)


def get_current_chat_id() -> str:
    """获取当前请求的 chat_id"""
    return _current_chat_id.get()
