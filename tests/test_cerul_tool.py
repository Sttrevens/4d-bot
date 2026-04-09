"""Cerul 工具测试

覆盖：
- API key 缺失时报错
- search 成功返回格式化内容并注册来源 URL
- search API 错误透传
- usage 成功返回摘要
- 工具已挂载到全局 TOOL MAP
"""

from __future__ import annotations

from unittest.mock import patch


class TestCerulSearch:
    @patch.dict("os.environ", {}, clear=True)
    def test_search_requires_api_key(self):
        from app.tools.cerul_ops import cerul_search

        result = cerul_search({"query": "Sam Altman AI safety"})
        assert not result.ok
        assert result.code == "invalid_param"
        assert "CERUL_API_KEY" in result.content

    @patch.dict("os.environ", {"CERUL_API_KEY": "cerul_test_key"}, clear=True)
    @patch("app.tools.cerul_ops.register_urls")
    @patch("app.tools.cerul_ops._cerul_request")
    def test_search_success_formats_results(self, mock_request, mock_register_urls):
        from app.tools.cerul_ops import cerul_search

        mock_request.return_value = (
            200,
            {
                "results": [
                    {
                        "title": "The Future of OpenAI",
                        "url": "https://cerul.ai/v/abc123",
                        "speaker": "Sam Altman",
                        "source": "youtube",
                        "score": 0.78,
                        "snippet": "Compute is a strategic resource for AI progress.",
                        "timestamp_start": 2104.0,  # 35:04
                        "timestamp_end": 2166.0,    # 36:06
                    }
                ],
                "credits_used": 1,
                "credits_remaining": 99,
                "request_id": "req_aaaaaaaaaaaaaaaaaaaaaaaa",
            },
            "req_aaaaaaaaaaaaaaaaaaaaaaaa",
        )

        result = cerul_search(
            {
                "query": 'In which interview did Sam Altman mention "compute is the new oil"?',
                "max_results": 3,
            }
        )

        assert result.ok
        assert "The Future of OpenAI" in result.content
        assert "Sam Altman" in result.content
        assert "35:04-36:06" in result.content
        assert "https://cerul.ai/v/abc123" in result.content
        assert "credits_used=1" in result.content
        assert "credits_remaining=99" in result.content
        mock_register_urls.assert_called_once_with(["https://cerul.ai/v/abc123"])

    @patch.dict("os.environ", {"CERUL_API_KEY": "cerul_test_key"}, clear=True)
    @patch("app.tools.cerul_ops._cerul_request")
    def test_search_api_error(self, mock_request):
        from app.tools.cerul_ops import cerul_search

        mock_request.return_value = (
            401,
            {"error": {"code": "unauthorized", "message": "Invalid API key"}},
            "req_bbbbbbbbbbbbbbbbbbbbbbbb",
        )

        result = cerul_search({"query": "Dario responsible scaling"})
        assert not result.ok
        assert result.code == "api_error"
        assert "Invalid API key" in result.content


class TestCerulUsage:
    @patch.dict("os.environ", {"CERUL_API_KEY": "cerul_test_key"}, clear=True)
    @patch("app.tools.cerul_ops._cerul_request")
    def test_usage_success(self, mock_request):
        from app.tools.cerul_ops import cerul_usage

        mock_request.return_value = (
            200,
            {
                "tier": "free",
                "credits_used": 18,
                "credits_remaining": 82,
                "daily_free_remaining": 7,
                "daily_free_limit": 10,
                "rate_limit_per_sec": 1,
            },
            "req_cccccccccccccccccccccccc",
        )

        result = cerul_usage({})
        assert result.ok
        assert "tier=free" in result.content
        assert "credits_remaining=82" in result.content
        assert "daily_free_remaining=7/10" in result.content


def test_cerul_tool_registered_in_global_map():
    from app.services.base_agent import ALL_TOOL_MAP

    assert "cerul_search" in ALL_TOOL_MAP
    assert "cerul_usage" in ALL_TOOL_MAP
