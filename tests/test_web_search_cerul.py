"""web_search 与 Cerul 自动增强集成测试"""

from __future__ import annotations

from unittest.mock import patch

from app.tools.tool_result import ToolResult


class _FakeResult:
    def __init__(self, title: str, body: str, href: str):
        self.title = title
        self.body = body
        self.href = href


def test_video_evidence_query_prefers_cerul():
    from app.tools.web_search import web_search

    with patch("app.tools.web_search.cerul_search") as mock_cerul, \
            patch("app.tools.web_search._do_search") as mock_ddg:
        mock_cerul.return_value = ToolResult.success(
            "Cerul 搜索完成：1 条结果\n\n"
            "[结果 1] Demo Talk\n"
            "speaker=Demo source=youtube time=01:05-01:30\n"
            "link: https://cerul.ai/v/demo"
        )

        result = web_search(
            {"query": "What did Demis Hassabis say about AlphaFold in interviews?"}
        )

        assert result.ok
        assert "Cerul 搜索完成" in result.content
        assert "https://cerul.ai/v/demo" in result.content
        mock_cerul.assert_called_once()
        mock_ddg.assert_not_called()


def test_video_evidence_query_falls_back_to_web_when_no_hit():
    from app.tools.web_search import web_search

    with patch("app.tools.web_search.cerul_search") as mock_cerul, \
            patch("app.tools.web_search._do_search") as mock_ddg, \
            patch("app.tools.web_search.register_urls") as mock_register:
        mock_cerul.return_value = ToolResult.success("Cerul 没有找到匹配的视频片段。")
        mock_ddg.return_value = [
            _FakeResult(
                "AlphaFold article",
                "A summary article",
                "https://example.com/alphafold",
            )
        ]

        result = web_search({"query": "Demis Hassabis interview AlphaFold timestamp"})

        assert result.ok
        assert "[来源 1] AlphaFold article" in result.content
        assert "已回退网页搜索" in result.content
        mock_ddg.assert_called_once()
        mock_register.assert_called_once_with(["https://example.com/alphafold"])


def test_non_video_query_keeps_original_web_search_path():
    from app.tools.web_search import web_search

    with patch("app.tools.web_search.cerul_search") as mock_cerul, \
            patch("app.tools.web_search._do_search") as mock_ddg, \
            patch("app.tools.web_search.register_urls") as mock_register:
        mock_ddg.return_value = [
            _FakeResult(
                "Python docs",
                "Dictionary operations in Python",
                "https://docs.python.org/3/tutorial/datastructures.html",
            )
        ]

        result = web_search({"query": "python dict merge best practice"})

        assert result.ok
        assert "[来源 1] Python docs" in result.content
        mock_cerul.assert_not_called()
        mock_ddg.assert_called_once()
        mock_register.assert_called_once()
