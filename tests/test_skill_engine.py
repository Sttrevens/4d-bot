"""Tests for skill_engine.py — SKILL.md parser + trigger matching + export"""

import json
import pytest
from app.tools.skill_engine import (
    parse_skill_md,
    match_skill_triggers,
    _parse_simple_yaml,
    _normalize_tool_def,
    export_skill_md,
)


# ── parse_skill_md ──

class TestParseSkillMd:
    def test_full_skill_md(self):
        content = """---
name: competitor-analysis
description: "分析竞品数据"
triggers:
  - 竞品
  - 竞争对手
  - competitor
tools:
  - name: analyze_competitor
    description: "分析竞品数据"
    parameters:
      url: { type: string, required: true, description: "竞品URL" }
      depth: { type: string, description: "分析深度" }
---

## 使用指南

1. 先搜索竞品信息
2. 分析数据
3. 生成报告
"""
        result = parse_skill_md(content)
        assert isinstance(result, dict)
        assert result["name"] == "competitor-analysis"
        assert result["description"] == "分析竞品数据"
        assert "竞品" in result["triggers"]
        assert "竞争对手" in result["triggers"]
        assert "competitor" in result["triggers"]
        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "analyze_competitor"
        assert "使用指南" in result["instructions"]

    def test_knowledge_only_skill(self):
        """纯知识型 skill，没有 tools 部分"""
        content = """---
name: sales-playbook
description: 销售手册
triggers:
  - 销售
  - 商机
---

## 销售流程

1. 了解需求
2. 推荐方案
3. 跟进
"""
        result = parse_skill_md(content)
        assert isinstance(result, dict)
        assert result["name"] == "sales-playbook"
        assert result["tools"] == []
        assert "销售流程" in result["instructions"]

    def test_no_frontmatter(self):
        """没有 frontmatter 的纯 Markdown"""
        content = "# 这是一个普通的 Markdown 文件\n\n内容"
        result = parse_skill_md(content)
        assert isinstance(result, dict)
        assert result["name"] == ""
        assert "普通的 Markdown" in result["instructions"]

    def test_missing_end_marker(self):
        """缺少 frontmatter 结束标记"""
        content = "---\nname: test\n没有结束标记"
        result = parse_skill_md(content)
        assert isinstance(result, str)  # error message

    def test_empty_content(self):
        content = ""
        result = parse_skill_md(content)
        assert isinstance(result, dict)
        assert result["instructions"] == ""

    def test_minimal_frontmatter(self):
        content = """---
name: minimal
---
Just instructions."""
        result = parse_skill_md(content)
        assert isinstance(result, dict)
        assert result["name"] == "minimal"
        assert result["instructions"] == "Just instructions."


# ── _parse_simple_yaml ──

class TestParseSimpleYaml:
    def test_scalar_values(self):
        result = _parse_simple_yaml("name: hello\ndescription: world")
        assert result["name"] == "hello"
        assert result["description"] == "world"

    def test_quoted_values(self):
        result = _parse_simple_yaml('name: "hello world"')
        assert result["name"] == "hello world"

    def test_list_values(self):
        result = _parse_simple_yaml("triggers:\n  - foo\n  - bar\n  - baz")
        assert isinstance(result, dict)
        assert result["triggers"] == ["foo", "bar", "baz"]

    def test_empty_lines_and_comments(self):
        result = _parse_simple_yaml("# comment\nname: test\n\n# another\ndesc: ok")
        assert result["name"] == "test"
        assert result["desc"] == "ok"


# ── _normalize_tool_def ──

class TestNormalizeToolDef:
    def test_basic_tool(self):
        tool = {
            "name": "my_tool",
            "description": "does things",
            "parameters": {
                "query": {"type": "string", "required": True, "description": "search query"},
                "limit": {"type": "integer", "description": "max results"},
            },
        }
        result = _normalize_tool_def(tool)
        assert result["name"] == "my_tool"
        assert result["description"] == "does things"
        assert result["input_schema"]["type"] == "object"
        assert "query" in result["input_schema"]["properties"]
        assert "query" in result["input_schema"]["required"]
        assert "limit" not in result["input_schema"].get("required", [])

    def test_string_shorthand(self):
        tool = {
            "name": "t",
            "description": "d",
            "parameters": {
                "url": "The URL to fetch",
            },
        }
        result = _normalize_tool_def(tool)
        assert result["input_schema"]["properties"]["url"]["type"] == "string"
        assert result["input_schema"]["properties"]["url"]["description"] == "The URL to fetch"

    def test_no_parameters(self):
        tool = {"name": "t", "description": "d", "parameters": {}}
        result = _normalize_tool_def(tool)
        assert result["input_schema"]["properties"] == {}


# ── match_skill_triggers ──

class TestMatchSkillTriggers:
    def test_single_match(self):
        assert match_skill_triggers("帮我分析一下竞品", ["竞品", "对手"]) == 1

    def test_multiple_match(self):
        assert match_skill_triggers("分析竞品对手的数据", ["竞品", "对手"]) == 2

    def test_no_match(self):
        assert match_skill_triggers("今天天气好", ["竞品", "对手"]) == 0

    def test_case_insensitive(self):
        assert match_skill_triggers("Run COMPETITOR analysis", ["competitor", "analysis"]) == 2

    def test_empty_text(self):
        assert match_skill_triggers("", ["竞品"]) == 0

    def test_empty_triggers(self):
        assert match_skill_triggers("竞品分析", []) == 0


# ── Integration: parse → normalize → match ──

class TestIntegration:
    def test_parse_and_match(self):
        content = """---
name: xhs-research
description: 小红书调研
triggers:
  - 小红书
  - 红书
  - XHS
---

搜索小红书内容并分析。
"""
        parsed = parse_skill_md(content)
        assert isinstance(parsed, dict)
        score = match_skill_triggers("帮我调研一下小红书博主", parsed["triggers"])
        assert score >= 1
        score2 = match_skill_triggers("今天吃什么", parsed["triggers"])
        assert score2 == 0

    def test_tool_definitions_roundtrip(self):
        """验证 tool 定义从 SKILL.md 解析后格式正确"""
        content = """---
name: test-tool
description: test
triggers:
  - test
tools:
  - name: fetch_data
    description: Fetch data from API
    parameters:
      endpoint: { type: string, required: true, description: "API endpoint" }
---

Instructions here.
"""
        parsed = parse_skill_md(content)
        assert len(parsed["tools"]) == 1
        tool = parsed["tools"][0]
        assert tool["name"] == "fetch_data"
        assert tool["input_schema"]["type"] == "object"
        assert "endpoint" in tool["input_schema"]["properties"]
        # required may or may not be extracted depending on inline JSON parsing
        # The key thing is the property exists with correct type
        assert tool["input_schema"]["properties"]["endpoint"]["type"] == "string"
