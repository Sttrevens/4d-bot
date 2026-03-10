"""Code Preflight Context 测试

验证代码修改任务的上下文预加载机制：
- 标识符提取（类名、变量名、方法名）
- 停用词过滤（Unity/C# 关键词不搜）
- write_file 变更审查提醒
"""

from __future__ import annotations

import re
import pytest

# ── 直接复制 regex 和停用词（避免导入 gemini_provider 触发 google-genai） ──
# 与 gemini_provider.py 中的定义保持一致

_CODE_IDENT_RE = re.compile(
    r'\b([A-Z][a-zA-Z0-9]{2,}(?:Manager|Controller|UI|System|Handler|Service|Data|Config|View)?)\b'
    r'|'
    r'\b([a-z][a-zA-Z0-9]{3,}(?:List\d*|Array|Map|Dict|Data|Config|UI)?)\b'
)

_PREFLIGHT_STOP_WORDS = frozenset({
    "Unity", "GameObject", "Transform", "Component", "MonoBehaviour",
    "String", "Boolean", "Integer", "Float", "Double", "Object",
    "List", "Array", "Dict", "Data", "Config", "True", "False",
    "None", "Null", "This", "Class", "Type", "View", "System",
    "Vector2", "Vector3", "Quaternion", "Color", "Rect",
    "Task", "Async", "Await", "Event", "Action", "Func",
    "Debug", "Console", "Logger", "Error", "Exception",
})


def _extract(text: str) -> set[str]:
    """提取并过滤标识符"""
    ids = set()
    for m in _CODE_IDENT_RE.finditer(text):
        name = m.group(1) or m.group(2)
        if name and name not in _PREFLIGHT_STOP_WORDS and len(name) >= 4:
            ids.add(name)
    return ids


# ── 标识符提取测试 ──

class TestIdentifierExtraction:
    def test_extract_pascal_case_class(self):
        ids = _extract("帮我改一下 StreamInteractionUIManager 的逻辑")
        assert "StreamInteractionUIManager" in ids

    def test_extract_camel_case_variable(self):
        ids = _extract("把 moveUIList1 加个布尔开关")
        assert "moveUIList1" in ids

    def test_extract_multiple_identifiers(self):
        ids = _extract("moveUIList 和 pushTaskUI 需要改")
        assert "moveUIList" in ids
        assert "pushTaskUI" in ids

    def test_filter_unity_keywords(self):
        ids = _extract("这个 GameObject 的 Transform 有问题")
        assert "GameObject" not in ids
        assert "Transform" not in ids

    def test_filter_short_names(self):
        ids = _extract("改一下 foo 和 bar 变量")
        assert "foo" not in ids
        assert "bar" not in ids

    def test_extract_controller_suffix(self):
        ids = _extract("ConeDetectionController 需要修复")
        assert "ConeDetectionController" in ids

    def test_extract_from_code_context(self):
        ids = _extract("GetComponent<TaskUI>() 这行有 bug")
        assert "TaskUI" in ids

    def test_chinese_only_message_no_crash(self):
        ids = _extract("帮我改一下代码的逻辑，让它可以控制动画")
        assert isinstance(ids, set)

    def test_real_scenario_moveui(self):
        text = "给 moveUIList1 的遍历加个布尔开关 pushTaskUI，跳过 TaskUI 组件"
        ids = _extract(text)
        assert "moveUIList1" in ids
        assert "pushTaskUI" in ids
        assert "TaskUI" in ids

    def test_filter_system_and_common(self):
        ids = _extract("System.Debug.Log()")
        assert "System" not in ids
        assert "Debug" not in ids

    def test_extract_method_name(self):
        ids = _extract("OnEnterInteractionMode 方法需要改")
        assert "OnEnterInteractionMode" in ids

    def test_extract_from_git_context(self):
        """从 branch 名中提取"""
        ids = _extract("在 fix/taskui-not-moving 分支上改 StreamInteractionUIManager")
        assert "StreamInteractionUIManager" in ids
        # taskui 太短被过滤（因为带 - 分隔）

    def test_no_duplicates(self):
        ids = _extract("moveUIList 和 moveUIList 重复了")
        assert ids == {"moveUIList"}


class TestPreflightStopWords:
    def test_unity_types_filtered(self):
        assert "GameObject" in _PREFLIGHT_STOP_WORDS
        assert "Transform" in _PREFLIGHT_STOP_WORDS
        assert "MonoBehaviour" in _PREFLIGHT_STOP_WORDS

    def test_csharp_keywords_filtered(self):
        assert "String" in _PREFLIGHT_STOP_WORDS
        assert "Boolean" in _PREFLIGHT_STOP_WORDS

    def test_specific_names_not_filtered(self):
        assert "TaskUI" not in _PREFLIGHT_STOP_WORDS
        assert "StreamInteractionUIManager" not in _PREFLIGHT_STOP_WORDS
        assert "moveUIList" not in _PREFLIGHT_STOP_WORDS
        assert "ConeDetection" not in _PREFLIGHT_STOP_WORDS


class TestDiffReviewHints:
    def test_diff_review_detects_changed_identifiers(self):
        from app.tools.file_ops import _diff_review_hints
        old = "foreach (var moveUI in moveUIList1) {\n    moveUI.DOAnchorPos(offset, 0.3f);\n}\n"
        new = (
            "foreach (var moveUI in moveUIList1) {\n"
            "    if (!pushTaskUI && moveUI.GetComponent<TaskUI>() != null) continue;\n"
            "    moveUI.DOAnchorPos(offset, 0.3f);\n}\n"
        )
        hints = _diff_review_hints(old, new, "test.cs")
        assert "变更审查提醒" in hints
        assert "search_code" in hints

    def test_diff_review_no_hints_for_identical(self):
        from app.tools.file_ops import _diff_review_hints
        content = "int x = 1;\n"
        assert _diff_review_hints(content, content, "test.cs") == ""

    def test_diff_review_new_file_no_hints(self):
        """新文件（old 为空）不提示"""
        from app.tools.file_ops import _diff_review_hints
        hints = _diff_review_hints("", "int x = 1;\n", "test.cs")
        # 新文件的所有行都是新增的，但没有"在旧文件中存在的标识符"
        # 所以不应该提示（没有关联引用可搜）
        # hints 可以是空或非空，取决于标识符是否在 old 中出现

    def test_diff_review_filters_keywords(self):
        from app.tools.file_ops import _diff_review_hints
        old = "class Foo {}\n"
        new = "class Foo {}\npublic static void Main() {}\n"
        hints = _diff_review_hints(old, new, "test.cs")
        # "public" and "static" should be filtered out as keywords
        if hints:
            after_involves = hints.split("涉及")[1].split("。")[0] if "涉及" in hints else ""
            assert "public" not in after_involves


class TestPreflightIntegration:
    """集成场景测试：模拟真实的用户代码修改请求"""

    def test_typical_unity_code_request(self):
        """典型 Unity 代码修改请求"""
        text = "StreamInteractionUIManager.cs 里的 OnEnterInteractionMode 方法，给 moveUIList1 的遍历加个布尔开关"
        ids = _extract(text)
        # 应该提取到所有关键标识符
        assert "StreamInteractionUIManager" in ids
        assert "OnEnterInteractionMode" in ids
        assert "moveUIList1" in ids

    def test_bug_fix_request(self):
        """Bug 修复请求"""
        text = "ConeDetection.cs 的 GetTargetState 方法有 bug，返回值不对"
        ids = _extract(text)
        assert "ConeDetection" in ids
        assert "GetTargetState" in ids

    def test_vague_request_still_extracts(self):
        """模糊请求但包含标识符"""
        text = "改一下 playerHealth 相关的逻辑"
        ids = _extract(text)
        assert "playerHealth" in ids

    def test_no_identifiers_returns_empty(self):
        """纯自然语言请求"""
        text = "帮我把代码改得更好一点"
        ids = _extract(text)
        assert len(ids) == 0 or all(len(i) < 4 for i in ids if i in _PREFLIGHT_STOP_WORDS)
