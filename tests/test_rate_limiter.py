"""限流器测试

测试滑动窗口限流逻辑、fail-open 行为、租户/用户级限流。
"""

from unittest.mock import patch, MagicMock

from app.services.rate_limiter import check_rate_limit, DEFAULT_TENANT_RPM, DEFAULT_USER_RPM


class TestCheckRateLimit:
    @patch("app.services.rate_limiter.redis")
    def test_allows_within_limit(self, mock_redis):
        """未超限时应放行"""
        mock_redis.available.return_value = True
        # ZREMRANGEBYSCORE result, ZCARD result (count=5), ZADD result, EXPIRE result
        mock_redis.pipeline.return_value = [0, 5, 1, 1]

        allowed, reason = check_rate_limit("t1", "u1")
        assert allowed is True
        assert reason == ""

    @patch("app.services.rate_limiter.redis")
    def test_blocks_tenant_over_limit(self, mock_redis):
        """租户级超限应拒绝"""
        mock_redis.available.return_value = True
        # ZCARD returns count >= DEFAULT_TENANT_RPM
        mock_redis.pipeline.return_value = [0, DEFAULT_TENANT_RPM, 1, 1]

        allowed, reason = check_rate_limit("t1")
        assert allowed is False
        assert "租户限额" in reason

    @patch("app.services.rate_limiter.redis")
    def test_blocks_user_over_limit(self, mock_redis):
        """用户级超限应拒绝"""
        mock_redis.available.return_value = True
        # First pipeline (tenant): count=5, within limit
        # Second pipeline (user): count >= DEFAULT_USER_RPM
        mock_redis.pipeline.side_effect = [
            [0, 5, 1, 1],                    # tenant: OK
            [0, DEFAULT_USER_RPM, 1, 1],     # user: exceeded
        ]

        allowed, reason = check_rate_limit("t1", "u1")
        assert allowed is False
        assert "个人限额" in reason

    @patch("app.services.rate_limiter.redis")
    def test_custom_limits(self, mock_redis):
        """自定义限额"""
        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = [0, 5, 1, 1]  # 5 requests in window

        # Custom limit of 3 → should block
        allowed, reason = check_rate_limit("t1", tenant_rpm=3)
        # The pipeline returns ZCARD=5 which is >= 3
        assert allowed is False

    @patch("app.services.rate_limiter.redis")
    def test_fail_open_redis_unavailable(self, mock_redis):
        """Redis 不可用时放行"""
        mock_redis.available.return_value = False

        allowed, reason = check_rate_limit("t1", "u1")
        assert allowed is True
        assert reason == ""

    @patch("app.services.rate_limiter.redis")
    def test_fail_open_redis_error(self, mock_redis):
        """Redis 出错时放行"""
        mock_redis.available.return_value = True
        mock_redis.pipeline.side_effect = Exception("connection lost")

        allowed, reason = check_rate_limit("t1", "u1")
        assert allowed is True

    def test_empty_tenant_id(self):
        """空租户 ID 放行"""
        allowed, reason = check_rate_limit("")
        assert allowed is True

    @patch("app.services.rate_limiter.redis")
    def test_no_user_check_when_sender_empty(self, mock_redis):
        """无 sender_id 时只检查 tenant 级"""
        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = [0, 1, 1, 1]  # count=1, within limit

        allowed, reason = check_rate_limit("t1", sender_id="")
        assert allowed is True
        # pipeline should only be called once (tenant only)
        assert mock_redis.pipeline.call_count == 1
