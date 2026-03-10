"""Tests for deploy quota system"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest


class TestCheckDeployQuota:

    @patch("app.services.deploy_quota.redis")
    def test_new_user_has_quota(self, mock_redis):
        from app.services.deploy_quota import check_deploy_quota
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = None  # no existing record
        result = check_deploy_quota("kf-steven-ai", "user_001", free_deploys=1)
        assert result["allowed"] is True
        assert result["remaining"] == 1
        assert result["used"] == 0

    @patch("app.services.deploy_quota.redis")
    def test_user_with_used_quota(self, mock_redis):
        from app.services.deploy_quota import check_deploy_quota
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = [
            "total", "1", "used", "1", "deploys", '["deployed-bot"]',
        ]
        result = check_deploy_quota("kf-steven-ai", "user_001", free_deploys=1)
        assert result["allowed"] is False
        assert result["remaining"] == 0
        assert result["used"] == 1
        assert result["deploys"] == ["deployed-bot"]

    @patch("app.services.deploy_quota.redis")
    def test_user_with_remaining_quota(self, mock_redis):
        from app.services.deploy_quota import check_deploy_quota
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = [
            "total", "3", "used", "1", "deploys", '["bot-a"]',
        ]
        result = check_deploy_quota("kf-steven-ai", "user_002", free_deploys=3)
        assert result["allowed"] is True
        assert result["remaining"] == 2
        assert result["used"] == 1

    @patch("app.services.deploy_quota.redis")
    def test_unlimited_quota(self, mock_redis):
        from app.services.deploy_quota import check_deploy_quota
        result = check_deploy_quota("kf-steven-ai", "user_001", free_deploys=0)
        assert result["allowed"] is True
        assert result["remaining"] == -1
        # Redis should not be called
        mock_redis.execute.assert_not_called()

    @patch("app.services.deploy_quota.redis")
    def test_fail_open_redis_unavailable(self, mock_redis):
        from app.services.deploy_quota import check_deploy_quota
        mock_redis.available.return_value = False
        result = check_deploy_quota("kf-steven-ai", "user_001", free_deploys=1)
        assert result["allowed"] is True

    @patch("app.services.deploy_quota.redis")
    def test_fail_open_redis_exception(self, mock_redis):
        from app.services.deploy_quota import check_deploy_quota
        mock_redis.available.return_value = True
        mock_redis.execute.side_effect = Exception("Redis error")
        result = check_deploy_quota("kf-steven-ai", "user_001", free_deploys=1)
        assert result["allowed"] is True


class TestConsumeDeployQuota:

    @patch("app.services.deploy_quota.redis")
    def test_consume_new_user(self, mock_redis):
        from app.services.deploy_quota import consume_deploy_quota
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = None  # no existing record
        mock_redis.pipeline.return_value = None

        ok = consume_deploy_quota("kf-steven-ai", "user_001", "new-bot", free_deploys=1)
        assert ok is True
        mock_redis.pipeline.assert_called_once()
        cmds = mock_redis.pipeline.call_args[0][0]
        # Should set used=1
        used_cmd = [c for c in cmds if len(c) >= 4 and c[2] == "used"]
        assert len(used_cmd) == 1
        assert used_cmd[0][3] == "1"

    @patch("app.services.deploy_quota.redis")
    def test_consume_increments_used(self, mock_redis):
        from app.services.deploy_quota import consume_deploy_quota
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = [
            "total", "3", "used", "1", "deploys", '["bot-a"]',
        ]
        mock_redis.pipeline.return_value = None

        ok = consume_deploy_quota("kf-steven-ai", "user_001", "bot-b", free_deploys=3)
        assert ok is True
        cmds = mock_redis.pipeline.call_args[0][0]
        used_cmd = [c for c in cmds if len(c) >= 4 and c[2] == "used"]
        assert used_cmd[0][3] == "2"
        deploys_cmd = [c for c in cmds if len(c) >= 4 and c[2] == "deploys"]
        assert "bot-b" in deploys_cmd[0][3]

    @patch("app.services.deploy_quota.redis")
    def test_consume_unlimited_skips(self, mock_redis):
        from app.services.deploy_quota import consume_deploy_quota
        ok = consume_deploy_quota("kf-steven-ai", "user_001", "new-bot", free_deploys=0)
        assert ok is True
        mock_redis.pipeline.assert_not_called()

    @patch("app.services.deploy_quota.redis")
    def test_consume_redis_unavailable(self, mock_redis):
        from app.services.deploy_quota import consume_deploy_quota
        mock_redis.available.return_value = False
        ok = consume_deploy_quota("kf-steven-ai", "user_001", "new-bot", free_deploys=1)
        assert ok is False


class TestSetResetQuota:

    @patch("app.services.deploy_quota.redis")
    def test_set_user_quota(self, mock_redis):
        from app.services.deploy_quota import set_user_quota
        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = None

        ok = set_user_quota("kf-steven-ai", "user_001", total=5, notes="paid customer")
        assert ok is True
        cmds = mock_redis.pipeline.call_args[0][0]
        total_cmd = [c for c in cmds if len(c) >= 4 and c[2] == "total"]
        assert total_cmd[0][3] == "5"

    @patch("app.services.deploy_quota.redis")
    def test_reset_user_quota(self, mock_redis):
        from app.services.deploy_quota import reset_user_quota
        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = None

        ok = reset_user_quota("kf-steven-ai", "user_001")
        assert ok is True
        cmds = mock_redis.pipeline.call_args[0][0]
        used_cmd = [c for c in cmds if len(c) >= 4 and c[2] == "used"]
        assert used_cmd[0][3] == "0"


class TestGetUserQuota:

    @patch("app.services.deploy_quota.redis")
    def test_get_existing_user(self, mock_redis):
        from app.services.deploy_quota import get_user_quota
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = [
            "total", "1", "used", "0", "deploys", "[]",
            "first_request", "2026-01-01T00:00:00", "notes", "trial user",
        ]

        info = get_user_quota("kf-steven-ai", "user_001")
        assert info is not None
        assert info["total"] == 1
        assert info["used"] == 0
        assert info["remaining"] == 1
        assert info["notes"] == "trial user"

    @patch("app.services.deploy_quota.redis")
    def test_get_nonexistent_user(self, mock_redis):
        from app.services.deploy_quota import get_user_quota
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = None

        info = get_user_quota("kf-steven-ai", "user_999")
        assert info is None


class TestRequestProvisionQuotaIntegration:
    """Test that request_provision properly checks deploy quota"""

    @patch("app.services.deploy_quota.redis")
    def test_request_blocked_when_quota_exceeded(self, mock_quota_redis):
        """User with no remaining quota should be blocked"""
        from app.tools.customer_ops import _request_provision

        mock_quota_redis.available.return_value = True
        mock_quota_redis.execute.return_value = [
            "total", "1", "used", "1", "deploys", '["old-bot"]',
        ]

        sender = MagicMock()
        sender.sender_id = "user_001"
        sender.is_super_admin = False

        tenant = MagicMock()
        tenant.tenant_id = "kf-steven-ai"
        tenant.deploy_free_quota = 1

        with patch("app.tenant.context.get_current_sender", return_value=sender), \
             patch("app.tenant.context.get_current_tenant", return_value=tenant):

            result = _request_provision({
                "tenant_id": "new-bot",
                "name": "New Bot",
                "platform": "feishu",
                "credentials_json": "{}",
            })

            assert result.ok is False
            assert "quota_exceeded" in (result.code or "")

    @patch("app.services.deploy_quota.redis")
    def test_superadmin_bypasses_quota(self, mock_quota_redis):
        """Super admin should bypass quota checks entirely"""
        from app.tools.customer_ops import _request_provision

        sender = MagicMock()
        sender.sender_id = "admin_001"
        sender.sender_name = "Admin"
        sender.is_super_admin = True

        tenant = MagicMock()
        tenant.tenant_id = "kf-steven-ai"
        tenant.deploy_free_quota = 1

        with patch("app.tenant.context.get_current_sender", return_value=sender), \
             patch("app.tenant.context.get_current_tenant", return_value=tenant), \
             patch("app.services.provision_approval.create_request") as mock_create:

            mock_create.return_value = {"request_id": "req_test123"}

            result = _request_provision({
                "tenant_id": "new-bot",
                "name": "New Bot",
                "platform": "feishu",
                "credentials_json": "{}",
            })

            # Should not check quota
            mock_quota_redis.execute.assert_not_called()
            assert result.ok is True
