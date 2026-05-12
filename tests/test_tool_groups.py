"""工具分组 + 懒加载系统测试"""

import pytest


class TestSelectToolGroups:
    """_select_tool_groups: 根据用户消息选择工具组"""

    def test_empty_text_returns_all(self):
        from app.services.base_agent import _select_tool_groups, _TOOL_GROUPS
        result = _select_tool_groups("", "feishu")
        assert result == set(_TOOL_GROUPS.keys())

    def test_feishu_always_includes_feishu_collab(self):
        from app.services.base_agent import _select_tool_groups
        result = _select_tool_groups("帮我查下代码里的bug", "feishu")
        assert "feishu_collab" in result
        assert "core" in result
        assert "code_dev" in result

    def test_wecom_no_feishu_collab(self):
        from app.services.base_agent import _select_tool_groups
        result = _select_tool_groups("帮我查下代码里的bug", "wecom_kf")
        assert "feishu_collab" not in result
        assert "code_dev" in result

    def test_research_keywords(self):
        from app.services.base_agent import _select_tool_groups
        result = _select_tool_groups("帮我调研一下小红书上的竞品", "wecom_kf")
        assert "research" in result
        assert "core" in result

    def test_generic_message_returns_all(self):
        from app.services.base_agent import _select_tool_groups, _TOOL_GROUPS
        # 没有匹配任何关键词 → 安全回退到全部
        result = _select_tool_groups("你好", "wecom_kf")
        assert result == set(_TOOL_GROUPS.keys())

    def test_multiple_groups_match(self):
        from app.services.base_agent import _select_tool_groups
        result = _select_tool_groups("帮我查日程，顺便调研一下小红书", "feishu")
        assert "feishu_collab" in result
        assert "research" in result
        assert "core" in result

    def test_content_keywords(self):
        from app.services.base_agent import _select_tool_groups
        result = _select_tool_groups("帮我生成一份PDF报告", "wecom_kf")
        assert "content" in result

    def test_devops_keywords(self):
        from app.services.base_agent import _select_tool_groups
        result = _select_tool_groups("看下服务器日志", "feishu")
        assert "devops" in result

    def test_case_insensitive(self):
        from app.services.base_agent import _select_tool_groups
        result = _select_tool_groups("Check the Git repo", "feishu")
        assert "code_dev" in result


class TestGetGroupToolNames:
    """_get_group_tool_names: 获取工具组中的工具名"""

    def test_core_group(self):
        from app.services.base_agent import _get_group_tool_names
        names = _get_group_tool_names({"core"})
        assert "think" in names
        assert "web_search" in names
        assert "save_memory" in names

    def test_multiple_groups(self):
        from app.services.base_agent import _get_group_tool_names, _TOOL_GROUPS
        names = _get_group_tool_names({"core", "research"})
        assert "think" in names  # from core
        assert "web_search" in names  # from core
        # research group should have social media tools
        assert names & _TOOL_GROUPS["research"]

    def test_empty_groups(self):
        from app.services.base_agent import _get_group_tool_names
        names = _get_group_tool_names(set())
        assert len(names) == 0

    def test_group_membership_comes_from_plugin_registry(self, monkeypatch):
        from app.plugins.registry import plugin_registry
        from app.services.base_agent import _get_group_tool_names
        from app.tools.browser_ops import TOOL_MAP as BROWSER_TOOL_MAP

        plugin_registry.discover()
        entry = plugin_registry.get_plugin("browser_ops")
        assert entry is not None
        monkeypatch.setattr(entry.manifest, "group", "content")

        names = _get_group_tool_names({"research"})

        assert not (set(BROWSER_TOOL_MAP) & names)


class TestGetTenantToolsLazyLoading:
    """_get_tenant_tools with user_text: 验证懒加载过滤"""

    def _make_tenant(self, platform="feishu"):
        from app.tenant.config import TenantConfig
        return TenantConfig(
            tenant_id="test-bot",
            app_id="test",
            app_secret="test",
            llm_api_key="test",
            platform=platform,
            self_iteration_enabled=False,
            tools_enabled=[],  # empty = all enabled
        )

    def test_no_user_text_returns_all(self):
        from app.services.base_agent import _get_tenant_tools
        tenant = self._make_tenant()
        tools_all, _ = _get_tenant_tools(tenant, user_text="")
        tools_text, _ = _get_tenant_tools(tenant, user_text="")
        assert len(tools_all) == len(tools_text)

    def test_specific_intent_returns_fewer(self):
        from app.services.base_agent import _get_tenant_tools
        tenant = self._make_tenant("wecom_kf")
        tools_all, _ = _get_tenant_tools(tenant, user_text="")
        tools_research, _ = _get_tenant_tools(tenant, user_text="帮我调研小红书博主")
        # 调研意图应该返回更少的工具
        assert len(tools_research) < len(tools_all)

    def test_generic_message_returns_all(self):
        from app.services.base_agent import _get_tenant_tools
        tenant = self._make_tenant("wecom_kf")
        tools_all, _ = _get_tenant_tools(tenant, user_text="")
        tools_generic, _ = _get_tenant_tools(tenant, user_text="你好啊")
        # 通用消息应该回退到全部工具
        assert len(tools_generic) == len(tools_all)

    def test_request_more_tools_included(self):
        from app.services.base_agent import _get_tenant_tools
        tenant = self._make_tenant("wecom_kf")
        tools, tool_map = _get_tenant_tools(tenant, user_text="帮我调研小红书")
        tool_names = {t["function"]["name"] for t in tools}
        # 应包含 request_more_tools 元工具
        assert "request_more_tools" in tool_names
        assert "request_more_tools" in tool_map

    def test_read_tool_output_is_core_tool(self):
        from app.services.base_agent import _get_tenant_tools
        tenant = self._make_tenant("wecom_kf")
        tools, tool_map = _get_tenant_tools(tenant, user_text="帮我调研小红书")
        tool_names = {t["function"]["name"] for t in tools}

        assert "read_tool_output" in tool_names
        assert "read_tool_output" in tool_map

    def test_tool_map_stays_complete(self):
        """tool_map 应保持完整（支持动态扩展后调用）"""
        from app.services.base_agent import _get_tenant_tools, ALL_TOOL_MAP
        tenant = self._make_tenant("wecom_kf")
        _, tool_map = _get_tenant_tools(tenant, user_text="帮我调研小红书")
        # tool_map 应该包含所有工具（不只是当前加载的）
        # 因为 request_more_tools 动态扩展后 LLM 需要能调用新工具
        assert "web_search" in tool_map  # core
        assert "think" in tool_map  # always

    def test_tenant_tool_visibility_comes_from_plugin_registry(self, monkeypatch):
        from app.plugins.registry import plugin_registry
        from app.services.base_agent import _get_tenant_tools
        from app.tools.calendar_ops import TOOL_MAP as CALENDAR_TOOL_MAP

        plugin_registry.discover()
        entry = plugin_registry.get_plugin("calendar_ops")
        assert entry is not None
        monkeypatch.setattr(entry.manifest, "platforms", ["qq"])

        tenant = self._make_tenant("feishu")
        tools, tool_map = _get_tenant_tools(tenant, user_text="")
        tool_names = {t["function"]["name"] for t in tools}

        assert not (set(CALENDAR_TOOL_MAP) & tool_names)
        assert not (set(CALENDAR_TOOL_MAP) & set(tool_map))


class TestExpandToolGroup:
    """_expand_tool_group: 动态扩展工具集"""

    def _make_tenant(self, platform="wecom_kf"):
        from app.tenant.config import TenantConfig
        return TenantConfig(
            tenant_id="test-bot",
            app_id="test",
            app_secret="test",
            llm_api_key="test",
            platform=platform,
            self_iteration_enabled=False,
            tools_enabled=[],
        )

    def test_expand_new_group(self):
        from app.services.base_agent import _expand_tool_group
        tenant = self._make_tenant()
        # 只有 core 工具已加载
        current = {"think", "web_search", "save_memory", "recall_memory"}
        new_tools, new_map = _expand_tool_group("research", tenant, current)
        assert len(new_tools) > 0
        # 新工具不应包含已加载的
        new_names = {t["function"]["name"] for t in new_tools}
        assert not new_names & current

    def test_expand_already_loaded(self):
        from app.services.base_agent import _expand_tool_group, _TOOL_GROUPS
        tenant = self._make_tenant()
        # 已加载 research 组的所有工具
        current = set(_TOOL_GROUPS["research"])
        new_tools, new_map = _expand_tool_group("research", tenant, current)
        assert len(new_tools) == 0

    def test_expand_unknown_group(self):
        from app.services.base_agent import _expand_tool_group
        tenant = self._make_tenant()
        new_tools, new_map = _expand_tool_group("nonexistent", tenant, set())
        assert len(new_tools) == 0


class TestToolTracker:
    """tool_tracker: 工具性能追踪（fail-open 测试）"""

    def test_record_without_redis(self):
        """Redis 不可用时静默跳过，不报错"""
        from app.services.tool_tracker import record_tool_call
        # 不应抛异常
        record_tool_call("test", "web_search", True, latency_ms=100)
        record_tool_call("test", "web_search", False, error_msg="timeout")

    def test_record_lesson_without_redis(self):
        from app.services.tool_tracker import record_lesson
        record_lesson("test", "web_search", "搜索时用短关键词效果更好")

    def test_build_experience_hint_without_redis(self):
        from app.services.tool_tracker import build_experience_hint
        result = build_experience_hint("test", {"web_search", "export_file"})
        assert result == ""  # Redis 不可用时返回空

    def test_skip_think_tool(self):
        """think 工具不应被追踪"""
        from app.services.tool_tracker import record_tool_call
        record_tool_call("test", "think", True)  # 应该直接返回

    def test_skip_empty_tenant(self):
        from app.services.tool_tracker import record_tool_call
        record_tool_call("", "web_search", True)  # 应该直接返回


class TestAutoLessonGeneration:
    """P1: 连续失败自动生成经验教训"""

    def test_consecutive_failure_tracking(self):
        from app.services.tool_tracker import (
            _session_failures, _track_consecutive_failures,
            reset_session_failures, _AUTO_LESSON_THRESHOLD,
        )
        tid = "test-auto-lesson"
        reset_session_failures(tid)

        # 连续失败 N-1 次不触发
        for _ in range(_AUTO_LESSON_THRESHOLD - 1):
            _track_consecutive_failures(tid, "web_search", False, "timeout error")
        assert _session_failures.get(tid, {}).get("web_search", 0) == _AUTO_LESSON_THRESHOLD - 1

        # 第 N 次触发（计数重置为 0）
        _track_consecutive_failures(tid, "web_search", False, "timeout error")
        assert _session_failures.get(tid, {}).get("web_search", 0) == 0

        reset_session_failures(tid)

    def test_success_resets_counter(self):
        from app.services.tool_tracker import (
            _session_failures, _track_consecutive_failures,
            reset_session_failures,
        )
        tid = "test-reset"
        reset_session_failures(tid)

        _track_consecutive_failures(tid, "web_search", False, "error")
        _track_consecutive_failures(tid, "web_search", False, "error")
        # 成功一次重置计数
        _track_consecutive_failures(tid, "web_search", True, "")
        assert _session_failures.get(tid, {}).get("web_search") is None

        reset_session_failures(tid)

    def test_generate_failure_lesson_patterns(self):
        from app.services.tool_tracker import _generate_failure_lesson
        # 超时模式
        lesson = _generate_failure_lesson("web_search", "timeout error", 3)
        assert "超时" in lesson
        # 限流模式
        lesson = _generate_failure_lesson("web_search", "429 Too Many Requests", 3)
        assert "限流" in lesson
        # 权限模式
        lesson = _generate_failure_lesson("web_search", "403 Forbidden", 3)
        assert "权限" in lesson
        # 通用兜底
        lesson = _generate_failure_lesson("web_search", "unknown error", 3)
        assert "连续失败" in lesson

    def test_reset_session_failures(self):
        from app.services.tool_tracker import (
            _session_failures, reset_session_failures,
        )
        _session_failures["test-cleanup"] = {"tool1": 2}
        reset_session_failures("test-cleanup")
        assert "test-cleanup" not in _session_failures


class TestToolCombinationTracking:
    """P3: 工具组合模式追踪"""

    def test_record_sequence(self):
        from app.services.tool_tracker import (
            record_tool_sequence, _session_sequences,
        )
        tid = "test-seq"
        _session_sequences.pop(tid, None)

        record_tool_sequence(tid, "web_search")
        record_tool_sequence(tid, "browser_open")
        record_tool_sequence(tid, "browser_read")

        assert _session_sequences[tid] == ["web_search", "browser_open", "browser_read"]
        _session_sequences.pop(tid, None)

    def test_skip_combo_tools(self):
        from app.services.tool_tracker import (
            record_tool_sequence, _session_sequences,
        )
        tid = "test-skip"
        _session_sequences.pop(tid, None)

        record_tool_sequence(tid, "think")  # should be skipped
        record_tool_sequence(tid, "web_search")

        assert _session_sequences.get(tid, []) == ["web_search"]
        _session_sequences.pop(tid, None)

    def test_flush_empty_sequence(self):
        from app.services.tool_tracker import (
            flush_session_sequence, _session_sequences,
        )
        tid = "test-flush-empty"
        _session_sequences[tid] = ["web_search"]  # only 1 tool, too short
        flush_session_sequence(tid)
        assert tid not in _session_sequences

    def test_flush_extracts_combos(self):
        from app.services.tool_tracker import (
            flush_session_sequence, _session_sequences,
        )
        tid = "test-flush-combo"
        _session_sequences[tid] = ["web_search", "browser_open", "browser_read"]
        # flush should extract combos (Redis not available, just verify no error)
        flush_session_sequence(tid)
        assert tid not in _session_sequences

    def test_get_frequent_combos_without_redis(self):
        from app.services.tool_tracker import get_frequent_combos
        result = get_frequent_combos("test")
        assert result == []  # Redis 不可用时返回空

    def test_build_combo_hint_without_redis(self):
        from app.services.tool_tracker import build_combo_hint
        result = build_combo_hint("test", {"web_search"})
        assert result == ""  # Redis 不可用时返回空


class TestMemoryIndex:
    """P2: 记忆索引层"""

    def test_search_index_empty(self):
        from app.services.memory import search_index
        # 没有索引数据时返回空
        result = search_index(keyword="test")
        assert isinstance(result, list)

    def test_memory_distill_no_data(self):
        """经验蒸馏：数据不足时直接返回空"""
        import asyncio
        from app.services.memory import distill_experience

        # 模拟一个有 tenant context 的环境
        # distill_experience 需要 journal，但 Redis 不可用时 journal 为空
        try:
            result = asyncio.get_event_loop().run_until_complete(distill_experience())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(distill_experience())
            loop.close()
        assert result == []  # 数据不足，返回空


class TestTaskClassification:
    """任务类型分类（用于 sub-agent 委托和 research 指令注入）"""

    def test_quick_task(self):
        from app.services.base_agent import classify_task_type
        assert classify_task_type("你好") == "quick"
        assert classify_task_type("谢谢") == "quick"
        assert classify_task_type("ok") == "quick"

    def test_research_task(self):
        from app.services.base_agent import classify_task_type
        assert classify_task_type("帮我调研一下小红书上的竞品") == "research"
        assert classify_task_type("帮我写一份详细的市场调研报告") == "research"
        assert classify_task_type("平台的对标账号") == "research"
        assert classify_task_type("帮我找几个KOL达人") == "research"

    def test_deep_task(self):
        from app.services.base_agent import classify_task_type
        assert classify_task_type("帮我看下代码里的bug") == "deep"
        assert classify_task_type("部署一下最新版本") == "deep"

    def test_normal_task(self):
        from app.services.base_agent import classify_task_type
        assert classify_task_type("帮我查一下明天的日程") == "normal"

    def test_empty_text_is_quick(self):
        from app.services.base_agent import classify_task_type
        assert classify_task_type("") == "quick"
        assert classify_task_type("hi") == "quick"

    def test_max_rounds_is_generous(self):
        from app.services.base_agent import _MAX_ROUNDS
        assert _MAX_ROUNDS >= 50  # 宽松安全网，不强制截断


class TestDiscoverModules:
    """M2: 动态能力模块发现"""

    def test_discover_social_media(self):
        from app.services.base_agent import discover_modules
        # 使用 available_modules 参数避免依赖文件系统
        available = ["social_media_research", "code_review", "data_analysis"]
        result = discover_modules("帮我调研小红书博主", available_modules=available)
        assert "social_media_research" in result

    def test_discover_code_review(self):
        from app.services.base_agent import discover_modules
        available = ["social_media_research", "code_review", "data_analysis"]
        result = discover_modules("帮我做个代码审查", available_modules=available)
        assert "code_review" in result

    def test_discover_empty_text(self):
        from app.services.base_agent import discover_modules
        result = discover_modules("")
        assert result == []

    def test_discover_no_match(self):
        from app.services.base_agent import discover_modules
        available = ["social_media_research", "code_review"]
        result = discover_modules("今天天气不错", available_modules=available)
        assert result == []

    def test_discover_max_two(self):
        from app.services.base_agent import discover_modules
        available = [
            "social_media_research", "code_review",
            "data_analysis", "content_creation",
        ]
        result = discover_modules("帮我调研小红书竞品并写一份数据分析报告", available_modules=available)
        assert len(result) <= 2

    def test_discover_filters_unavailable(self):
        from app.services.base_agent import discover_modules
        # 模块不在 available 列表中
        available = ["nonexistent_module"]
        result = discover_modules("帮我调研小红书博主", available_modules=available)
        assert result == []


class TestDeepResearchInstructions:
    """M3: 深度研究模式指令"""

    def test_research_instructions_constant_exists(self):
        from app.services.base_agent import _DEEP_RESEARCH_INSTRUCTIONS
        assert "深度调研模式" in _DEEP_RESEARCH_INSTRUCTIONS
        assert "export_file" in _DEEP_RESEARCH_INSTRUCTIONS

    def test_max_tool_result_len(self):
        from app.services.base_agent import _MAX_TOOL_RESULT_LEN
        # 至少 16000 以避免截断导致 URL 幻觉
        assert _MAX_TOOL_RESULT_LEN >= 16000
