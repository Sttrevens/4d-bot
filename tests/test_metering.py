"""计量系统测试

测试 UsageRecord 创建、record_usage 容错、check_quota 逻辑。
Redis 不可用时的 fail-open 行为。
"""

from unittest.mock import patch, MagicMock
import time

from app.services.metering import (
    UsageRecord,
    record_usage,
    check_quota,
    get_usage_summary,
    get_daily_breakdown,
)


class TestUsageRecord:
    def test_default_values(self):
        rec = UsageRecord(tenant_id="t1")
        assert rec.tenant_id == "t1"
        assert rec.api_calls == 1
        assert rec.input_tokens == 0
        assert rec.output_tokens == 0
        assert rec.tool_calls == 0
        assert rec.timestamp > 0

    def test_custom_values(self):
        rec = UsageRecord(
            tenant_id="t1",
            sender_id="u1",
            model="gemini-3-flash",
            provider="gemini",
            input_tokens=100,
            output_tokens=50,
            tool_calls=3,
            rounds=5,
            latency_ms=1200,
        )
        assert rec.input_tokens == 100
        assert rec.output_tokens == 50
        assert rec.tool_calls == 3
        assert rec.latency_ms == 1200


class TestRecordUsage:
    @patch("app.services.metering.redis")
    def test_record_writes_to_redis(self, mock_redis):
        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = [None] * 20

        rec = UsageRecord(
            tenant_id="test-tenant",
            sender_id="user123",
            input_tokens=100,
            output_tokens=50,
        )
        record_usage(rec)

        mock_redis.pipeline.assert_called_once()
        commands = mock_redis.pipeline.call_args[0][0]
        assert len(commands) >= 6
        assert commands[0][0] == "HINCRBY"
        assert "input_tokens" in commands[0][2]

    @patch("app.services.metering.redis")
    def test_record_skips_when_redis_unavailable(self, mock_redis):
        mock_redis.available.return_value = False
        rec = UsageRecord(tenant_id="t1", input_tokens=100)
        record_usage(rec)
        mock_redis.pipeline.assert_not_called()

    @patch("app.services.metering.redis")
    def test_record_skips_empty_tenant(self, mock_redis):
        rec = UsageRecord(tenant_id="", input_tokens=100)
        record_usage(rec)
        mock_redis.pipeline.assert_not_called()

    @patch("app.services.metering.redis")
    def test_record_handles_redis_error(self, mock_redis):
        mock_redis.available.return_value = True
        mock_redis.pipeline.side_effect = Exception("connection lost")
        rec = UsageRecord(tenant_id="t1", input_tokens=100)
        record_usage(rec)  # Should not raise


class TestCheckQuota:
    @patch("app.services.metering.redis")
    def test_no_quota_configured(self, mock_redis):
        """无配额限制时应放行"""
        mock_redis.available.return_value = True

        mock_tenant = MagicMock()
        mock_tenant.quota_monthly_api_calls = 0
        mock_tenant.quota_monthly_tokens = 0

        with patch("app.tenant.registry.tenant_registry") as mock_reg:
            mock_reg.get.return_value = mock_tenant
            allowed, reason = check_quota("t1")
            assert allowed is True
            assert reason == ""

    @patch("app.services.metering.redis")
    def test_quota_exceeded_api_calls(self, mock_redis):
        """API 调用次数超限"""
        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = ["100", "5000", "3000"]

        mock_tenant = MagicMock()
        mock_tenant.quota_monthly_api_calls = 50
        mock_tenant.quota_monthly_tokens = 0

        with patch("app.tenant.registry.tenant_registry") as mock_reg:
            mock_reg.get.return_value = mock_tenant
            allowed, reason = check_quota("t1")
            assert allowed is False
            assert "100" in reason
            assert "50" in reason

    @patch("app.services.metering.redis")
    def test_quota_exceeded_tokens(self, mock_redis):
        """Token 用量超限"""
        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = ["10", "500000", "300000"]

        mock_tenant = MagicMock()
        mock_tenant.quota_monthly_api_calls = 0
        mock_tenant.quota_monthly_tokens = 100000

        with patch("app.tenant.registry.tenant_registry") as mock_reg:
            mock_reg.get.return_value = mock_tenant
            allowed, reason = check_quota("t1")
            assert allowed is False
            assert "token" in reason

    @patch("app.services.metering.redis")
    def test_quota_within_limits(self, mock_redis):
        """用量在限额内"""
        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = ["10", "5000", "3000"]

        mock_tenant = MagicMock()
        mock_tenant.quota_monthly_api_calls = 100
        mock_tenant.quota_monthly_tokens = 1000000

        with patch("app.tenant.registry.tenant_registry") as mock_reg:
            mock_reg.get.return_value = mock_tenant
            allowed, reason = check_quota("t1")
            assert allowed is True

    @patch("app.services.metering.redis")
    def test_fail_open_on_redis_error(self, mock_redis):
        mock_redis.available.return_value = True
        mock_redis.pipeline.side_effect = Exception("redis down")
        allowed, reason = check_quota("t1")
        assert allowed is True

    def test_fail_open_when_redis_unavailable(self):
        with patch("app.services.metering.redis") as mock_redis:
            mock_redis.available.return_value = False
            allowed, reason = check_quota("t1")
            assert allowed is True


class TestGetUsageSummary:
    @patch("app.services.metering.redis")
    def test_returns_parsed_data(self, mock_redis):
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = [
            "input_tokens", "1000",
            "output_tokens", "500",
            "api_calls", "10",
        ]
        result = get_usage_summary("t1", "2026-02")
        assert result["input_tokens"] == 1000
        assert result["output_tokens"] == 500
        assert result["api_calls"] == 10

    @patch("app.services.metering.redis")
    def test_returns_empty_when_no_data(self, mock_redis):
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = None
        result = get_usage_summary("t1")
        assert result == {}

    @patch("app.services.metering.redis")
    def test_returns_empty_when_unavailable(self, mock_redis):
        mock_redis.available.return_value = False
        result = get_usage_summary("t1")
        assert result == {}
