"""Tests for customer store + approval + super admin systems"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest


# ── customer_store tests ──


class TestCustomerStore:

    @patch("app.services.customer_store.redis")
    def test_bind_customer_success(self, mock_redis):
        from app.services.customer_store import bind_customer

        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = None

        ok = bind_customer(
            external_userid="ext_user_123",
            tenant_id="test-bot",
            name="Test Company",
            platform="feishu",
            port=8105,
        )
        assert ok is True
        mock_redis.pipeline.assert_called_once()
        cmds = mock_redis.pipeline.call_args[0][0]
        assert len(cmds) == 4
        data = json.loads(cmds[0][2])
        assert data["tenant_id"] == "test-bot"
        assert data["name"] == "Test Company"

    @patch("app.services.customer_store.redis")
    def test_bind_customer_redis_unavailable(self, mock_redis):
        from app.services.customer_store import bind_customer
        mock_redis.available.return_value = False
        assert bind_customer("ext_123", "test-bot") is False

    @patch("app.services.customer_store.redis")
    def test_bind_customer_missing_params(self, mock_redis):
        from app.services.customer_store import bind_customer
        mock_redis.available.return_value = True
        assert bind_customer("", "test-bot") is False
        assert bind_customer("ext_123", "") is False

    @patch("app.services.customer_store.redis")
    def test_get_customer_found(self, mock_redis):
        from app.services.customer_store import get_customer
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = json.dumps({
            "tenant_id": "test-bot", "name": "Test Co",
        })
        result = get_customer("ext_123")
        assert result is not None
        assert result["tenant_id"] == "test-bot"

    @patch("app.services.customer_store.redis")
    def test_get_customer_not_found(self, mock_redis):
        from app.services.customer_store import get_customer
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = None
        assert get_customer("nonexistent") is None

    @patch("app.services.customer_store.redis")
    def test_get_customer_by_tenant(self, mock_redis):
        from app.services.customer_store import get_customer_by_tenant
        stored = json.dumps({"tenant_id": "test-bot", "name": "Test Co"})
        mock_redis.available.return_value = True
        mock_redis.execute.side_effect = ["ext_123", stored]
        result = get_customer_by_tenant("test-bot")
        assert result is not None
        assert result["tenant_id"] == "test-bot"

    @patch("app.services.customer_store.redis")
    def test_update_customer(self, mock_redis):
        from app.services.customer_store import update_customer
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = json.dumps({
            "tenant_id": "test-bot", "name": "Old Name",
        })
        mock_redis.pipeline.return_value = None
        ok = update_customer("ext_123", name="New Name")
        assert ok is True
        data = json.loads(mock_redis.pipeline.call_args[0][0][0][2])
        assert data["name"] == "New Name"

    @patch("app.services.customer_store.redis")
    def test_unbind_customer(self, mock_redis):
        from app.services.customer_store import unbind_customer
        mock_redis.available.return_value = True
        mock_redis.execute.side_effect = [
            json.dumps({"tenant_id": "test-bot"}), 1, 1,
        ]
        assert unbind_customer("ext_123") is True

    @patch("app.services.customer_store.redis")
    def test_list_customers_empty(self, mock_redis):
        from app.services.customer_store import list_customers
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = ["0", []]
        assert list_customers() == []


# ── super_admin tests ──


class TestSuperAdmin:

    @patch("app.services.super_admin.redis")
    def test_is_super_admin_by_id(self, mock_redis):
        from app.services.super_admin import is_super_admin, invalidate_cache
        invalidate_cache()
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = json.dumps({
            "name": "Admin",
            "identities": [
                {"platform": "wecom_kf", "user_id": "wm_steven_123", "tenant_id": "kf-steven-ai"},
            ],
        })
        assert is_super_admin("wm_steven_123") is True

    @patch("app.services.super_admin.redis")
    def test_is_super_admin_unknown_id(self, mock_redis):
        from app.services.super_admin import is_super_admin, invalidate_cache
        invalidate_cache()
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = json.dumps({
            "name": "Admin",
            "identities": [
                {"platform": "wecom_kf", "user_id": "wm_steven_123"},
            ],
        })
        assert is_super_admin("random_user_456") is False

    @patch("app.services.super_admin.redis")
    def test_is_super_admin_redis_unavailable_fallback(self, mock_redis):
        from app.services.super_admin import is_super_admin, invalidate_cache
        invalidate_cache()
        mock_redis.available.return_value = False
        # Without Redis, no identities to match
        assert is_super_admin("any_id") is False

    @patch("app.services.super_admin.redis")
    def test_add_identity(self, mock_redis):
        from app.services.super_admin import add_identity, invalidate_cache
        invalidate_cache()
        mock_redis.available.return_value = True
        mock_redis.execute.side_effect = [
            json.dumps({"name": "admin", "identities": []}),  # load
            "OK",  # save
        ]
        ok = add_identity("feishu", "ou_xxx", "code-bot", "飞书")
        assert ok is True

    @patch("app.services.super_admin.redis")
    def test_add_identity_dedup(self, mock_redis):
        from app.services.super_admin import add_identity, invalidate_cache
        invalidate_cache()
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = json.dumps({
            "name": "admin",
            "identities": [{"user_id": "ou_xxx", "platform": "feishu"}],
        })
        # Already exists, should return True without saving
        ok = add_identity("feishu", "ou_xxx")
        assert ok is True


# ── provision_approval tests ──


class TestProvisionApproval:

    @patch("app.services.provision_approval.redis")
    def test_create_request(self, mock_redis):
        from app.services.provision_approval import create_request
        mock_redis.available.return_value = True
        mock_redis.pipeline.return_value = None

        result = create_request(
            requester_id="ext_customer",
            requester_name="客户A",
            tenant_id="customer-a-bot",
            name="客户A AI助手",
            platform="feishu",
            credentials={"app_id": "xxx", "app_secret": "yyy"},
        )
        assert result is not None
        assert result["status"] == "pending"
        assert result["request_id"].startswith("req_")
        assert result["tenant_id"] == "customer-a-bot"
        # Credentials should NOT be in the main data (stored separately)
        assert "credentials" not in result
        assert result["credential_fields"] == ["app_id", "app_secret"]

    @patch("app.services.provision_approval.redis")
    def test_create_request_redis_unavailable(self, mock_redis):
        from app.services.provision_approval import create_request
        mock_redis.available.return_value = False
        assert create_request("id", "name", "tid", "n", "p", {}) is None

    @patch("app.services.provision_approval.redis")
    def test_get_request(self, mock_redis):
        from app.services.provision_approval import get_request
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = json.dumps({
            "request_id": "req_abc",
            "status": "pending",
        })
        result = get_request("req_abc")
        assert result is not None
        assert result["request_id"] == "req_abc"

    @patch("app.services.provision_approval.redis")
    def test_reject_request(self, mock_redis):
        from app.services.provision_approval import reject_request
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = json.dumps({
            "request_id": "req_abc",
            "status": "pending",
        })
        mock_redis.pipeline.return_value = None

        result = reject_request("req_abc", rejected_by="admin", reason="不符合条件")
        assert result is not None
        assert result["status"] == "rejected"
        assert result["reject_reason"] == "不符合条件"


# ── customer_ops tool security tests ──


class TestCustomerOpsToolSecurity:

    def test_bind_customer_requires_super_admin(self):
        from app.tools.customer_ops import _bind_customer
        from app.tenant.context import _current_sender, SenderContext
        # Set non-admin sender
        _current_sender.set(SenderContext(sender_id="random", is_super_admin=False))
        result = _bind_customer({"external_userid": "ext_1", "tenant_id": "t1"})
        assert result.ok is False
        assert result.code == "permission"

    def test_list_customers_requires_super_admin(self):
        from app.tools.customer_ops import _list_customers
        from app.tenant.context import _current_sender, SenderContext
        _current_sender.set(SenderContext(sender_id="random", is_super_admin=False))
        result = _list_customers({})
        assert result.ok is False
        assert result.code == "permission"

    def test_approve_requires_super_admin(self):
        from app.tools.customer_ops import _approve_provision_request
        from app.tenant.context import _current_sender, SenderContext
        _current_sender.set(SenderContext(sender_id="random", is_super_admin=False))
        result = _approve_provision_request({"request_id": "req_123"})
        assert result.ok is False
        assert result.code == "permission"

    def test_reject_requires_super_admin(self):
        from app.tools.customer_ops import _reject_provision_request
        from app.tenant.context import _current_sender, SenderContext
        _current_sender.set(SenderContext(sender_id="random", is_super_admin=False))
        result = _reject_provision_request({"request_id": "req_123"})
        assert result.ok is False
        assert result.code == "permission"

    @patch("app.services.deploy_quota.redis")
    @patch("app.services.provision_approval.redis")
    def test_request_provision_anyone_can_use(self, mock_approval_redis, mock_quota_redis):
        """request_provision 任何人都能用（不需要超管）"""
        from app.tools.customer_ops import _request_provision
        from app.tenant.context import _current_sender, _current_tenant, SenderContext
        _current_sender.set(SenderContext(
            sender_id="customer_abc", sender_name="张三", is_super_admin=False,
        ))
        # Set tenant context with deploy_free_quota
        from unittest.mock import MagicMock
        tenant = MagicMock()
        tenant.tenant_id = "kf-steven-ai"
        tenant.deploy_free_quota = 1
        _current_tenant.set(tenant)

        mock_approval_redis.available.return_value = True
        mock_approval_redis.pipeline.return_value = None
        # Deploy quota: user has remaining quota
        mock_quota_redis.available.return_value = True
        mock_quota_redis.execute.return_value = None  # new user, no record
        mock_quota_redis.pipeline.return_value = None

        result = _request_provision({
            "tenant_id": "test-bot",
            "name": "Test Bot",
            "platform": "feishu",
            "credentials_json": '{"app_id": "x"}',
        })
        assert result.ok is True
        assert "已提交" in result.content


# ── sender context tests ──


class TestSenderContext:

    def test_sender_context_default(self):
        from app.tenant.context import get_current_sender, SenderContext, _current_sender
        _current_sender.set(None)
        sender = get_current_sender()
        assert sender.sender_id == ""
        assert sender.is_super_admin is False

    def test_sender_context_set(self):
        from app.tenant.context import _current_sender, SenderContext, get_current_sender
        ctx = SenderContext(sender_id="test_user", sender_name="Test", is_super_admin=True)
        _current_sender.set(ctx)
        sender = get_current_sender()
        assert sender.sender_id == "test_user"
        assert sender.is_super_admin is True
