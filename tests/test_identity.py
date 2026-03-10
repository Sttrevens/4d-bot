"""身份系统测试

验证:
- verify_code 原子性（GETDEL 防 TOCTOU）
- 验证码暴力破解防护（5 次失败后锁定）
- 验证码强度（8 位字母数字）
- _resolve_identity 不再自动创建身份
- identity CRUD 基本操作
"""

from unittest.mock import patch, MagicMock, call
import json

import pytest


# ── verify_code 原子性 ──

class TestVerifyCodeAtomic:
    """verify_code 必须用 GETDEL 原子操作，防止 TOCTOU 竞争"""

    @patch("app.services.identity.redis")
    def test_getdel_used_not_get_then_del(self, mock_redis):
        """应使用 GETDEL 而非 GET+DEL"""
        mock_redis.available.return_value = True
        # 模拟 GETDEL 返回数据
        verify_data = json.dumps({
            "identity_id": "id-123",
            "from_platform": "feishu",
            "from_pid": "u1",
            "target_platform": "wecom",
            "target_pid": "u2",
            "created_at": "2026-01-01T00:00:00",
        })
        mock_redis.execute.side_effect = [
            None,  # GET attempt_key → no prior attempts
            verify_data,  # GETDEL → returns and deletes
            "OK",  # DEL attempt_key (clear on success)
        ]
        mock_redis.pipeline.return_value = [1, True]  # link_identity pipeline

        from app.services.identity import verify_code
        result = verify_code("ABC123XY", "wecom", "u2", bot_id="bot1")

        assert result is not None
        assert result["ok"] is True
        # Verify GETDEL was called (second execute call)
        calls = mock_redis.execute.call_args_list
        assert calls[1] == call("GETDEL", "identity_verify:ABC123XY")

    @patch("app.services.identity.redis")
    def test_code_not_found_returns_none(self, mock_redis):
        """验证码不存在时返回 None"""
        mock_redis.available.return_value = True
        mock_redis.execute.side_effect = [
            None,  # GET attempt_key
            None,  # GETDEL → code doesn't exist
        ]
        mock_redis.pipeline.return_value = [1, True]

        from app.services.identity import verify_code
        result = verify_code("INVALID1", "wecom", "u2")

        assert result is None

    @patch("app.services.identity.redis")
    def test_consumed_code_returns_none(self, mock_redis):
        """已被消费的验证码（GETDEL 返回 None）应返回 None"""
        mock_redis.available.return_value = True
        mock_redis.execute.side_effect = [
            None,  # GET attempt_key
            None,  # GETDEL → already consumed by concurrent request
        ]
        mock_redis.pipeline.return_value = [1, True]

        from app.services.identity import verify_code
        result = verify_code("USED0001", "wecom", "u2")

        assert result is None


# ── 暴力破解防护 ──

class TestBruteForceProtection:
    """5 次失败后锁定 10 分钟"""

    @patch("app.services.identity.redis")
    def test_blocks_after_5_failures(self, mock_redis):
        """5 次错误后应拒绝验证"""
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = "5"  # attempt count = 5

        from app.services.identity import verify_code
        result = verify_code("ANYCODE1", "wecom", "u2")

        assert result is None
        # Should not attempt GETDEL at all
        assert mock_redis.execute.call_count == 1  # only the attempt check

    @patch("app.services.identity.redis")
    def test_increments_on_failure(self, mock_redis):
        """验证失败时应递增计数器"""
        mock_redis.available.return_value = True
        mock_redis.execute.side_effect = [
            "2",   # GET attempt_key → 2 prior attempts
            None,  # GETDEL → code not found
        ]
        mock_redis.pipeline.return_value = [3, True]

        from app.services.identity import verify_code
        result = verify_code("WRONG001", "wecom", "u2")

        assert result is None
        # Should have called pipeline to INCR + EXPIRE
        mock_redis.pipeline.assert_called_once()
        pipeline_cmds = mock_redis.pipeline.call_args[0][0]
        assert pipeline_cmds[0][0] == "INCR"
        assert pipeline_cmds[1][0] == "EXPIRE"


# ── 验证码强度 ──

class TestCodeStrength:
    def test_code_is_8_chars(self):
        """验证码应为 8 位"""
        from app.services.identity import _generate_code
        code = _generate_code()
        assert len(code) == 8

    def test_code_is_alphanumeric(self):
        """验证码应包含字母和数字"""
        from app.services.identity import _generate_code
        import string
        valid_chars = set(string.digits + string.ascii_uppercase)
        for _ in range(20):
            code = _generate_code()
            assert all(c in valid_chars for c in code)


# ── _resolve_identity 不自动创建 ──

class TestResolveIdentityNoAutoCreate:
    """_resolve_identity 不应自动为新用户创建 identity。

    直接验证 intent.py 源码不含 create_identity 调用。
    """

    def test_no_create_identity_import_in_resolve(self):
        """_resolve_identity 不应导入或调用 create_identity"""
        import inspect
        # 读取 _resolve_identity 源码，确认不含 create_identity
        # 这样避免复杂的模块 import chain mock
        src_path = "app/router/intent.py"
        with open(src_path, "r") as f:
            source = f.read()

        # 找到 _resolve_identity 函数体
        start = source.find("def _resolve_identity(")
        assert start != -1, "_resolve_identity function not found"
        # 找到下一个顶级 def 或文件末尾
        next_def = source.find("\ndef ", start + 1)
        func_body = source[start:next_def] if next_def != -1 else source[start:]

        assert "create_identity" not in func_body, \
            "_resolve_identity should NOT call create_identity (auto-create without consent)"

    def test_resolve_identity_sets_empty_identity_for_new_user(self):
        """确认 _resolve_identity 在无 identity 时设置空 identity_id"""
        import inspect
        src_path = "app/router/intent.py"
        with open(src_path, "r") as f:
            source = f.read()

        start = source.find("def _resolve_identity(")
        next_def = source.find("\ndef ", start + 1)
        func_body = source[start:next_def] if next_def != -1 else source[start:]

        # 应使用 resolve_sender（只查找，不创建）
        assert "resolve_sender" in func_body
        # identity_id or "" — 无 identity 时应为空字符串
        assert 'identity_id=identity_id or ""' in func_body


# ── 基本 CRUD ──

class TestIdentityCRUD:
    @patch("app.services.identity.redis")
    def test_create_identity(self, mock_redis):
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = None  # find_identity returns None
        mock_redis.pipeline.return_value = ["OK", "OK", 1]

        from app.services.identity import create_identity
        iid = create_identity("张三", "feishu", "u-feishu-1", bot_id="bot1")

        assert iid is not None
        assert len(iid) == 36  # UUID format

    @patch("app.services.identity.redis")
    def test_create_identity_returns_existing(self, mock_redis):
        """已关联的用户不应重复创建"""
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = "existing-id"  # find_identity

        from app.services.identity import create_identity
        iid = create_identity("张三", "feishu", "u1")

        assert iid == "existing-id"
        mock_redis.pipeline.assert_not_called()

    @patch("app.services.identity.redis")
    def test_find_identity(self, mock_redis):
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = "id-789"

        from app.services.identity import find_identity
        assert find_identity("feishu", "u1") == "id-789"

    @patch("app.services.identity.redis")
    def test_find_identity_not_found(self, mock_redis):
        mock_redis.available.return_value = True
        mock_redis.execute.return_value = None

        from app.services.identity import find_identity
        assert find_identity("feishu", "u999") is None

    @patch("app.services.identity.redis")
    def test_redis_unavailable_returns_none(self, mock_redis):
        mock_redis.available.return_value = False

        from app.services.identity import create_identity, find_identity, verify_code
        assert create_identity("test", "feishu", "u1") is None
        assert find_identity("feishu", "u1") is None
        assert verify_code("CODE1234", "wecom", "u2") is None
