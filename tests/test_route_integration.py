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

    @pytest.mark.asyncio
    async def test_hermes_runtime_enabled_returns_runtime_reply(self, mock_tenant):
        """显式启用 Hermes runtime 时，route_message 走 runtime adapter。"""
        from app.hermes_runtime.types import RuntimeResponse

        mock_tenant.llm_provider = "openai"
        mock_tenant.platform = "feishu"
        mock_tenant.tools_enabled = ["think"]
        mock_tenant.allowed_users = []
        mock_tenant.trial_enabled = False
        mock_tenant.quota_user_tokens_6h = 0
        mock_tenant.agent_runtime = "hermes_sidecar"
        mock_tenant.hermes_runtime_enabled = True
        mock_tenant.hermes_runtime_shadow = False
        mock_tenant.hermes_runtime_rollout_percent = 100
        mock_tenant.hermes_runtime_fallback_to_legacy = True
        mock_tenant.hermes_runtime_profile = "default"
        mock_tenant.hermes_code_execution_backend = "none"
        mock_tenant.mcp_enabled = False

        with patch("app.tenant.context.get_current_tenant", return_value=mock_tenant), \
             patch("app.router.intent.check_quota", return_value=(True, "")), \
             patch("app.router.intent.check_rate_limit", return_value=(True, "")), \
             patch("app.router.intent.chat_history") as mock_history, \
             patch("app.router.intent.set_current_user"), \
             patch("app.router.intent.record_usage"), \
             patch("app.hermes_runtime.observability.record_runtime_event") as mock_runtime_event, \
             patch("app.hermes_runtime.client.HermesRuntimeClient.run_turn",
                   new_callable=AsyncMock,
                   return_value=RuntimeResponse(run_id="run-1", status="completed", final_text="Hermes 回复")), \
             patch("app.router.intent.kimi_handle_message", new_callable=AsyncMock) as legacy_handler:

            mock_history.get.return_value = []

            from app.router.intent import route_message
            reply = await route_message("hello", sender_id="u1", run_id="run-1")

            assert reply == "Hermes 回复"
            legacy_handler.assert_not_awaited()
            selected = [call.args[0].to_dict() for call in mock_runtime_event.call_args_list
                        if call.args[0].event == "runtime.selected"]
            assert selected
            assert selected[0]["run_id"] == "run-1"
            assert selected[0]["tenant_id"] == "test-tenant"
            assert selected[0]["platform"] == "feishu"

    @pytest.mark.asyncio
    async def test_hermes_runtime_failure_falls_back_to_legacy(self, mock_tenant):
        """Hermes runtime 失败且 fallback 开启时，仍由 legacy provider 响应用户。"""
        from app.hermes_runtime.types import RuntimeResponse

        mock_tenant.llm_provider = "openai"
        mock_tenant.platform = "feishu"
        mock_tenant.tools_enabled = ["think"]
        mock_tenant.allowed_users = []
        mock_tenant.trial_enabled = False
        mock_tenant.quota_user_tokens_6h = 0
        mock_tenant.agent_runtime = "hermes_sidecar"
        mock_tenant.hermes_runtime_enabled = True
        mock_tenant.hermes_runtime_shadow = False
        mock_tenant.hermes_runtime_rollout_percent = 100
        mock_tenant.hermes_runtime_fallback_to_legacy = True
        mock_tenant.hermes_runtime_profile = "default"
        mock_tenant.hermes_code_execution_backend = "none"
        mock_tenant.mcp_enabled = False

        failed = RuntimeResponse.failed(
            run_id="run-1",
            code="runtime_unavailable",
            message="sidecar unavailable",
            retryable=True,
        )

        with patch("app.tenant.context.get_current_tenant", return_value=mock_tenant), \
             patch("app.router.intent.check_quota", return_value=(True, "")), \
             patch("app.router.intent.check_rate_limit", return_value=(True, "")), \
             patch("app.router.intent.chat_history") as mock_history, \
             patch("app.router.intent.set_current_user"), \
             patch("app.router.intent.record_usage"), \
             patch("app.hermes_runtime.observability.record_runtime_event") as mock_runtime_event, \
             patch("app.hermes_runtime.client.HermesRuntimeClient.run_turn",
                   new_callable=AsyncMock,
                   return_value=failed), \
             patch("app.router.intent.kimi_handle_message",
                   new_callable=AsyncMock,
                   return_value="legacy 回复"):

            mock_history.get.return_value = []

            from app.router.intent import route_message
            reply = await route_message("hello", sender_id="u1", run_id="run-1")

            assert reply == "legacy 回复"
