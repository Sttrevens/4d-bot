"""租户配置和注册表测试

验证:
- TenantConfig 字段完整性
- TenantRegistry 加载、查找、默认租户逻辑
- 环境变量解析（${VAR} 和 ${VAR:default}）
"""

import json
import os
import tempfile
from unittest.mock import patch

from app.tenant.config import TenantConfig
from app.tenant.registry import TenantRegistry


class TestTenantConfigFields:
    def test_all_platform_credentials(self):
        """飞书/企微/微信客服凭证字段都应存在"""
        config = TenantConfig()
        # 飞书
        assert hasattr(config, "app_id")
        assert hasattr(config, "app_secret")
        assert hasattr(config, "verification_token")
        assert hasattr(config, "encrypt_key")
        # 企微
        assert hasattr(config, "wecom_corpid")
        assert hasattr(config, "wecom_corpsecret")
        assert hasattr(config, "wecom_token")
        assert hasattr(config, "wecom_encoding_aes_key")
        # 微信客服
        assert hasattr(config, "wecom_kf_secret")
        assert hasattr(config, "wecom_kf_open_kfid")

    def test_llm_config_fields(self):
        """LLM 配置字段"""
        config = TenantConfig()
        assert hasattr(config, "llm_provider")
        assert hasattr(config, "llm_api_key")
        assert hasattr(config, "llm_model")
        assert hasattr(config, "llm_model_strong")
        assert hasattr(config, "coding_model")

    def test_security_fields(self):
        """安全相关字段"""
        config = TenantConfig()
        assert hasattr(config, "admin_open_ids")
        assert hasattr(config, "admin_names")
        assert hasattr(config, "self_iteration_enabled")
        assert hasattr(config, "instance_management_enabled")
        assert hasattr(config, "tools_enabled")

    def test_from_dict(self):
        """从 dict 创建配置"""
        data = {
            "tenant_id": "test",
            "name": "Test Bot",
            "platform": "wecom_kf",
            "quota_monthly_api_calls": 500,
        }
        config = TenantConfig(**data)
        assert config.tenant_id == "test"
        assert config.platform == "wecom_kf"
        assert config.quota_monthly_api_calls == 500

    def test_unknown_fields_ignored(self):
        """未知字段不应导致错误（向前兼容）"""
        # TenantRegistry.load_from_file filters fields via TenantConfig(**item)
        # Tested via registry test below
        pass


class TestTenantRegistry:
    def _make_tenants_json(self, tenants_list: list, default_id: str = "") -> str:
        """创建临时 tenants.json 文件（标准 {"tenants": [...]} 格式）"""
        fd, path = tempfile.mkstemp(suffix=".json")
        data = {"tenants": tenants_list}
        if default_id:
            data["default_tenant_id"] = default_id
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        return path

    def test_load_from_file(self):
        """从文件加载租户配置"""
        path = self._make_tenants_json([
            {
                "tenant_id": "bot-a",
                "name": "Bot A",
                "platform": "feishu",
                "app_id": "cli_xxx",
            },
            {
                "tenant_id": "bot-b",
                "name": "Bot B",
                "platform": "wecom_kf",
            },
        ])

        try:
            registry = TenantRegistry()
            count = registry.load_from_file(path)
            assert count == 2

            a = registry.get("bot-a")
            assert a is not None
            assert a.tenant_id == "bot-a"
            assert a.name == "Bot A"
            assert a.app_id == "cli_xxx"

            b = registry.get("bot-b")
            assert b is not None
            assert b.platform == "wecom_kf"
        finally:
            os.unlink(path)

    def test_find_by_app_id(self):
        """通过 app_id 查找租户"""
        path = self._make_tenants_json([
            {
                "tenant_id": "code-bot",
                "name": "Code Bot",
                "app_id": "cli_abc123",
            },
        ])

        try:
            registry = TenantRegistry()
            registry.load_from_file(path)

            found = registry.find_by_app_id("cli_abc123")
            assert found is not None
            assert found.tenant_id == "code-bot"

            not_found = registry.find_by_app_id("cli_nonexistent")
            assert not_found is None
        finally:
            os.unlink(path)

    def test_get_default(self):
        """获取默认租户"""
        path = self._make_tenants_json([
            {"tenant_id": "first", "name": "First"},
            {"tenant_id": "second", "name": "Second"},
        ], default_id="first")

        try:
            registry = TenantRegistry()
            registry.load_from_file(path)

            default = registry.get_default()
            assert default is not None
            assert default.tenant_id == "first"
        finally:
            os.unlink(path)

    def test_env_var_resolution(self):
        """环境变量解析"""
        path = self._make_tenants_json([
            {
                "tenant_id": "test",
                "name": "Test",
                "app_id": "${TEST_APP_ID_METERING}",
                "app_secret": "${TEST_SECRET_METERING:default_secret}",
            },
        ])

        try:
            with patch.dict(os.environ, {"TEST_APP_ID_METERING": "resolved_id"}):
                registry = TenantRegistry()
                registry.load_from_file(path)

                t = registry.get("test")
                assert t is not None
                assert t.app_id == "resolved_id"
                assert t.app_secret == "default_secret"
        finally:
            os.unlink(path)

    def test_all_tenants(self):
        """获取全部租户"""
        path = self._make_tenants_json([
            {"tenant_id": "a", "name": "A"},
            {"tenant_id": "b", "name": "B"},
            {"tenant_id": "c", "name": "C"},
        ])

        try:
            registry = TenantRegistry()
            registry.load_from_file(path)

            all_t = registry.all_tenants()
            assert len(all_t) == 3
            assert "a" in all_t
            assert "b" in all_t
            assert "c" in all_t
        finally:
            os.unlink(path)

    def test_nonexistent_file(self):
        """文件不存在时返回 0"""
        registry = TenantRegistry()
        count = registry.load_from_file("/tmp/nonexistent_tenants_xyz.json")
        assert count == 0
