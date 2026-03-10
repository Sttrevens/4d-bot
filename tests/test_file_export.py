"""file_export.py 单元测试

覆盖:
- CSV 生成（纯文本 CSV + JSON 数组转 CSV）
- JSON 格式化
- 平台检测（飞书拒绝、企微放行）
- 文件大小限制
- 上传/发送流程 mock
"""

import json
import contextvars
from unittest.mock import patch, MagicMock

import pytest

from app.tools.file_export import (
    _generate_csv,
    _generate_json,
    _export_file,
    _MAX_FILE_SIZE,
)
from app.tools.tool_result import ToolResult
from app.tools.feishu_api import _current_user_open_id


# ── CSV 生成 ──

class TestGenerateCsv:
    def test_plain_csv_passthrough(self):
        """纯 CSV 文本直接返回（加 BOM）"""
        content = "name,age\nAlice,30\nBob,25"
        result = _generate_csv(content)
        # UTF-8 BOM
        assert result.startswith(b"\xef\xbb\xbf")
        decoded = result.decode("utf-8-sig")
        assert "name,age" in decoded
        assert "Alice,30" in decoded

    def test_json_array_to_csv(self):
        """JSON 数组自动转 CSV"""
        data = [
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 25},
        ]
        content = json.dumps(data)
        result = _generate_csv(content)
        decoded = result.decode("utf-8-sig")
        assert "name,age" in decoded
        assert "Alice,30" in decoded
        assert "Bob,25" in decoded

    def test_invalid_json_falls_back_to_text(self):
        """无效 JSON 回退为纯文本"""
        content = "[not valid json"
        result = _generate_csv(content)
        decoded = result.decode("utf-8-sig")
        assert "[not valid json" in decoded

    def test_empty_content(self):
        result = _generate_csv("")
        assert result == b"\xef\xbb\xbf"


# ── JSON 生成 ──

class TestGenerateJson:
    def test_formats_json(self):
        content = '{"a":1,"b":2}'
        result = _generate_json(content)
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": 2}
        # 应该有缩进
        assert b"  " in result

    def test_invalid_json_passthrough(self):
        content = "not json at all"
        result = _generate_json(content)
        assert result == b"not json at all"


# ── Helper: 设置 contextvars 并 mock tenant ──

def _setup_context(platform="wecom", sender_id="user123"):
    """设置 contextvar 并返回 mock tenant"""
    _current_user_open_id.set(sender_id)
    tenant = MagicMock()
    tenant.platform = platform
    tenant.tenant_id = "test-tenant"
    tenant.wecom_corpid = "corp123"
    tenant.wecom_corpsecret = "secret123"
    tenant.wecom_agent_id = 1000001
    tenant.wecom_kf_secret = "kf_secret"
    tenant.wecom_kf_open_kfid = "kf_123"
    return tenant


# ── 平台检测 ──

class TestExportFilePlatformCheck:
    @patch("app.tools.file_export.get_current_tenant")
    def test_feishu_rejected(self, mock_get_tenant):
        """飞书平台应被拒绝，提示用 create_feishu_doc"""
        mock_get_tenant.return_value = _setup_context(platform="feishu", sender_id="user123")
        result = _export_file("report.csv", "a,b\n1,2")
        assert not result.ok
        assert "create_feishu_doc" in result.content

    @patch("app.tools.file_export.get_current_tenant")
    def test_no_sender_id(self, mock_get_tenant):
        """无法确定用户时应报错"""
        mock_get_tenant.return_value = _setup_context(platform="wecom", sender_id="")
        result = _export_file("report.csv", "a,b\n1,2")
        assert not result.ok
        assert "无法确定" in result.content

    @patch("app.tools.file_export.get_current_tenant")
    def test_unknown_platform(self, mock_get_tenant):
        """未知平台应报错"""
        mock_get_tenant.return_value = _setup_context(platform="slack", sender_id="user123")
        result = _export_file("report.csv", "a,b\n1,2")
        assert not result.ok
        assert "不支持" in result.content

    @patch("app.tools.file_export.get_current_tenant")
    def test_file_too_large(self, mock_get_tenant):
        """超大文件应报错"""
        mock_get_tenant.return_value = _setup_context(platform="wecom", sender_id="user123")
        huge_content = "x" * (_MAX_FILE_SIZE + 1)
        result = _export_file("big.txt", huge_content)
        assert not result.ok
        assert "太大" in result.content


# ── 上传发送流程 ──

class TestExportFileFlow:
    @patch("app.tools.file_export._send_file_wecom")
    @patch("app.tools.file_export._upload_media_sync")
    @patch("app.tools.file_export._get_token_sync")
    @patch("app.tools.file_export.get_current_tenant")
    def test_wecom_full_flow(self, mock_get_tenant,
                              mock_token, mock_upload, mock_send):
        """企微内部应用：完整流程 token→upload→send"""
        mock_get_tenant.return_value = _setup_context("wecom", "user_abc")
        mock_token.return_value = "fake_token"
        mock_upload.return_value = "media_123"
        mock_send.return_value = {"errcode": 0, "errmsg": "ok"}

        result = _export_file("data.csv", "a,b\n1,2")
        assert result.ok
        assert "data.csv" in result.content
        assert "已发送" in result.content

        mock_token.assert_called_once_with("corp123", "secret123")
        mock_upload.assert_called_once()
        mock_send.assert_called_once_with("fake_token", "user_abc", "media_123", 1000001)

    @patch("app.tools.file_export._send_file_wecom_kf")
    @patch("app.tools.file_export._upload_media_sync")
    @patch("app.tools.file_export._get_token_sync")
    @patch("app.tools.file_export.get_current_tenant")
    def test_wecom_kf_full_flow(self, mock_get_tenant,
                                 mock_token, mock_upload, mock_send):
        """微信客服：完整流程"""
        mock_get_tenant.return_value = _setup_context("wecom_kf", "ext_user_xyz")
        mock_token.return_value = "fake_kf_token"
        mock_upload.return_value = "media_456"
        mock_send.return_value = {"errcode": 0, "errmsg": "ok"}

        result = _export_file("report.md", "# Title\n\nSome content")
        assert result.ok

        mock_token.assert_called_once_with("corp123", "kf_secret", cache_key="corp123:kf")
        mock_send.assert_called_once_with("fake_kf_token", "ext_user_xyz", "media_456", "kf_123")

    @patch("app.tools.file_export._upload_media_sync")
    @patch("app.tools.file_export._get_token_sync")
    @patch("app.tools.file_export.get_current_tenant")
    def test_upload_failure(self, mock_get_tenant, mock_token, mock_upload):
        """上传失败应报错"""
        mock_get_tenant.return_value = _setup_context("wecom", "user_abc")
        mock_token.return_value = "fake_token"
        mock_upload.return_value = ""

        result = _export_file("data.csv", "a,b\n1,2")
        assert not result.ok
        assert "上传失败" in result.content

    @patch("app.tools.file_export._send_file_wecom")
    @patch("app.tools.file_export._upload_media_sync")
    @patch("app.tools.file_export._get_token_sync")
    @patch("app.tools.file_export.get_current_tenant")
    def test_send_failure(self, mock_get_tenant,
                           mock_token, mock_upload, mock_send):
        """发送失败应报错"""
        mock_get_tenant.return_value = _setup_context("wecom", "user_abc")
        mock_token.return_value = "fake_token"
        mock_upload.return_value = "media_123"
        mock_send.return_value = {"errcode": 40001, "errmsg": "invalid token"}

        result = _export_file("data.csv", "a,b\n1,2")
        assert not result.ok
        assert "发送失败" in result.content

    @patch("app.tools.file_export._send_file_wecom")
    @patch("app.tools.file_export._upload_media_sync")
    @patch("app.tools.file_export._get_token_sync")
    @patch("app.tools.file_export.get_current_tenant")
    def test_json_file_export(self, mock_get_tenant,
                               mock_token, mock_upload, mock_send):
        """JSON 文件导出"""
        mock_get_tenant.return_value = _setup_context("wecom", "user_abc")
        mock_token.return_value = "fake_token"
        mock_upload.return_value = "media_789"
        mock_send.return_value = {"errcode": 0, "errmsg": "ok"}

        result = _export_file("data.json", '{"key":"value"}')
        assert result.ok

        # 检查 upload 时传入了格式化的 JSON
        upload_call_args = mock_upload.call_args
        file_bytes = upload_call_args[0][1]
        assert b'"key": "value"' in file_bytes  # 格式化缩进


# ── 工具注册 ──

class TestToolRegistration:
    def test_tool_definitions_format(self):
        from app.tools.file_export import TOOL_DEFINITIONS, TOOL_MAP
        assert len(TOOL_DEFINITIONS) == 1
        td = TOOL_DEFINITIONS[0]
        assert td["name"] == "export_file"
        assert "input_schema" in td
        assert "filename" in td["input_schema"]["properties"]
        assert "content" in td["input_schema"]["properties"]
        assert td["name"] in TOOL_MAP

    def test_tool_in_kimi_coder_all_map(self):
        """export_file 应在全局工具 map 中"""
        from app.services.base_agent import ALL_TOOL_MAP
        assert "export_file" in ALL_TOOL_MAP
