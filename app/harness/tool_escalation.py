"""Runtime policy for stopping overly-heavy tool escalation on light advice turns."""

from __future__ import annotations

import re

_LIGHT_ADVICE_RE = re.compile(
    r"(攻略|路线|速通|一小时|半天|一日|半日|打卡|避坑|怎么逛|怎么玩|"
    r"行程|路线图|游览|旅游|寺|寺庙|景点|景区|出行|生活攻略|求个攻略|推荐路线)",
    re.IGNORECASE,
)
_EXPLICIT_HEAVY_RESEARCH_RE = re.compile(
    r"(严谨调研|深度调研|完整报告|详细报告|爬取|登录后查看|必须小红书|必须站内|"
    r"一定要小红书|必须用浏览器|逐条核对)",
    re.IGNORECASE,
)

_PUBLIC_INFO_TOOLS = frozenset({
    "web_search",
    "fetch_url",
    "search_social_media",
    "xhs_search",
    "xhs_playwright_search",
})
_HEAVY_DOWNSTREAM_TOOLS = frozenset({
    "browser_open",
    "browser_do",
    "browser_read",
    "xhs_login",
    "xhs_check_login",
})
_SOCIAL_CHAIN_TOOLS = _PUBLIC_INFO_TOOLS | _HEAVY_DOWNSTREAM_TOOLS

_FAILURE_RE = re.compile(r"(失败|超时|error|timeout|登录墙|需要登录|blocked)", re.IGNORECASE)
_TASK_CALENDAR_INTENT_RE = re.compile(
    r"(任务|待办|todo|task|tasklist|日程|日历|calendar|提醒|会议|安排|排期|报销单)",
    re.IGNORECASE,
)
_FILE_DELIVERABLE_INTENT_RE = re.compile(
    r"(\b(pdf|ppt|csv|xlsx|excel|html)\b|导出|生成(文档|文件|报告)|发我|给我(文件|pdf|报告))",
    re.IGNORECASE,
)
_TASK_CALENDAR_TOOLS = frozenset({
    "list_feishu_tasks",
    "list_feishu_tasklists",
    "list_tasklist_tasks",
    "create_feishu_task",
    "update_feishu_task",
    "complete_feishu_task",
    "list_calendar_events",
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
    "check_availability",
    "find_free_slots",
})
_FILE_DELIVERABLE_TOOLS = frozenset({
    "export_file",
    "create_document",
    "create_feishu_doc",
    "write_feishu_doc",
    "update_feishu_doc",
    "edit_feishu_doc",
})
_CODE_EXPLORATION_TOOLS = frozenset({
    "list_files",
    "read_file",
    "search_files",
    "search_logs",
})


def is_light_advice_turn(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if _EXPLICIT_HEAVY_RESEARCH_RE.search(text):
        return False
    return bool(_LIGHT_ADVICE_RE.search(text))


def _is_success_outcome(outcome: str) -> bool:
    text = (outcome or "").strip()
    if not text:
        return False
    return not _FAILURE_RE.search(text) and not text.startswith("→ 失败")


def _is_failure_outcome(outcome: str) -> bool:
    text = (outcome or "").strip()
    if not text:
        return False
    return bool(_FAILURE_RE.search(text) or text.startswith("→ 失败"))


def build_tool_settle_nudge(
    user_text: str,
    proposed_tools: list[str] | tuple[str, ...],
    action_outcomes: list[tuple[str, str]] | None,
) -> str | None:
    """Return a nudge when a light advice task should stop escalating heavy tools."""
    if not is_light_advice_turn(user_text):
        return None
    if not proposed_tools or not action_outcomes:
        return None

    proposed = set(proposed_tools)
    if not (proposed & _SOCIAL_CHAIN_TOOLS):
        return None

    public_successes = 0
    successful_xhs = 0
    heavy_failures = []

    for func_name, outcome in action_outcomes:
        if func_name in _PUBLIC_INFO_TOOLS and _is_success_outcome(outcome):
            public_successes += 1
            if func_name in {"xhs_search", "xhs_playwright_search"}:
                successful_xhs += 1
        if func_name in _SOCIAL_CHAIN_TOOLS and _is_failure_outcome(outcome):
            heavy_failures.append(func_name)

    enough_info = public_successes >= 2 or successful_xhs >= 1
    has_social_failure = bool(heavy_failures)

    if proposed & _HEAVY_DOWNSTREAM_TOOLS:
        if enough_info:
            return (
                "⚠️ 这是生活攻略/路线建议类问题。你已经拿到足够的公开资料了，"
                "不要再继续升级到 browser_open / browser_read / xhs_login 这类重工具。"
                "请基于已经拿到的搜索结果，直接给出一个实用、简洁、可执行的答案。"
                "如果小红书/浏览器链路刚才超时了，只需诚实说明站内结果没有补充成功，不要继续卡住。"
            )
        if has_social_failure and public_successes >= 1:
            return (
                "⚠️ 小红书/浏览器链路已经出现超时或失败。当前又不是必须登录站内才能完成的问题。"
                "你已经至少拿到一份公开搜索结果，请停止继续升级重工具，"
                "直接基于现有资料先给用户一个可用攻略，并简短说明站内补充未成功。"
            )

    if proposed & {"xhs_search", "xhs_playwright_search"} and has_social_failure and enough_info:
        return (
            "⚠️ 你已经搜到足够的公开资料，而且小红书链路刚失败过。"
            "不要反复重试 xhs_search。请直接总结现有信息，先把可执行答案给用户。"
        )

    return None


def build_tool_domain_nudge(
    user_text: str,
    proposed_tools: list[str] | tuple[str, ...],
    *,
    task_type: str = "",
) -> str | None:
    """Block obvious cross-domain tool drift on research/chat turns."""
    if not proposed_tools:
        return None
    proposed = set(proposed_tools)
    turn_type = (task_type or "").strip().lower()
    if turn_type not in {"research", "normal"}:
        return None

    off_domain = proposed & _TASK_CALENDAR_TOOLS
    if off_domain and not _TASK_CALENDAR_INTENT_RE.search(user_text or ""):
        blocked_names = "、".join(sorted(off_domain))
        return (
            "⚠️ 当前是调研/问答任务，不是任务或日历操作。"
            f"你刚才尝试调用了不相关工具：{blocked_names}。"
            "请回到当前问题，优先使用 web_search/fetch_url/read_feishu_doc/recall_memory 等信息检索工具，"
            "然后直接给用户结论。"
        )

    if (
        _FILE_DELIVERABLE_INTENT_RE.search(user_text or "")
        and not (proposed & _FILE_DELIVERABLE_TOOLS)
        and (proposed & _CODE_EXPLORATION_TOOLS)
    ):
        return (
            "⚠️ 用户明确要文件交付（PDF/报告/导出），但你在绕去代码搜索工具。"
            "请停止 list_files/read_file/search_files 这类偏离调用，"
            "直接生成并发送交付物：优先调用 export_file（飞书场景可用 create_feishu_doc）。"
        )

    return None
