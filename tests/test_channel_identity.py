"""多 Channel + 跨平台身份系统测试

测试:
1. ChannelConfig 创建和属性
2. TenantConfig 多 channel 支持（get_channels, has_platform, find_channel_by_id）
3. TenantRegistry channel 查找
4. SenderContext identity 字段
5. Identity 工具注册
6. Identity Redis 操作（mock）
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.tenant.config import TenantConfig, ChannelConfig
from app.tenant.context import SenderContext


# ── ChannelConfig 基础 ──

class TestChannelConfig:
    def test_default_values(self):
        ch = ChannelConfig()
        assert ch.platform == "feishu"
        assert ch.enabled is True
        assert ch.channel_id == ""
        assert ch.app_id == ""

    def test_feishu_channel(self):
        ch = ChannelConfig(
            channel_id="ch-feishu-1",
            platform="feishu",
            app_id="cli_xxx",
            app_secret="secret",
        )
        assert ch.channel_id == "ch-feishu-1"
        assert ch.platform == "feishu"
        assert ch.app_id == "cli_xxx"

    def test_wecom_channel(self):
        ch = ChannelConfig(
            channel_id="ch-wecom-1",
            platform="wecom",
            wecom_corpid="corp_xxx",
            wecom_corpsecret="secret",
        )
        assert ch.platform == "wecom"
        assert ch.wecom_corpid == "corp_xxx"

    def test_wecom_kf_channel(self):
        ch = ChannelConfig(
            channel_id="ch-kf-1",
            platform="wecom_kf",
            wecom_kf_open_kfid="kf_xxx",
        )
        assert ch.platform == "wecom_kf"
        assert ch.wecom_kf_open_kfid == "kf_xxx"


# ── TenantConfig 多 Channel ──

class TestTenantMultiChannel:
    def test_no_channels_uses_primary(self):
        """无 channels 配置时，从顶层字段自动生成 primary channel"""
        t = TenantConfig(
            tenant_id="test",
            platform="feishu",
            app_id="cli_xxx",
            app_secret="secret",
        )
        channels = t.get_channels()
        assert len(channels) == 1
        assert channels[0].platform == "feishu"
        assert channels[0].app_id == "cli_xxx"

    def test_explicit_channels(self):
        """显式配置 channels 时，使用配置的 channel 列表"""
        t = TenantConfig(
            tenant_id="test",
            channels=[
                ChannelConfig(channel_id="ch1", platform="feishu", app_id="cli_1"),
                ChannelConfig(channel_id="ch2", platform="wecom", wecom_corpid="corp_1"),
            ],
        )
        channels = t.get_channels()
        assert len(channels) == 2
        assert channels[0].platform == "feishu"
        assert channels[1].platform == "wecom"

    def test_disabled_channel_excluded(self):
        """disabled 的 channel 不出现在 get_channels 结果中"""
        t = TenantConfig(
            tenant_id="test",
            channels=[
                ChannelConfig(channel_id="ch1", platform="feishu", enabled=True),
                ChannelConfig(channel_id="ch2", platform="wecom", enabled=False),
            ],
        )
        channels = t.get_channels()
        assert len(channels) == 1
        assert channels[0].channel_id == "ch1"

    def test_has_platform_multi(self):
        """has_platform 检查所有 channel"""
        t = TenantConfig(
            tenant_id="test",
            channels=[
                ChannelConfig(platform="feishu"),
                ChannelConfig(platform="wecom_kf"),
            ],
        )
        assert t.has_platform("feishu") is True
        assert t.has_platform("wecom_kf") is True
        assert t.has_platform("wecom") is False

    def test_has_platform_legacy(self):
        """旧配置（无 channels）也能通过 has_platform"""
        t = TenantConfig(tenant_id="test", platform="feishu")
        assert t.has_platform("feishu") is True
        assert t.has_platform("wecom") is False

    def test_get_channel_by_platform(self):
        t = TenantConfig(
            tenant_id="test",
            channels=[
                ChannelConfig(channel_id="ch1", platform="feishu"),
                ChannelConfig(channel_id="ch2", platform="wecom"),
            ],
        )
        ch = t.get_channel("wecom")
        assert ch is not None
        assert ch.channel_id == "ch2"

    def test_find_channel_by_id(self):
        t = TenantConfig(
            tenant_id="test",
            channels=[
                ChannelConfig(channel_id="ch-abc", platform="feishu"),
                ChannelConfig(channel_id="ch-def", platform="wecom"),
            ],
        )
        ch = t.find_channel_by_id("ch-def")
        assert ch is not None
        assert ch.platform == "wecom"
        assert t.find_channel_by_id("nonexistent") is None

    def test_get_channel_platforms(self):
        t = TenantConfig(
            tenant_id="test",
            channels=[
                ChannelConfig(platform="feishu"),
                ChannelConfig(platform="wecom_kf"),
                ChannelConfig(platform="wecom"),
            ],
        )
        platforms = t.get_channel_platforms()
        assert set(platforms) == {"feishu", "wecom_kf", "wecom"}


# ── Registry Channel 查找 ──

class TestRegistryChannelLookup:
    def test_find_by_channel_id(self):
        from app.tenant.registry import TenantRegistry
        reg = TenantRegistry()
        t = TenantConfig(
            tenant_id="bot1",
            channels=[
                ChannelConfig(channel_id="ch-123", platform="feishu"),
            ],
        )
        reg.register(t)
        tenant, ch = reg.find_by_channel_id("ch-123")
        assert tenant is not None
        assert tenant.tenant_id == "bot1"
        assert ch.platform == "feishu"

    def test_find_by_channel_id_not_found(self):
        from app.tenant.registry import TenantRegistry
        reg = TenantRegistry()
        tenant, ch = reg.find_by_channel_id("nonexistent")
        assert tenant is None
        assert ch is None

    def test_find_by_kf_open_kfid(self):
        from app.tenant.registry import TenantRegistry
        reg = TenantRegistry()
        t = TenantConfig(
            tenant_id="bot1",
            channels=[
                ChannelConfig(platform="wecom_kf", wecom_kf_open_kfid="kf_abc"),
            ],
        )
        reg.register(t)
        tenant, ch = reg.find_by_kf_open_kfid("kf_abc")
        assert tenant is not None
        assert ch.wecom_kf_open_kfid == "kf_abc"


# ── SenderContext ──

class TestSenderContext:
    def test_default_empty(self):
        ctx = SenderContext()
        assert ctx.identity_id == ""
        assert ctx.channel_platform == ""
        assert ctx.linked_platforms == {}

    def test_with_identity(self):
        ctx = SenderContext(
            sender_id="ou_xxx",
            sender_name="Steven",
            identity_id="uuid-123",
            channel_platform="feishu",
            linked_platforms={"feishu": "ou_xxx", "wecom": "userid_yyy"},
        )
        assert ctx.identity_id == "uuid-123"
        assert len(ctx.linked_platforms) == 2
        assert ctx.channel_platform == "feishu"


# ── set_current_sender 兼容 ──

class TestSetCurrentSender:
    def test_string_signature(self):
        """旧的 set_current_sender(sender_id, name) 调用仍然可用"""
        with patch("app.services.super_admin.is_super_admin", return_value=False), \
             patch("app.tenant.context.get_current_tenant") as mock_tenant:
            mock_tenant.return_value = MagicMock(tenant_id="t1", platform="feishu")
            from app.tenant.context import set_current_sender, get_current_sender
            result = set_current_sender("u1", "test_user")
            assert isinstance(result, SenderContext)
            assert result.sender_id == "u1"
            assert result.sender_name == "test_user"

    def test_sender_context_signature(self):
        """新的 set_current_sender(SenderContext(...)) 调用"""
        with patch("app.services.super_admin.is_super_admin", return_value=False), \
             patch("app.tenant.context.get_current_tenant") as mock_tenant:
            mock_tenant.return_value = MagicMock(tenant_id="t1")
            from app.tenant.context import set_current_sender, get_current_sender
            ctx = SenderContext(
                sender_id="ou_xxx",
                sender_name="Steven",
                identity_id="uuid-abc",
                channel_platform="feishu",
            )
            result = set_current_sender(ctx)
            assert result.identity_id == "uuid-abc"
            assert result.sender_id == "ou_xxx"


# ── Identity 工具注册 ──

class TestIdentityToolRegistration:
    def test_identity_tools_in_all_tool_map(self):
        from app.services.base_agent import ALL_TOOL_MAP
        assert "search_known_user" in ALL_TOOL_MAP
        assert "initiate_identity_verification" in ALL_TOOL_MAP
        assert "confirm_identity_verification" in ALL_TOOL_MAP
        assert "get_user_identity" in ALL_TOOL_MAP

    def test_identity_tools_in_all_tool_defs(self):
        from app.services.base_agent import _ALL_TOOL_DEFS
        names = {t["name"] for t in _ALL_TOOL_DEFS}
        assert "search_known_user" in names
        assert "initiate_identity_verification" in names

    def test_identity_tools_in_core_group(self):
        from app.services.base_agent import _TOOL_GROUPS
        core = _TOOL_GROUPS["core"]
        assert "search_known_user" in core
        assert "get_user_identity" in core

    def test_identity_tool_defs_have_input_schema(self):
        """identity 工具定义必须用 input_schema（不是 parameters）"""
        from app.tools.identity_ops import TOOL_DEFINITIONS
        for tool in TOOL_DEFINITIONS:
            assert "input_schema" in tool, f"tool {tool['name']} missing input_schema"
            assert "parameters" not in tool, f"tool {tool['name']} should use input_schema not parameters"


# ── Identity 系统（mock Redis）──

class TestIdentitySystem:
    def test_create_identity_no_redis(self):
        """Redis 不可用时返回 None"""
        with patch("app.services.identity.redis") as mock_redis:
            mock_redis.available.return_value = False
            from app.services.identity import create_identity
            result = create_identity("Steven", "feishu", "ou_xxx")
            assert result is None

    def test_find_identity_no_redis(self):
        with patch("app.services.identity.redis") as mock_redis:
            mock_redis.available.return_value = False
            from app.services.identity import find_identity
            result = find_identity("feishu", "ou_xxx")
            assert result is None

    def test_resolve_sender_no_redis(self):
        with patch("app.services.identity.redis") as mock_redis:
            mock_redis.available.return_value = False
            from app.services.identity import resolve_sender
            iid, linked = resolve_sender("bot1", "feishu", "ou_xxx")
            assert iid is None
            assert linked == {}

    def test_generate_code_format(self):
        from app.services.identity import _generate_code
        import string
        code = _generate_code()
        assert len(code) == 8
        valid_chars = set(string.digits + string.ascii_uppercase)
        assert all(c in valid_chars for c in code)

    def test_initiate_verification_no_redis(self):
        with patch("app.services.identity.redis") as mock_redis:
            mock_redis.available.return_value = False
            from app.services.identity import initiate_verification
            result = initiate_verification("id1", "feishu", "ou_x", "wecom", "uid_y")
            assert result is None
