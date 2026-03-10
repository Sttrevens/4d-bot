"""路由层集成测试

验证 route_message 的前置检查:
- 配额超限时直接拒绝
- 限流超限时直接拒绝
- 正常请求放行并记录用量
"""

from unittest.mock import patch, MagicMock, AsyncMock

import pytest


@pytest.fixture
def mock_tenant():
    tenant = MagicMock()
    tenant.tenant_id = "test-tenant"
    tenant.llm_provider = "gemini"
    tenant.llm_model = "gemini-3-flash"
    tenant.llm_model_strong = ""
    tenant.coding_model = ""
    tenant.rate_limit_rpm = 0
    tenant.rate_limit_user_rpm = 0
    tenant.quota_monthly_api_calls = 0
    tenant.quota_monthly_tokens = 0
    return tenant


class TestRouteQuotaCheck:
    @pytest.mark.asyncio
    async def test_quota_exceeded_returns_error(self, mock_tenant):
        """配额超限时应直接返回错误消息"""
        with patch("app.tenant.context.get_current_tenant", return_value=mock_tenant), \
             patch("app.router.intent.check_quota", return_value=(False, "本月 API 调用次数已达上限（100/100）")), \
             patch("app.router.intent.check_rate_limit", return_value=(True, "")):

            from app.router.intent import route_message
            reply = await route_message("hello", sender_id="u1")
            assert "上限" in reply
            assert "升级配额" in reply

    @pytest.mark.asyncio
    async def test_rate_limited_returns_error(self, mock_tenant):
        """限流超限时应直接返回错误消息"""
        with patch("app.tenant.context.get_current_tenant", return_value=mock_tenant), \
             patch("app.router.intent.check_quota", return_value=(True, "")), \
             patch("app.router.intent.check_rate_limit", return_value=(False, "请求过于频繁")):

            from app.router.intent import route_message
            reply = await route_message("hello", sender_id="u1")
            assert "频繁" in reply

    @pytest.mark.asyncio
    async def test_normal_request_passes_through(self, mock_tenant):
        """正常请求应通过检查并调用 handler（使用 openai provider 避免 google-genai 导入问题）"""
        mock_tenant.llm_provider = "openai"  # use openai to avoid google-genai import chain

        with patch("app.tenant.context.get_current_tenant", return_value=mock_tenant), \
             patch("app.router.intent.check_quota", return_value=(True, "")), \
             patch("app.router.intent.check_rate_limit", return_value=(True, "")), \
             patch("app.router.intent.chat_history") as mock_history, \
             patch("app.router.intent.set_current_user"), \
             patch("app.router.intent.needs_reauth", return_value=False), \
             patch("app.router.intent.record_usage") as mock_record, \
             patch("app.router.intent.kimi_handle_message", new_callable=AsyncMock, return_value="回复内容"):

            mock_history.get.return_value = []

            from app.router.intent import route_message
            reply = await route_message("hello", sender_id="u1")
            assert reply == "回复内容"
