"""自我修复边界测试

验证 auto-fix 的 allowlist 策略:
- 只能写入 app/tools/ 和 app/knowledge/
- 基础设施层文件被拦截
- 读取不受限制
"""

from unittest.mock import patch, MagicMock

from app.services.error_log import ErrorRecord
from app.services.auto_fix import _ALLOWED_WRITE_PATHS, _execute_tool


class TestAllowlist:
    def test_allowed_paths(self):
        """允许写入的路径"""
        assert "app/tools/" in _ALLOWED_WRITE_PATHS
        assert "app/knowledge/" in _ALLOWED_WRITE_PATHS

    def test_write_to_tools_allowed(self):
        """写入 app/tools/ 应放行"""
        mock_handler = MagicMock(return_value="OK")
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": "app/tools/my_tool.py", "content": "# code"},
            tool_map,
        )
        mock_handler.assert_called_once()

    def test_write_to_knowledge_allowed(self):
        """写入 app/knowledge/ 应放行"""
        mock_handler = MagicMock(return_value="OK")
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": "app/knowledge/notes.md", "content": "# notes"},
            tool_map,
        )
        mock_handler.assert_called_once()

    def test_write_to_services_blocked(self):
        """写入 app/services/ 应被拦截"""
        mock_handler = MagicMock()
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": "app/services/gemini_provider.py", "content": "# hacked"},
            tool_map,
        )
        assert "不允许修改" in result
        mock_handler.assert_not_called()

    def test_write_to_main_blocked(self):
        """写入 app/main.py 应被拦截"""
        mock_handler = MagicMock()
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": "app/main.py", "content": "# hacked"},
            tool_map,
        )
        assert "不允许修改" in result
        mock_handler.assert_not_called()

    def test_write_to_config_blocked(self):
        """写入 app/config.py 应被拦截"""
        mock_handler = MagicMock()
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": "app/config.py", "content": "# hacked"},
            tool_map,
        )
        assert "不允许修改" in result

    def test_write_to_tenant_blocked(self):
        """写入 app/tenant/ 应被拦截"""
        mock_handler = MagicMock()
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": "app/tenant/config.py", "content": "# hacked"},
            tool_map,
        )
        assert "不允许修改" in result

    def test_write_to_webhook_blocked(self):
        """写入 app/webhook/ 应被拦截"""
        mock_handler = MagicMock()
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": "app/webhook/handler.py", "content": "# hacked"},
            tool_map,
        )
        assert "不允许修改" in result

    def test_edit_file_same_restrictions(self):
        """self_edit_file 也受 allowlist 限制"""
        mock_handler = MagicMock()
        tool_map = {"self_edit_file": mock_handler}

        result = _execute_tool(
            "self_edit_file",
            {"path": "app/services/kimi_coder.py", "old": "a", "new": "b"},
            tool_map,
        )
        assert "不允许修改" in result
        mock_handler.assert_not_called()

    def test_edit_tools_allowed(self):
        """self_edit_file 对 app/tools/ 放行"""
        mock_handler = MagicMock(return_value="OK")
        tool_map = {"self_edit_file": mock_handler}

        result = _execute_tool(
            "self_edit_file",
            {"path": "app/tools/web_search.py", "old": "a", "new": "b"},
            tool_map,
        )
        mock_handler.assert_called_once()

    def test_read_not_restricted(self):
        """self_read_file 读取不受 allowlist 限制"""
        mock_handler = MagicMock(return_value="file content")
        tool_map = {"self_read_file": mock_handler}

        result = _execute_tool(
            "self_read_file",
            {"path": "app/services/gemini_provider.py"},
            tool_map,
        )
        mock_handler.assert_called_once()
        assert result == "file content"

    def test_non_write_tools_not_restricted(self):
        """非写入工具不受 allowlist 限制"""
        mock_handler = MagicMock(return_value="search results")
        tool_map = {"self_search_code": mock_handler}

        result = _execute_tool(
            "self_search_code",
            {"query": "def handle_message"},
            tool_map,
        )
        mock_handler.assert_called_once()

    def test_unknown_tool(self):
        """未知工具应返回错误"""
        result = _execute_tool("nonexistent_tool", {}, {})
        assert "unknown tool" in result

    def test_tool_exception_handled(self):
        """工具执行异常应被捕获"""
        mock_handler = MagicMock(side_effect=ValueError("bad input"))
        tool_map = {"self_read_file": mock_handler}

        result = _execute_tool("self_read_file", {"path": "x.py"}, tool_map)
        assert "异常" in result

    def test_write_to_scripts_blocked(self):
        """写入 scripts/ 应被拦截"""
        mock_handler = MagicMock()
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": "scripts/tenant_ctl.py", "content": "# hacked"},
            tool_map,
        )
        assert "不允许修改" in result

    def test_write_to_dockerfile_blocked(self):
        """写入 Dockerfile 应被拦截"""
        mock_handler = MagicMock()
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": "Dockerfile", "content": "FROM evil"},
            tool_map,
        )
        assert "不允许修改" in result

    def test_write_to_github_workflows_blocked(self):
        """写入 .github/workflows/ 应被拦截"""
        mock_handler = MagicMock()
        tool_map = {"self_write_file": mock_handler}

        result = _execute_tool(
            "self_write_file",
            {"path": ".github/workflows/deploy.yml", "content": "# hacked"},
            tool_map,
        )
        assert "不允许修改" in result


class TestTransientErrorContainment:
    def test_network_timeouts_are_not_self_fixable(self):
        from app.services.auto_fix import _errors_are_transient_only

        errors = [
            ErrorRecord(
                time="2026-04-26T01:27:06",
                category="timeout",
                summary="Page.goto: Timeout 30000ms exceeded",
                detail="playwright._impl._errors.TimeoutError: Page.goto: Timeout 30000ms exceeded",
                tool_name="browser_open",
                tool_args='{"url":"https://news.qq.com/rain/a/20251222A07HXX00"}',
            ),
            ErrorRecord(
                time="2026-04-26T01:57:46",
                category="api_error",
                summary="httpx.ConnectTimeout while sending wecom kf progress",
                detail="httpcore.ConnectTimeout\nhttpx.ConnectTimeout",
                tool_name="reply_text",
            ),
            ErrorRecord(
                time="2026-04-26T01:29:34",
                category="timeout",
                summary="xhs_search timed out after 120s",
                detail="app.tools.xhs_ops: xhs_ops: search '游戏开发 AI 工作流 痛点' timed out after 120s",
                tool_name="xhs_search",
            ),
        ]

        assert _errors_are_transient_only(errors)

    def test_unknown_tool_is_not_transient(self):
        from app.services.auto_fix import _errors_are_transient_only

        errors = [
            ErrorRecord(
                time="2026-04-26T02:00:00",
                category="tool_error",
                summary="unknown tool: send_feishu_message",
                detail="unknown tool: send_feishu_message",
                tool_name="send_feishu_message",
            )
        ]

        assert not _errors_are_transient_only(errors)

    def test_remote_protocol_disconnect_is_transient(self):
        from app.services.auto_fix import _classify_transient_errors, _errors_are_transient_only

        errors = [
            ErrorRecord(
                time="2026-04-26T05:39:02",
                category="api_error",
                summary="auto_fix gemini API call failed",
                detail="httpx.RemoteProtocolError: Server disconnected without sending a response.",
                tool_name="gemini",
            )
        ]

        classification = _classify_transient_errors(errors)
        assert classification.is_all_transient
        assert "remote_protocol" in classification.reasons
        assert _errors_are_transient_only(errors)

    def test_repeated_web_search_failure_is_transient(self):
        from app.services.auto_fix import _classify_transient_errors, _errors_are_transient_only

        errors = [
            ErrorRecord(
                time="2026-04-26T05:35:58",
                category="tool_error",
                summary="web_search 连续失败3次，考虑换一种方式或工具",
                detail="tool_tracker: auto-lesson for web_search: web_search 连续失败3次",
                tool_name="web_search",
            )
        ]

        classification = _classify_transient_errors(errors)
        assert classification.is_all_transient
        assert "web_search" in classification.reasons
        assert _errors_are_transient_only(errors)
