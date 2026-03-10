"""租户数据隔离测试

验证跨租户的全局状态不会串扰:
- tuk() 使用 tenant:userid 作为 key
- UserStateManager 的 _user_modes 使用 tuk key
- TenantConfig 新增的配额/限流字段正确初始化
"""

from unittest.mock import patch, MagicMock
from dataclasses import fields

from app.tenant.config import TenantConfig


class TestTenantConfig:
    def test_default_values(self):
        config = TenantConfig()
        assert config.tenant_id == ""
        assert config.platform == "feishu"
        assert config.self_iteration_enabled is False
        assert config.instance_management_enabled is False

    def test_quota_fields_exist(self):
        """配额字段应存在且默认为 0"""
        config = TenantConfig()
        assert config.quota_monthly_api_calls == 0
        assert config.quota_monthly_tokens == 0
        assert config.rate_limit_rpm == 0
        assert config.rate_limit_user_rpm == 0

    def test_quota_fields_configurable(self):
        """配额字段可自定义"""
        config = TenantConfig(
            tenant_id="paid-customer",
            quota_monthly_api_calls=1000,
            quota_monthly_tokens=5000000,
            rate_limit_rpm=120,
            rate_limit_user_rpm=20,
        )
        assert config.quota_monthly_api_calls == 1000
        assert config.quota_monthly_tokens == 5000000
        assert config.rate_limit_rpm == 120
        assert config.rate_limit_user_rpm == 20

    def test_all_fields_have_defaults(self):
        """所有字段都应有默认值（允许从 JSON 部分加载）"""
        config = TenantConfig()
        for f in fields(config):
            value = getattr(config, f.name)
            assert value is not None, f"Field {f.name} has None default"


class TestTukFunction:
    """测试 tuk() 函数生成租户隔离 key（现在统一在 base.py 中）"""

    def test_tuk_generates_tenant_key(self):
        """tuk 函数应生成 tenant_id:sender_id 格式的 key"""
        from app.webhook.base import tuk

        with patch("app.webhook.base.get_current_tenant") as mock_get:
            mock_tenant = MagicMock()
            mock_tenant.tenant_id = "code-bot"
            mock_get.return_value = mock_tenant

            key = tuk("user123")
            assert key == "code-bot:user123"

    def test_different_tenants_different_keys(self):
        """不同租户的同一用户 ID 应产生不同 key"""
        from app.webhook.base import tuk

        with patch("app.webhook.base.get_current_tenant") as mock_get:
            # Tenant A
            mock_tenant_a = MagicMock()
            mock_tenant_a.tenant_id = "tenant-a"
            mock_get.return_value = mock_tenant_a
            key_a = tuk("user123")

            # Tenant B
            mock_tenant_b = MagicMock()
            mock_tenant_b.tenant_id = "tenant-b"
            mock_get.return_value = mock_tenant_b
            key_b = tuk("user123")

            assert key_a != key_b
            assert key_a == "tenant-a:user123"
            assert key_b == "tenant-b:user123"

    def test_tuk_same_user_same_tenant(self):
        """同租户同用户应产生相同 key"""
        from app.webhook.base import tuk

        with patch("app.webhook.base.get_current_tenant") as mock_get:
            mock_tenant = MagicMock()
            mock_tenant.tenant_id = "my-bot"
            mock_get.return_value = mock_tenant

            assert tuk("user1") == tuk("user1")
            assert tuk("user1") != tuk("user2")


class TestModeIsolation:
    """测试用户模式不会跨租户串扰（通过 UserStateManager）"""

    def test_wecom_modes_isolated(self):
        """企微 handler 的用户模式应使用 tuk key（通过 _state）"""
        from app.webhook.base import UserStateManager

        state = UserStateManager()

        with patch("app.webhook.base.get_current_tenant") as mock_get:
            # Tenant A 设置 full_access
            mock_a = MagicMock()
            mock_a.tenant_id = "tenant-a"
            mock_get.return_value = mock_a
            state.set_mode("user1", "full_access")

            # Tenant B 的 user1 应该还是 safe
            mock_b = MagicMock()
            mock_b.tenant_id = "tenant-b"
            mock_get.return_value = mock_b
            mode_b = state.get_mode("user1")
            assert mode_b == "safe"

            # Tenant A 的 user1 仍然是 full_access
            mock_get.return_value = mock_a
            mode_a = state.get_mode("user1")
            assert mode_a == "full_access"

    def test_wecom_kf_modes_isolated(self):
        """不同 UserStateManager 实例互不影响"""
        from app.webhook.base import UserStateManager

        state_a = UserStateManager()
        state_b = UserStateManager()

        with patch("app.webhook.base.get_current_tenant") as mock_get:
            mock_tenant = MagicMock()
            mock_tenant.tenant_id = "same-tenant"
            mock_get.return_value = mock_tenant

            state_a.set_mode("user1", "full_access")
            assert state_a.get_mode("user1") == "full_access"
            assert state_b.get_mode("user1") == "safe"  # Different instance


class TestMessageDedup:
    """测试消息去重器"""

    def test_set_dedup(self):
        """Set 模式去重"""
        from app.webhook.base import MessageDedup

        dedup = MessageDedup(max_cache=100)
        assert not dedup.is_duplicate("msg1")
        assert dedup.is_duplicate("msg1")
        assert not dedup.is_duplicate("msg2")

    def test_ttl_dedup(self):
        """TTL 模式去重"""
        from app.webhook.base import MessageDedup

        dedup = MessageDedup(max_cache=100, ttl=600)
        assert not dedup.is_duplicate("ev1")
        assert dedup.is_duplicate("ev1")
        assert not dedup.is_duplicate("ev2")

    def test_empty_id_not_duplicate(self):
        """空 ID 不应被视为重复"""
        from app.webhook.base import MessageDedup

        dedup = MessageDedup(max_cache=100)
        assert not dedup.is_duplicate("")
        assert not dedup.is_duplicate("")

    def test_set_dedup_cleanup(self):
        """Set 模式超过 max_cache 时应清理"""
        from app.webhook.base import MessageDedup

        dedup = MessageDedup(max_cache=10)
        for i in range(20):
            dedup.is_duplicate(f"msg{i}")
        # 应该清理了一半（10 -> 5），然后又加了 10
        # 总数应不超过 max_cache + 少量
        assert len(dedup._store_set) <= 15


class TestSplitReply:
    """测试回复分段"""

    def test_short_reply_no_split(self):
        from app.webhook.base import split_reply
        assert split_reply("hello", 2000) == ["hello"]

    def test_long_reply_splits(self):
        from app.webhook.base import split_reply
        text = "a" * 3000
        chunks = split_reply(text, 2000)
        assert len(chunks) == 2
        assert "".join(chunks) == text

    def test_split_at_newline(self):
        from app.webhook.base import split_reply
        text = "a" * 1500 + "\n" + "b" * 1000
        chunks = split_reply(text, 2000)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 1500
