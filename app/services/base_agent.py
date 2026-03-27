"""Agent 共享基础设施

两个 provider（gemini_provider / kimi_coder）的共享逻辑提取到此处，
消除重复代码、统一行为、降低维护成本。

提供:
- 工具注册表: ALL_TOOL_MAP, _ALL_TOOL_DEFS, _get_tenant_tools()
- System prompt 构建: _build_system_prompt()
- Agent 循环辅助: _has_unmatched_reads, _drain_inbox, _build_progress_hint
- 输出处理: _strip_degenerate_repetition, _trigger_memory, _set_tool_summary
- 安全: _is_admin, _get_custom_tool_risk, _user_confirmed
- 常量: _MAX_ROUNDS, _MAX_TOOL_RESULT_LEN, etc.
- 类型: ProgressCallback
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import contextvars
from typing import Callable, Awaitable

# ── 超时智能消息：跟踪 agent 进度，超时时提供有信息量的提示 ──
# handler 层在 TimeoutError 时读取此变量，构造上下文相关的超时消息。
_agent_progress: contextvars.ContextVar[list[str]] = contextvars.ContextVar(
    "_agent_progress", default=[],
)


def record_agent_progress(tool_name: str) -> None:
    """记录 agent 调用了哪个工具（供超时消息使用）。"""
    try:
        progress = _agent_progress.get()
        progress.append(tool_name)
    except LookupError:
        _agent_progress.set([tool_name])


def build_timeout_message() -> str:
    """根据 agent 已完成的工具调用，构造友好的超时消息。

    面向普通用户（不懂技术），语气要自然亲切。
    """
    try:
        progress = _agent_progress.get()
    except LookupError:
        progress = []

    if not progress:
        return "抱歉，这条消息处理时间太长了~ 你可以换个方式再说一遍，或者把问题拆小一点试试？"

    # 根据工具类型构造用户能理解的说明
    tool_set = set(progress)
    parts = []

    if tool_set & {"web_search", "fetch_url", "browser_open", "search_social_media",
                   "xhs_search", "xhs_playwright_search"}:
        parts.append("资料已经帮你查好了")
    if tool_set & {"export_file"}:
        parts.append("文件正在生成中，但花的时间比预期长")
    if tool_set & {"create_calendar_event", "update_calendar_event"}:
        n = progress.count("create_calendar_event") + progress.count("update_calendar_event")
        parts.append(f"已经帮你安排了 {n} 个日程")
    if tool_set & {"create_feishu_task"}:
        n = progress.count("create_feishu_task")
        parts.append(f"已经帮你创建了 {n} 个任务")
    if tool_set & {"send_feishu_message", "send_message_to_user", "reply_feishu_message"}:
        parts.append("消息已经发出去了")

    if parts:
        summary = "，".join(parts)
        return f"抱歉没能一口气做完~ 不过{summary}。你发个「继续」我接着帮你搞定剩下的！"
    else:
        return f"抱歉，处理时间太长了，但我已经做了一部分工作。你发个「继续」我接着帮你做~"


def reset_agent_progress() -> None:
    """重置进度跟踪（每个请求开始时调用）。"""
    _agent_progress.set([])

from app.config import settings
from app.tools.file_ops import (
    TOOL_DEFINITIONS as FILE_TOOLS,
    TOOL_MAP as FILE_TOOL_MAP,
)
from app.tools.git_ops import (
    TOOL_DEFINITIONS as GIT_TOOLS,
    TOOL_MAP as GIT_TOOL_MAP,
)
from app.tools.github_ops import (
    TOOL_DEFINITIONS as GITHUB_TOOLS,
    TOOL_MAP as GITHUB_TOOL_MAP,
)
from app.tools.web_search import (
    TOOL_DEFINITIONS as WEB_TOOLS,
    TOOL_MAP as WEB_TOOL_MAP,
)
from app.tools.repo_search import (
    TOOL_DEFINITIONS as REPO_SEARCH_TOOLS,
    TOOL_MAP as REPO_SEARCH_TOOL_MAP,
)
from app.tools.issue_ops import (
    TOOL_DEFINITIONS as ISSUE_TOOLS,
    TOOL_MAP as ISSUE_TOOL_MAP,
)
from app.tools.calendar_ops import (
    TOOL_DEFINITIONS as CALENDAR_TOOLS,
    TOOL_MAP as CALENDAR_TOOL_MAP,
)
from app.tools.doc_ops import (
    TOOL_DEFINITIONS as DOC_TOOLS,
    TOOL_MAP as DOC_TOOL_MAP,
)
from app.tools.minutes_ops import (
    TOOL_DEFINITIONS as MINUTES_TOOLS,
    TOOL_MAP as MINUTES_TOOL_MAP,
)
from app.tools.task_ops import (
    TOOL_DEFINITIONS as TASK_TOOLS,
    TOOL_MAP as TASK_TOOL_MAP,
)
from app.tools.user_ops import (
    TOOL_DEFINITIONS as USER_TOOLS,
    TOOL_MAP as USER_TOOL_MAP,
)
from app.tools.message_ops import (
    TOOL_DEFINITIONS as MESSAGE_TOOLS,
    TOOL_MAP as MESSAGE_TOOL_MAP,
)
from app.tools.self_ops import (
    TOOL_DEFINITIONS as SELF_TOOLS,
    TOOL_MAP as SELF_TOOL_MAP,
)
from app.tools.server_ops import (
    TOOL_DEFINITIONS as SERVER_TOOLS,
    TOOL_MAP as SERVER_TOOL_MAP,
)
from app.tools.bitable_ops import (
    TOOL_DEFINITIONS as BITABLE_TOOLS,
    TOOL_MAP as BITABLE_TOOL_MAP,
)
from app.tools.memory_ops import (
    TOOL_DEFINITIONS as MEMORY_TOOLS,
    TOOL_MAP as MEMORY_TOOL_MAP,
)
from app.tools.custom_tool_ops import (
    TOOL_DEFINITIONS as CUSTOM_TOOL_TOOLS,
    TOOL_MAP as CUSTOM_TOOL_MAP,
    load_tenant_tools,
)
from app.tools.provision_ops import (
    TOOL_DEFINITIONS as PROVISION_TOOLS,
    TOOL_MAP as PROVISION_TOOL_MAP,
)
from app.tools.file_export import (
    TOOL_DEFINITIONS as FILE_EXPORT_TOOLS,
    TOOL_MAP as FILE_EXPORT_TOOL_MAP,
)
from app.tools.video_url_ops import (
    TOOL_DEFINITIONS as VIDEO_URL_TOOLS,
    TOOL_MAP as VIDEO_URL_TOOL_MAP,
)
from app.tools.skill_ops import (
    TOOL_DEFINITIONS as SKILL_TOOLS,
    TOOL_MAP as SKILL_TOOL_MAP,
)
from app.tools.env_ops import (
    TOOL_DEFINITIONS as ENV_TOOLS,
    TOOL_MAP as ENV_TOOL_MAP,
)
from app.tools.browser_ops import (
    TOOL_DEFINITIONS as BROWSER_TOOLS,
    TOOL_MAP as BROWSER_TOOL_MAP,
)
from app.tools.capability_ops import (
    TOOL_DEFINITIONS as CAPABILITY_TOOLS,
    TOOL_MAP as CAPABILITY_TOOL_MAP,
)
from app.tools.module_ops import (
    TOOL_DEFINITIONS as MODULE_TOOLS,
    TOOL_MAP as MODULE_TOOL_MAP,
)
from app.tools.mail_ops import (
    TOOL_DEFINITIONS as MAIL_TOOLS,
    TOOL_MAP as MAIL_TOOL_MAP,
)
from app.tools.social_media_ops import (
    TOOL_DEFINITIONS as SOCIAL_MEDIA_TOOLS,
    TOOL_MAP as SOCIAL_MEDIA_TOOL_MAP,
)
from app.tools.xhs_ops import (
    TOOL_DEFINITIONS as XHS_TOOLS,
    TOOL_MAP as XHS_TOOL_MAP,
)
from app.tools.customer_ops import (
    TOOL_DEFINITIONS as CUSTOMER_TOOLS,
    TOOL_MAP as CUSTOMER_TOOL_MAP,
)
from app.tools.reminder_ops import (
    TOOL_DEFINITIONS as REMINDER_TOOLS,
    TOOL_MAP as REMINDER_TOOL_MAP,
)
from app.tools.identity_ops import (
    TOOL_DEFINITIONS as IDENTITY_TOOLS,
    TOOL_MAP as IDENTITY_TOOL_MAP,
)
from app.tools.image_ops import (
    TOOL_DEFINITIONS as IMAGE_TOOLS,
    TOOL_MAP as IMAGE_TOOL_MAP,
)
from app.tools.skill_mgmt_ops import (
    TOOL_DEFINITIONS as SKILL_MGMT_TOOLS,
    TOOL_MAP as SKILL_MGMT_TOOL_MAP,
)
from app.tools.cron_agent_ops import (
    TOOL_DEFINITIONS as CRON_AGENT_TOOLS,
    TOOL_MAP as CRON_AGENT_TOOL_MAP,
)
from app.services import user_registry
from app.services import memory as bot_memory
from app.services import planner as bot_planner

logger = logging.getLogger(__name__)

# ── think 工具：让模型按需"想一想"再行动，零额外 API 调用 ──
_THINK_TOOL_DEF = {
    "name": "think",
    "description": (
        "用这个工具来组织你的思路、规划下一步行动。"
        "在复杂任务中，先 think 再行动。think 的内容不会发送给用户。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "thought": {
                "type": "string",
                "description": "你的思考过程：分析情况、列出方案、决定下一步",
            },
        },
        "required": ["thought"],
    },
}


def _handle_think(args: dict) -> str:
    """think 工具：纯内部推理，不产生外部副作用"""
    logger.debug("think: %s", args.get("thought", "")[:200])
    return "OK"


# ── 工具注册表 ──

ALL_TOOL_MAP = {
    "think": _handle_think,
    **FILE_TOOL_MAP, **GIT_TOOL_MAP, **GITHUB_TOOL_MAP,
    **WEB_TOOL_MAP, **REPO_SEARCH_TOOL_MAP, **ISSUE_TOOL_MAP,
    **CALENDAR_TOOL_MAP, **DOC_TOOL_MAP, **MINUTES_TOOL_MAP,
    **TASK_TOOL_MAP, **USER_TOOL_MAP, **MESSAGE_TOOL_MAP,
    **SELF_TOOL_MAP, **SERVER_TOOL_MAP, **MEMORY_TOOL_MAP,
    **BITABLE_TOOL_MAP,
    **CUSTOM_TOOL_MAP,
    **PROVISION_TOOL_MAP,
    **FILE_EXPORT_TOOL_MAP,
    **VIDEO_URL_TOOL_MAP,
    **SKILL_TOOL_MAP,
    **ENV_TOOL_MAP,
    **BROWSER_TOOL_MAP,
    **CAPABILITY_TOOL_MAP,
    **MODULE_TOOL_MAP,
    **MAIL_TOOL_MAP,
    **SOCIAL_MEDIA_TOOL_MAP,
    **XHS_TOOL_MAP,
    **CUSTOMER_TOOL_MAP,
    **REMINDER_TOOL_MAP,
    **IDENTITY_TOOL_MAP,
    **IMAGE_TOOL_MAP,
    **SKILL_MGMT_TOOL_MAP,
    **CRON_AGENT_TOOL_MAP,
}

# 自我迭代相关工具名（客户租户禁用）
_SELF_ITERATION_TOOLS = frozenset(SELF_TOOL_MAP.keys()) | frozenset(SERVER_TOOL_MAP.keys())

# 实例管理工具名（仅 instance_management_enabled 租户可用）
_INSTANCE_MGMT_TOOLS = frozenset(PROVISION_TOOL_MAP.keys()) | frozenset(CUSTOMER_TOOL_MAP.keys())

# 自定义工具元操作名（需要自动注入 tenant_id）
_CUSTOM_TOOL_META_NAMES = frozenset(CUSTOM_TOOL_MAP.keys()) | frozenset(SKILL_TOOL_MAP.keys()) | frozenset(SKILL_MGMT_TOOL_MAP.keys())

# 飞书专属工具名（企微租户禁用 — 这些工具依赖飞书 API / user_access_token）
_FEISHU_ONLY_TOOLS = (
    frozenset(CALENDAR_TOOL_MAP.keys())
    | frozenset(DOC_TOOL_MAP.keys())
    | frozenset(MINUTES_TOOL_MAP.keys())
    | frozenset(TASK_TOOL_MAP.keys())
    | frozenset(USER_TOOL_MAP.keys())
    | frozenset(MESSAGE_TOOL_MAP.keys())
    | frozenset(BITABLE_TOOL_MAP.keys())
    | frozenset(MAIL_TOOL_MAP.keys())
)

# 企微专属工具名（当前为空 — file_export 两个平台都可用，飞书优先云文档但不禁止导出）
_WECOM_ONLY_TOOLS: frozenset = frozenset()

# 全量工具定义列表（用于按租户过滤）
_ALL_TOOL_DEFS = (
    [_THINK_TOOL_DEF]
    + CALENDAR_TOOLS + DOC_TOOLS + MINUTES_TOOLS + TASK_TOOLS
    + USER_TOOLS + MESSAGE_TOOLS + WEB_TOOLS
    + REPO_SEARCH_TOOLS + FILE_TOOLS + GIT_TOOLS + GITHUB_TOOLS + ISSUE_TOOLS
    + SELF_TOOLS + SERVER_TOOLS
    + MEMORY_TOOLS + BITABLE_TOOLS
    + CUSTOM_TOOL_TOOLS
    + PROVISION_TOOLS
    + FILE_EXPORT_TOOLS
    + VIDEO_URL_TOOLS
    + SKILL_TOOLS
    + ENV_TOOLS
    + BROWSER_TOOLS
    + CAPABILITY_TOOLS
    + MODULE_TOOLS
    + MAIL_TOOLS
    + SOCIAL_MEDIA_TOOLS
    + XHS_TOOLS
    + CUSTOMER_TOOLS
    + REMINDER_TOOLS
    + IDENTITY_TOOLS
    + IMAGE_TOOLS
    + SKILL_MGMT_TOOLS
    + CRON_AGENT_TOOLS
)


# ── 共享常量 ──

_MAX_ROUNDS = 50  # 宽松安全网，让模型自己决定何时完成
# 工具返回值最大字符数：超过则截断，防止 context 爆炸导致后续轮次极慢
# 从 8000 提升到 16000：8000 截断经常切掉 Google Sheets 等数据源的 URL，
# 导致 LLM 幻觉编造 URL。数据完整性比省 token 重要。
_MAX_TOOL_RESULT_LEN = 16000
# 压缩旧工具结果的阈值：保留最近 N 条工具结果原文，更早的截断为 200 字符
_COMPRESS_KEEP_RECENT = 8
_COMPRESS_AFTER_ROUND = 6

# 中间消息回调类型：async def callback(text: str) -> None
ProgressCallback = Callable[[str], Awaitable[None]]


# ── URL 溯源验证器（防止 LLM 幻觉编造 URL） ──
# 核心思路：prompt 约束是软限制，LLM 从根本上不擅长精确复制字符串。
# 结构性防护：收集所有工具返回结果中的 URL，在 LLM 生成写操作参数时
# 验证其中的 URL 是否真实出现过。未见过的 URL = 幻觉，直接拦截。

_URL_RE = re.compile(r'https?://[^\s"\'<>\]）），。、；：！,]+')

# 这些工具的参数中出现的 URL 不需要验证（用户主动提供的 URL）
_URL_CHECK_EXEMPT_TOOLS = frozenset({
    "fetch_url", "browser_open", "browser_do", "web_search",
    "analyze_video_url", "think",
    # 自定义工具测试：参数可能包含任意数据
    "test_custom_tool", "create_custom_tool",
})

# 这些工具执行写操作，其中的 URL 必须来自已见数据
_URL_CHECK_WRITE_TOOLS = frozenset({
    "update_calendar_event", "create_calendar_event",
    "create_document", "edit_feishu_doc", "write_feishu_doc",
    "add_bitable_record", "update_bitable_record", "batch_update_bitable_records",
    "send_feishu_message", "reply_feishu_message",
    "create_feishu_task", "update_feishu_task",
    "send_mail",
})

# ── 写操作意图验证器（结构性防幻觉 — Generator-Evaluator 分离）──
# 在执行写操作前，用独立 LLM 调用验证用户是否明确要求了此操作。
# 这是 Anthropic Harness 架构思路的落地：生成器和评估器分离，
# 不让同一个 agent 既做事又评判自己。

_WRITE_INTENT_VERIFY_TOOLS = frozenset({
    "create_calendar_event", "update_calendar_event",
    "create_feishu_task", "update_feishu_task",
    "create_document", "edit_feishu_doc", "write_feishu_doc",
    "send_mail",
    "send_feishu_message", "reply_feishu_message",
    "send_message_to_user",
    "add_bitable_record", "update_bitable_record", "batch_update_bitable_records",
    "xhs_publish",
})


def check_write_intent(
    func_name: str,
    func_args: dict,
    user_text: str,
    tools_called_so_far: list[str],
) -> str | None:
    """独立 LLM 验证：用户是否明确要求了这个写操作。

    返回 None 表示通过（允许执行），返回字符串表示拦截（含拒绝理由）。
    仅对 _WRITE_INTENT_VERIFY_TOOLS 中的工具生效。

    设计原则：
    - fail-open：LLM 调用失败/超时 → 放行（不阻塞正常流程）
    - 快速：用最小模型、低 token、短超时
    - 只在"无中生有"时拦截：用户已经在对话中表达了相关意图则放行
    """
    if func_name not in _WRITE_INTENT_VERIFY_TOOLS:
        return None

    # 如果用户消息本身包含明确的动作指令，直接放行（减少不必要的 LLM 调用）
    _EXPLICIT_ACTION_RE = re.compile(
        r"(创建|新建|添加|加个|安排|设置|发[给到]|写[个一]|生成|帮我[做弄搞发建排]|"
        r"约[个一]|排[个一]|建[个一]|提醒|通知|create|send|add|schedule|make)",
        re.IGNORECASE,
    )
    if _EXPLICIT_ACTION_RE.search(user_text):
        return None

    # 如果本轮已经调用过同类工具多次（批量操作），跳过验证
    same_tool_count = sum(1 for t in tools_called_so_far if t == func_name)
    if same_tool_count >= 1:
        return None  # 第一次已经验证过/放行了，后续批量不重复验证

    # 独立 LLM 调用验证
    try:
        from google import genai
        from google.genai import types
        from app.tenant.context import get_current_tenant

        tenant = get_current_tenant()
        base_url = getattr(tenant, "gemini_base_url", "") or "https://generativelanguage.googleapis.com"
        api_key = getattr(tenant, "gemini_api_key", "") or ""
        if not api_key:
            return None  # fail-open

        http_options = {}
        if base_url and "googleapis.com" not in base_url:
            http_options["base_url"] = base_url
        gc = genai.Client(api_key=api_key, http_options=http_options)

        args_brief = json.dumps(func_args, ensure_ascii=False)[:300]
        prompt = (
            f"用户原文：「{user_text[:200]}」\n"
            f"Agent 准备执行：{func_name}({args_brief})\n\n"
            f"判断：用户是否明确要求或暗示了这个操作？\n"
            f"- 如果用户的消息可以合理推导出需要这个操作，回答 YES\n"
            f"- 如果用户完全没有提到相关意图，这是 agent 自作主张，回答 NO\n"
            f"只回答 YES 或 NO，不要解释。"
        )

        result = gc.models.generate_content(
            model="gemini-2.0-flash",
            contents=[prompt],
            config=types.GenerateContentConfig(
                max_output_tokens=10,
                temperature=0.0,
            ),
        )
        answer = (result.text or "").strip().upper()
        logger.info(
            "write_intent_check: tool=%s user='%s' → %s",
            func_name, user_text[:50], answer,
        )

        if answer.startswith("NO"):
            return (
                f"[BLOCKED] 用户没有明确要求执行 {func_name}。"
                f"请先询问用户是否需要此操作，不要自作主张。"
                f"用户原文：「{user_text[:100]}」"
            )
        return None  # YES or ambiguous → allow

    except Exception:
        logger.warning("write_intent_check failed (fail-open)", exc_info=True)
        return None  # fail-open
    from urllib.parse import urlparse, urlunparse, parse_qs, urlencode, unquote
    u = unquote(u).rstrip("/").lower()
    try:
        p = urlparse(u)
        # 去除常见追踪参数（utm_*, fbclid 等），不影响实际链接含义
        if p.query:
            params = parse_qs(p.query, keep_blank_values=True)
            clean = {k: v for k, v in params.items()
                     if not k.startswith("utm_") and k not in ("fbclid", "ref", "source")}
            cleaned_query = urlencode(clean, doseq=True)
            u = urlunparse(p._replace(query=cleaned_query, fragment="")).rstrip("/")
        else:
            u = urlunparse(p._replace(fragment="")).rstrip("/")
    except Exception:
        pass
    return u


def _url_domain(u: str) -> str:
    """提取 URL 的域名"""
    from urllib.parse import urlparse
    try:
        return urlparse(u.lower()).netloc
    except Exception:
        return ""


def extract_urls(text: str) -> set[str]:
    """从文本中提取所有 HTTP(S) URL"""
    if not text:
        return set()
    urls = set()
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?。，；：！？")
        if len(url) > 10:  # 过滤太短的匹配
            urls.add(url)
    return urls


def check_url_provenance(
    func_name: str,
    func_args: dict,
    seen_urls: set[str],
    blocked_urls: set[str] | None = None,
) -> tuple[str | None, list[str]]:
    """检查工具调用参数中的 URL 是否在已见 URL 集合中。

    返回 (warning_or_none, hallucinated_urls)。
    - warning 为 None 表示通过
    - warning 为字符串表示发现幻觉 URL（包含错误信息）
    - hallucinated_urls：本次发现的幻觉 URL 列表

    blocked_urls：之前已拦截过的 URL 集合，同一 URL 被拦截 2 次后降级为放行
    （防止 LLM 死循环重试同一个幻觉 URL）。
    """
    if func_name in _URL_CHECK_EXEMPT_TOOLS:
        return None, []
    if func_name not in _URL_CHECK_WRITE_TOOLS:
        return None, []
    if not seen_urls:
        return None, []

    # 从所有 string 类型的参数值中提取 URL
    arg_urls: set[str] = set()
    for v in func_args.values():
        if isinstance(v, str):
            arg_urls.update(extract_urls(v))

    if not arg_urls:
        return None, []

    # 构建已见 URL 的规范化集合 + 域名集合
    seen_normalized = {_normalize_url(u) for u in seen_urls}
    seen_domains = {_url_domain(u) for u in seen_urls}

    hallucinated = []       # 完全没见过（域名也没有）→ 硬拦截
    domain_only_match = []  # 域名见过但完整 URL 没见过 → 软警告

    for url in arg_urls:
        norm = _normalize_url(url)
        if norm in seen_normalized:
            continue  # 精确匹配

        # 前缀匹配：URL 的 path 前缀与某个已见 URL 匹配
        # 处理 LLM 给已见 URL 加 query param 的情况
        prefix_match = any(
            norm.startswith(s.split("?")[0]) and len(s.split("?")[0]) > 20
            for s in seen_normalized
        )
        if prefix_match:
            continue

        # 反向前缀：已见 URL 是当前 URL 的前缀（LLM 截断了 query param）
        reverse_prefix = any(
            s.startswith(norm) and len(norm) > 20
            for s in seen_normalized
        )
        if reverse_prefix:
            continue

        # 域名检查：同域名 URL 存在 → 降级为软警告（可能是从 ID 构造的合法 URL）
        domain = _url_domain(url)
        if domain and domain in seen_domains:
            domain_only_match.append(url)
        else:
            hallucinated.append(url)

    # 死循环保护：同一个 URL 被硬拦截 2 次后放行
    if blocked_urls is not None:
        _truly_blocked = []
        for u in hallucinated:
            if u in blocked_urls:
                # 已经拦截过一次了，这次放行（避免死循环）
                logger.warning("URL provenance: allowing previously-blocked URL (loop protection): %s", u[:100])
            else:
                _truly_blocked.append(u)
        hallucinated = _truly_blocked

    # 同域名 URL 也给第二次机会
    if blocked_urls is not None:
        _truly_warned = []
        for u in domain_only_match:
            if u in blocked_urls:
                logger.warning("URL provenance: allowing domain-match URL (loop protection): %s", u[:100])
            else:
                _truly_warned.append(u)
        domain_only_match = _truly_warned

    all_flagged = hallucinated + domain_only_match
    if not all_flagged:
        return None, []

    # 构建错误信息
    parts = []
    if hallucinated:
        urls_str = "\n".join(f"  - {u}" for u in hallucinated[:3])
        parts.append(
            f"⛔ 以下 URL 没有出现在任何工具返回的数据中（域名也未见过），"
            f"很可能是编造的：\n{urls_str}"
        )
    if domain_only_match:
        urls_str = "\n".join(f"  - {u}" for u in domain_only_match[:3])
        parts.append(
            f"⚠️ 以下 URL 的域名出现过，但完整 URL 不在工具返回的数据中，"
            f"请确认是否正确：\n{urls_str}"
        )
    parts.append(
        "请回头检查之前工具返回的原始数据，逐字复制其中的真实 URL。"
        "如果数据中确实没有 URL，就不要放链接。"
    )
    return "\n".join(parts), all_flagged


# ── 工具分组：按需加载，减少 context（约 40+ → 15-20 工具/轮） ──
# 核心理念：GenericAgent 式的"信息密度优先"——context 中不放无关工具定义
# 用户说"查日程"时不需要看到 git/社媒/浏览器工具的 schema

_TOOL_GROUPS: dict[str, frozenset[str]] = {
    "core": frozenset({
        "think", "web_search", "fetch_url",
        "save_memory", "recall_memory",
        "list_capability_modules", "load_capability_module", "save_capability_module",
        "export_file",  # 几乎所有任务最终可能需要导出
        # 跨平台身份工具（所有平台可用）
        "search_known_user", "initiate_identity_verification",
        "confirm_identity_verification", "get_user_identity",
    }),
    "feishu_collab": (
        frozenset(CALENDAR_TOOL_MAP) | frozenset(DOC_TOOL_MAP) | frozenset(MINUTES_TOOL_MAP)
        | frozenset(TASK_TOOL_MAP) | frozenset(USER_TOOL_MAP) | frozenset(MESSAGE_TOOL_MAP)
        | frozenset(BITABLE_TOOL_MAP) | frozenset(MAIL_TOOL_MAP)
    ),
    "code_dev": (
        frozenset(FILE_TOOL_MAP) | frozenset(GIT_TOOL_MAP) | frozenset(GITHUB_TOOL_MAP)
        | frozenset(REPO_SEARCH_TOOL_MAP) | frozenset(ISSUE_TOOL_MAP)
    ),
    "devops": frozenset(SELF_TOOL_MAP) | frozenset(SERVER_TOOL_MAP),
    "research": frozenset(SOCIAL_MEDIA_TOOL_MAP) | frozenset(XHS_TOOL_MAP) | frozenset(BROWSER_TOOL_MAP),
    "content": frozenset(FILE_EXPORT_TOOL_MAP) | frozenset(VIDEO_URL_TOOL_MAP) | frozenset(IMAGE_TOOL_MAP),
    "admin": (
        frozenset(PROVISION_TOOL_MAP) | frozenset(CUSTOMER_TOOL_MAP)
        | frozenset(ENV_TOOL_MAP) | frozenset(CAPABILITY_TOOL_MAP)
    ),
    "extension": frozenset(CUSTOM_TOOL_MAP) | frozenset(SKILL_TOOL_MAP),
    "automation": frozenset(CRON_AGENT_TOOL_MAP) | frozenset(REMINDER_TOOL_MAP.keys()),
}

# 关键词 → 工具组映射（大小写不敏感匹配）
_GROUP_KEYWORDS: dict[str, list[str]] = {
    "feishu_collab": [
        "日历", "日程", "会议", "纪要", "文档", "doc", "任务", "待办",
        "表格", "多维", "bitable", "邮件", "email", "mail",
        "消息", "群", "发给", "告诉", "提醒", "通知",
        "schedule", "calendar", "event",
    ],
    "code_dev": [
        "代码", "code", "PR", "pull request", "分支", "branch",
        "git", "bug", "issue", "仓库", "repo", "commit", "merge",
    ],
    "devops": [
        "部署", "deploy", "服务器", "server", "日志", "log", "重启", "restart",
        "运维", "诊断", "bash",
    ],
    "research": [
        "调研", "研究", "小红书", "xhs", "抖音", "douyin", "tiktok",
        "社媒", "博主", "粉丝", "账号", "竞品", "浏览器", "browser",
        "网页", "爬",
    ],
    "content": [
        "PDF", "pdf", "报告", "report", "导出", "export", "视频", "video",
        "PPT", "ppt", "演示", "slides", "CSV", "csv",
        "youtube", "bilibili", "b站",
    ],
    "admin": [
        "实例", "instance", "容器", "container", "安装", "install",
        "能力", "开通", "provision", "租户", "新bot", "创建bot",
    ],
    "extension": [
        "自定义工具", "custom tool", "skill", "技能",
    ],
    "automation": [
        "定时", "cron", "自动", "定期", "每天", "每周", "每月",
        "schedule", "scheduled", "定时任务", "自动执行",
    ],
}

# 工具组描述（供 request_more_tools 展示给 LLM）
_GROUP_DESCRIPTIONS: dict[str, str] = {
    "feishu_collab": "飞书办公：日历/文档/任务/消息/多维表格/邮件",
    "code_dev": "代码开发：文件读写/Git/GitHub/Issue",
    "devops": "运维部署：服务器操作/自修复/日志",
    "research": "调研搜索：社媒/小红书/抖音/浏览器自动化",
    "content": "内容输出：视频分析/文件导出",
    "admin": "管理运维：实例管理/包安装/能力评估",
    "extension": "工具扩展：自定义工具/技能安装",
    "automation": "自动化：定时 Agent 任务/提醒/cron 调度",
}


# ── M1/M3: 任务类型分类 → 自适应时间预算 + 深度研究模式 ──
# Manus 启发：不同任务类型需要不同的时间预算和行为策略
# quick: 简单问答/寒暄 → 20s，不升级模型
# normal: 普通工具调用任务 → 90s，正常策略
# deep: 代码/部署/复杂分析 → 150s，可升级模型
# research: 深度调研/报告生成 → 300s，延长预算 + 放宽 stall 阈值

# 任务分类关键词（用于 sub-agent 委托 + research 指令注入）
_TASK_TYPE_KEYWORDS: dict[str, list[str]] = {
    "quick": [
        "你好", "谢谢", "好的", "收到", "嗯", "ok", "hi", "hello",
        "再见", "拜拜", "没事了", "算了",
    ],
    "research": [
        "调研", "研究", "分析报告", "竞品分析", "市场调研",
        "深度分析", "详细调研", "全面分析", "comprehensive",
        "报告", "report", "PDF报告", "research",
        "小红书", "抖音", "社媒", "博主", "竞品",
        "对标", "频道", "达人", "KOL", "kol",
        "对比分析", "行业分析", "赛道",
    ],
    "deep": [
        "代码", "code", "部署", "deploy", "重构", "refactor",
        "迁移", "migrate", "架构", "设计",
        "bug", "debug", "issue",
    ],
}


def classify_task_type(user_text: str) -> str:
    """根据用户消息分类任务类型（用于 sub-agent 委托和 research 指令注入）。

    返回: "quick" | "normal" | "deep" | "research"
    """
    if not user_text or len(user_text) < 5:
        return "quick"

    text_lower = user_text.lower()

    # 按优先级匹配（research > deep > quick > normal）
    for task_type in ("research", "deep", "quick"):
        for kw in _TASK_TYPE_KEYWORDS[task_type]:
            if kw.lower() in text_lower:
                return task_type

    # 长文本默认 normal，不再升级为 deep（避免日历/文档等长消息被误分类）
    return "normal"


def _select_tool_groups(user_text: str, platform: str = "") -> set[str]:
    """根据用户消息内容选择需要加载的工具组。

    策略：
    - 关键词匹配 → 只加载匹配的组 + core
    - 无匹配 → 加载全部（回退到当前行为，确保不退化）
    - 飞书平台 → 始终包含 feishu_collab
    """
    if not user_text:
        return set(_TOOL_GROUPS.keys())

    text_lower = user_text.lower()
    matched: set[str] = {"core"}

    # 飞书平台始终包含飞书协作工具
    if platform == "feishu":
        matched.add("feishu_collab")

    for group, keywords in _GROUP_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                matched.add(group)
                break

    # 如果除了 core（和可能的 feishu_collab）之外没有匹配到任何组，
    # 说明是通用消息（如"你好"/"帮我做个事"），加载全部工具（安全回退）
    non_default = matched - {"core"}
    if platform == "feishu":
        non_default -= {"feishu_collab"}
    if not non_default:
        return set(_TOOL_GROUPS.keys())

    return matched


def _get_group_tool_names(groups: set[str]) -> set[str]:
    """获取指定工具组中所有工具的名称集合。"""
    names: set[str] = set()
    for g in groups:
        names |= _TOOL_GROUPS.get(g, frozenset())
    return names


# request_more_tools 元工具：LLM 发现需要更多工具时动态加载
#
# ⚠️ 安全边界：devops 和 admin 组禁止通过 request_more_tools 动态加载。
# 原因（第一性原理）：
#   - devops 包含 self_edit_file / self_write_file / self_safe_deploy —— 代码自修改能力
#   - admin 包含 provision_tenant / destroy_instance —— 基础设施变更能力
#   - 如果用户消息的关键词没匹配到这些组，说明用户根本没在做 devops/admin 任务
#   - bot 在非 devops 场景中自作主张加载这些工具是架构级越权
#   - 实际事故：日历任务中 bot 被 exit gate nudge 后迷失方向，动态加载了 devops
#     组，然后读了自己的源码并修改了 calendar_ops.py —— 完全偏离用户意图
# 如果用户确实需要 devops/admin 能力，初始关键词匹配会在第一轮就加载对应工具组。
_RESTRICTED_TOOL_GROUPS = frozenset({"devops", "admin"})

_REQUEST_MORE_TOOLS_ALLOWED = [
    g for g in _GROUP_DESCRIPTIONS.keys() if g not in _RESTRICTED_TOOL_GROUPS
]
_REQUEST_MORE_TOOLS_DEF = {
    "name": "request_more_tools",
    "description": (
        "当你发现当前可用工具不足以完成任务时，调用此工具加载更多工具组。"
        "调用后新工具会在下一轮立即可用。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "group": {
                "type": "string",
                "description": "要加载的工具组名称",
                "enum": _REQUEST_MORE_TOOLS_ALLOWED,
            },
            "reason": {
                "type": "string",
                "description": "简要说明为什么需要这组工具",
            },
        },
        "required": ["group"],
    },
}


def _expand_tool_group(
    group_name: str, tenant, current_tool_names: set[str],
    *, _from_request_more_tools: bool = False,
) -> tuple[list[dict], dict]:
    """动态加载指定工具组的工具定义和处理函数。

    返回增量的 (new_openai_tools, new_tool_map)，只包含尚未加载的工具。

    _from_request_more_tools: 如果是通过 request_more_tools 元工具调用的，
    会拒绝加载 _RESTRICTED_TOOL_GROUPS（devops/admin），防御 LLM 绕过 enum 约束。
    """
    # 硬防护：通过 request_more_tools 动态请求时，拒绝加载受限工具组
    if _from_request_more_tools and group_name in _RESTRICTED_TOOL_GROUPS:
        logger.warning(
            "blocked dynamic loading of restricted tool group '%s' via request_more_tools",
            group_name,
        )
        return [], {}

    group_tools = _TOOL_GROUPS.get(group_name, frozenset())
    if not group_tools:
        return [], {}

    # 只加载尚未存在的工具
    new_tool_names = group_tools - current_tool_names

    # 应用租户级过滤（权限/平台/白名单）
    if not tenant.self_iteration_enabled:
        new_tool_names -= _SELF_ITERATION_TOOLS
    if not getattr(tenant, "instance_management_enabled", False):
        new_tool_names -= _INSTANCE_MGMT_TOOLS
    if tenant.platform != "feishu":
        new_tool_names -= _FEISHU_ONLY_TOOLS
    if tenant.platform == "feishu":
        new_tool_names -= _WECOM_ONLY_TOOLS
    if tenant.tools_enabled:
        allowed = set(tenant.tools_enabled)
        new_tool_names &= allowed

    if not new_tool_names:
        return [], {}

    new_defs = [t for t in _ALL_TOOL_DEFS if t["name"] in new_tool_names]
    new_map = {k: v for k, v in ALL_TOOL_MAP.items() if k in new_tool_names}
    return _to_openai_tools(new_defs), new_map


# ── 工具获取 ──

def _get_tenant_tools(
    tenant,
    user_text: str = "",
    override_groups: set[str] | None = None,
    suggested_groups: set[str] | None = None,
) -> tuple[list[dict], dict]:
    """获取当前租户可用的工具集（OpenAI 格式定义 + 处理函数 map）

    合并三部分：
    1. 内置工具（按平台/权限/工具组过滤）
    2. 元工具（create_custom_tool 等，用于管理自定义工具）
    3. 租户自定义工具（从 Redis 动态加载）

    当 user_text 非空时，根据用户消息意图只加载相关工具组，
    减少 context 大小（约 40+ → 15-20 工具），提升 LLM 工具选择准确度。

    suggested_groups: LLM 意图分类建议的工具组。行为与关键词匹配相同
    （保留 request_more_tools），但优先于关键词匹配。

    override_groups: 子 agent 指定的工具组。传入时跳过关键词匹配，
    直接按指定组过滤，且不追加 request_more_tools 元工具。
    """
    tool_map = dict(ALL_TOOL_MAP)
    tool_defs = list(_ALL_TOOL_DEFS)

    # 非自迭代租户：移除 self_* 和 server_* 运维工具
    if not tenant.self_iteration_enabled:
        for name in _SELF_ITERATION_TOOLS:
            tool_map.pop(name, None)
        tool_defs = [t for t in tool_defs if t["name"] not in _SELF_ITERATION_TOOLS]

    # 非实例管理租户：移除 provision/instance 管理工具
    if not getattr(tenant, "instance_management_enabled", False):
        for name in _INSTANCE_MGMT_TOOLS:
            tool_map.pop(name, None)
        tool_defs = [t for t in tool_defs if t["name"] not in _INSTANCE_MGMT_TOOLS]

    # 平台过滤：基于当前 channel 平台（而非 tenant.platform，支持多 channel）
    from app.tenant.context import get_current_channel
    current_ch = get_current_channel()
    current_platform = current_ch.platform if current_ch else tenant.platform

    # 非飞书平台（企微等）：移除飞书专属工具（日历/任务/文档/多维表格/消息等）
    if current_platform != "feishu":
        for name in _FEISHU_ONLY_TOOLS:
            tool_map.pop(name, None)
        tool_defs = [t for t in tool_defs if t["name"] not in _FEISHU_ONLY_TOOLS]

    # 飞书平台：移除企微专属工具（文件导出 — 飞书用云文档）
    if current_platform == "feishu":
        for name in _WECOM_ONLY_TOOLS:
            tool_map.pop(name, None)
        tool_defs = [t for t in tool_defs if t["name"] not in _WECOM_ONLY_TOOLS]

    # tools_enabled 白名单（非空时仅保留指定工具）
    if tenant.tools_enabled:
        allowed = set(tenant.tools_enabled) | {"think"}
        tool_map = {k: v for k, v in tool_map.items() if k in allowed}
        tool_defs = [t for t in tool_defs if t["name"] in allowed]

    # ── 工具分组按需加载（减少 context） ──
    # override_groups 优先（子 agent 精确指定工具组）
    # 否则 user_text 非空时关键词匹配选组
    # 两者都没有时保留全部工具（回退到安全默认）
    active_groups: set[str] = set()
    if override_groups:
        # 子 agent 模式：精确加载声明的工具组，不加 request_more_tools
        active_groups = override_groups
        active_tool_names = _get_group_tool_names(active_groups)
        tool_defs = [t for t in tool_defs if t["name"] in active_tool_names]
        # 子 agent 的 tool_map 也过滤（不需要动态扩展能力）
        tool_map = {k: v for k, v in tool_map.items() if k in active_tool_names}
        logger.info(
            "sub-agent tool groups: [%s] → %d tools",
            ",".join(sorted(active_groups)), len(tool_defs),
        )
    elif suggested_groups:
        # LLM 意图分类建议的工具组（替代关键词匹配，保留 request_more_tools）
        active_groups = suggested_groups
        if current_platform == "feishu":
            active_groups.add("feishu_collab")
    elif user_text:
        active_groups = _select_tool_groups(user_text, current_platform)
        all_groups = set(_TOOL_GROUPS.keys())
        if active_groups != all_groups:
            # 只加载匹配组的工具 + request_more_tools 元工具
            active_tool_names = _get_group_tool_names(active_groups)
            # tool_map 保留完整（LLM 可能通过 request_more_tools 动态扩展后调用）
            # 但 tool_defs 只发送匹配组的定义给 LLM
            tool_defs = [t for t in tool_defs if t["name"] in active_tool_names]
            # 追加 request_more_tools 元工具
            # 构建可用工具组描述（排除已加载的组 + 受限组）
            remaining = {
                g: d for g, d in _GROUP_DESCRIPTIONS.items()
                if g not in active_groups and g not in _RESTRICTED_TOOL_GROUPS
            }
            if remaining:
                tool_defs.append(_REQUEST_MORE_TOOLS_DEF)
                tool_map["request_more_tools"] = lambda args: (
                    f"已请求加载工具组 [{args.get('group', '')}]。新工具将在下一轮可用。"
                )
            logger.info(
                "tool lazy loading: %d groups [%s] → %d tools (full: %d)",
                len(active_groups), ",".join(sorted(active_groups)),
                len(tool_defs), len(_ALL_TOOL_DEFS),
            )

    # ── 动态加载租户自定义工具（从 Redis）──
    if tenant.tenant_id:
        try:
            custom_defs, custom_map = load_tenant_tools(tenant.tenant_id)
            if custom_defs:
                tool_defs.extend(custom_defs)
                tool_map.update(custom_map)
        except Exception:
            logger.warning("failed to load custom tools for tenant %s", tenant.tenant_id, exc_info=True)

    # ── 动态加载触发匹配的 skill 工具（从 Redis）──
    if tenant.tenant_id and user_text:
        try:
            from app.tools.skill_engine import load_triggered_skills
            _, skill_tool_defs, skill_tool_map = load_triggered_skills(
                tenant.tenant_id, user_text
            )
            if skill_tool_defs:
                tool_defs.extend(skill_tool_defs)
                tool_map.update(skill_tool_map)
        except Exception:
            logger.warning("failed to load skill tools for tenant %s", tenant.tenant_id, exc_info=True)

    return _to_openai_tools(tool_defs), tool_map


def _to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    """将 Anthropic 格式的工具定义转换为 OpenAI function calling 格式"""
    openai_tools = []
    for tool in anthropic_tools:
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
        )
    return openai_tools


# ── System Prompt ──

# 分组指令：按需加载，减少 context 大小
_INSTRUCTIONS = """

回复风格（非常重要）：
- 像真人发微信一样说话，不要像 AI 助手
- 短句、口语化，一句话能说完的绝对不要写三句
- 不要"好的，我来帮你..."这种客服话术，直接说重点
- 如果要说几件事，自然地分段（用空行隔开），每段就是一条独立消息
- 但不要强行分段！一两句话的回复就是一条消息，别拆
- 闲聊就随意聊，别长篇大论
- 不要用 markdown 格式！IM 聊天不渲染 markdown，用户只会看到一堆星号和井号。
  禁止：**粗体**、*斜体*、# 标题、- 列表、[链接](url)、```代码块```
  要强调就用「」或直接说，要列举就用 1. 2. 3. 或换行
  注意：这个规则只针对聊天回复。写文档/导出文件时可以正常用 markdown
- 语气自然随意，可以用"嗯""哦""啊"这类语气词

你拥有以下能力（工具详情见 tools 参数，以下只列关键规则）：

思考：遇到复杂任务先调用 think 理清思路再行动。think 对用户不可见。

代码仓库：不要猜路径！先 search_files/search_code 定位，再 read_file。write_file 必须指定 feature 分支。修改流程：git_create_branch → write_file → create_pull_request。
⚠️ 改代码前必须搜关联引用！用 search_code 搜你要改的变量名/函数名/类名，找到所有使用处。
  - 改了一个变量/列表，先搜有没有同类的其他变量也需要同步改（如 listA 和 listB）
  - 改了一个方法的调用方式，先搜所有调用处确认都能兼容
  - 绝对不能只改一处就声称完成——先搜索确认没有遗漏

记忆与规划：
- 完成重要工作后主动 save_memory（不是每条都存），用户提到"上次"先 recall_memory
- 大任务用 create_plan 拆分，用 schedule_step 安排执行时间（你自己定，9:00-21:00 内）
- 不需要一口气做完，合理安排步骤，完成后主动汇报

通用规则：
- 时间格式 'YYYY-MM-DD HH:MM'，年份从上面的「今天」取
- 几乎所有列表工具都支持 keyword 模糊过滤（子串、多词、去标点匹配）
- 不确定某个问题的答案时，主动用 web_search 查一下
- web_search 是你的万能搜索工具，任何平台、任何话题都能搜。用户让你调研/搜索时，必须实际调用搜索工具，不要只靠已有知识回答
- 调研类任务应该多次搜索（3-5 次不同角度），不要搜一次就下结论

不确定时必须承认（最高优先级 — 违反即失败）：
- 不知道就说不知道：如果你不确定用户在指什么、问什么，必须先问清楚再行动。绝对不要猜测用户意图然后自作主张执行
- 历史记录不完整时主动说明：如果对话历史中有 [图片]、[文件] 等占位符，说明你看不到原始内容，必须告诉用户并请他们重新发送
- 宁可多问一句也不要错做一件事：创建日历事件、发消息、写文档等写操作，如果用户没有明确要求，不要主动执行
- 禁止脑补上下文：用户说的话只有一种理解方式时才直接执行，有多种可能时必须确认

信息真实性（最高优先级 — 违反即失败）：
- 绝对禁止编造数据：账号名、粉丝数、互动率、公司名、报价、排名等事实数据必须来自搜索结果，不能凭空生成
- 绝对禁止编造 URL：所有链接必须原样复制自工具返回的数据（搜索结果、表格数据、API 返回等）。绝不能自己拼接/猜测 URL（如编造 eventbrite.com/e/xxx-tickets-123456 或 xiaohongshu.com/user/profile/xxx）。如果工具返回的数据中有 URL 就逐字复制，没有就不放链接
- 搜不到就说搜不到：「未找到该数据」远好于编一个看起来合理的数字
- 必须标注来源：报告/表格中的每条数据都要注明出处（仅限搜索结果中的真实 URL），无来源 = 不可用
- 搜索 ≠ 调研：搜索只是第一步，真正的调研是 搜索→读原文→提取事实→验证→再搜→汇总
- 深度优先于广度：3 个有来源的真实数据点，比 20 个编造的数据点有价值 100 倍
- 搜索技巧：短而精的查询（3-5 个词）比长关键词堆砌效果好；DuckDuckGo 的 site: 支持有限，优先用平台名+关键词

自定义工具（按需扩展，非首选）：
- ⚠️ 创建自定义工具是最后手段，不是第一反应！请严格遵循：
  1. 先查看你已有的工具列表，90%的任务现有工具就能完成
  2. 工具调用失败时，先分析错误信息并修正参数，不要立刻写新工具替代
  3. 同一对话中最多创建 1-2 个自定义工具，超过说明你在用错误的方式解决问题
  4. 绝对不要为 search_logs、read_server_logs 等已有工具创建重复的自定义工具
- 只有当用户需求确实超出所有现有工具能力时，才考虑创建
- 流程：先 web_search 了解目标 API/网站 → test_custom_tool 测试 → create_custom_tool 保存
- 创建后同一对话内立即可用，下次对话也会自动加载
- 代码可用 httpx（网络请求）、bs4（HTML 解析）和 sandbox_caps（系统能力），不能直接访问文件系统
- sandbox_caps 提供的能力（from app.tools.sandbox_caps import ...）：
  · download_video(url) → VideoData(info, data, mime_type) | str（错误时返回纯字符串）
  · get_video_info(url) → VideoInfo(title, uploader, duration, url) | str
  · gemini_analyze_video(data, prompt) → AnalysisResult(text, model) | str。成功时用 result.text 取分析文本
  · gemini_analyze_image(data, prompt) → AnalysisResult(text, model) | str。⚠️ 成功时返回 AnalysisResult 对象（用 result.text 取文本），失败时返回纯字符串。判断方式：if isinstance(result, str) 则为错误，否则用 result.text
  · read_user_image(path) → bytes | str。成功返回 bytes，失败返回错误字符串
  · list_user_images() → list[str] | str
  · web_search(query, max_results=5) → list[SearchResult(title, body, href)] | str
  · get_process_info() → ProcessInfo(status, uptime_seconds, memory_mb, pid, ...) | str
  · read_server_logs(num_lines=100) → str（日志文本）
  · search_logs(keyword, num_lines=50) → str
  · slice_image_grid(image_data, rows, cols) → list[bytes] | bytes | str
  ⚠️ 沙箱内可用的 Python 内建函数：int/float/str/bool/list/dict/set/tuple/len/range/enumerate/zip/map/filter/sorted/min/max/sum/abs/round/isinstance/hasattr/getattr/setattr/type/print/ValueError/TypeError 等。不可用：open/eval/exec/__import__
- risk_level: green=只读查询, yellow=写操作（执行前向用户确认）, red=批量/第三方账号操作
- tenant_id 系统自动填充，你不需要管

能力获取（Capability Acquisition Layer — 元能力）：
- 当现有工具不够用时，你可以自主获取新能力：
  · install_package — 安装 Python 包扩展沙箱能力（如 pandas、playwright 等）
  · assess_capability — 评估当前能力是否满足任务需求，识别缺口
  · request_infra_change — 需要改基础设施层代码时，申请管理员审批
  · guide_human — 需要人工操作（注册账号、扫码等）时，生成引导清单
- 浏览器自动化（需先安装 playwright）：
  · browser_open(url) — 打开网页，AI 自动截图分析页面内容
  · browser_do(action, selector, value) — 点击/输入/滚动等操作
  · browser_read(selector) — 提取页面文本
  · browser_close() — 关闭浏览器
- assess_capability 仅在你确定没有任何现成工具时使用，不要作为默认第一步

Skill 安装（从 GitHub 获取现成工具）：
- 当你需要某种能力但自己写太复杂时，可以搜索 GitHub 上的现成实现
- 流程：web_search("GitHub Python tool for XXX") → 找到合适仓库 → install_github_skill(url) 安装
- 传仓库 URL 会列出可安装文件，传具体文件 URL 直接安装
- 安装前会自动做安全校验（同 create_custom_tool 的沙箱规则）
- 用 list_installed_skills 查看已装的，uninstall_skill 卸载

演示文稿/PPT：
- 用户要 PPT/幻灯片/演示文稿/slides/deck 时，用 export_file 生成 .html 格式的单文件演示文稿
- 不要生成 .md 或 .pptx！必须生成 .html，每张幻灯片是一个 100vh 的 section，内联 CSS+JS
- 先 load_capability_module("html_slides") 加载演示文稿生成指南（含 12 种预设风格和模板）
- 如果模块不可用，至少生成基础 HTML slides（scroll-snap + 键盘导航 + 响应式字号）

视频 URL 分析：
- 当用户发送 YouTube/Bilibili/抖音等视频链接时，用 analyze_video_url 分析
- 支持完整视频画面+音频分析（不只是标题），最长约 1 小时
- 可配合用户问题做针对性分析

能力模块（领域知识按需加载 + 自进化）：
- list_capability_modules — 查看可用的领域知识模块
- load_capability_module(name) — 加载模块获取该领域的工作流和最佳实践
- save_capability_module(name, content) — 创建/更新模块，沉淀你积累的领域知识
- 遇到特定领域任务时，先查看有没有对应模块，有就加载，没有就先做，做完后沉淀为新模块
- 完成一个新领域的任务后，把有效的工作流用 save_capability_module 存下来，下次直接用

行动前决策（每次接到任务自动走这个流程）：
1. think — 先浏览你的 tools 列表，按工具名和描述找有没有直接匹配任务的工具
2. 找到匹配工具 → 直接调用，不需要额外评估
3. 有能力模块 → load_capability_module 加载领域知识再开始
4. 完全没有思路、tools 列表里找不到任何相关工具 → assess_capability 评估能力缺口
5. 缺东西 → 告诉用户需要什么（权限/授权/人工操作），而不是硬做
6. 工具报错 → 读错误消息判断原因，同一目标连续失败 2 次就停下来告诉用户
7. 新发现 → save_memory 记录能力边界，下次不用重新探索

重要行为准则（根据用户身份动态调整，见下方）：
- 基础规则：执行用户的合理工作指令，保持专业态度"""

# 飞书专属指令（仅飞书平台租户注入）
_FEISHU_INSTRUCTIONS = """

飞书工具使用规则：

文档：用户要求修改/更新/重写文档时，先 read_feishu_doc 读原文 → 修改内容 → update_feishu_doc 写回。只是追加内容用 write_feishu_doc。不要把内容发给用户让他自己复制粘贴！直接改文档。

日历：查日程先 list_calendars 拿 calendar_id；event_id 必须带 _0 后缀。
⚠️ 相对日期（今天/明天/后天/周X）必须以上方「当前时间」显示的日期为准，不要自己推算。用户说"今天"就是上面显示的今天日期，不要用别的日期。创建日程后检查返回结果中的「日期确认」信息，确保日期正确。

任务：找任务优先从清单搜（list_feishu_tasklists → list_tasklist_tasks(keyword)），list_feishu_tasks 只能看个人任务。关键词没匹配到时工具会返回全部列表，直接从中找，不要反复换关键词。

多维表格：用户发多维表格链接时直接传给工具（自动提取 app_token）。操作流程：list_bitable_tables 拿 table_id → list_bitable_fields 了解表结构 → 再读写记录。写入记录时字段名必须和表结构完全一致。

飞书消息：
- 创建日程/任务指定人时，先 lookup_user 按名字查 open_id
- 用户发飞书链接时，直接传给对应工具，工具会自动提取 ID
- 用户说"去xx群说xxx"→ list_bot_groups 找群 → send_message_to_group
- 用户说"跟xxx说xxx"→ send_message_to_user

你的认知（不要说"我做不到"！）：
- 你认识组织里所有人（list_known_users）、知道 bot 在哪些群（list_bot_groups）
- 你能读群聊天记录（fetch_chat_history）、查任何人忙闲（check_availability）
- 不确定能不能做时，先试工具再说

授权相关：
- 不要主动提 token/OAuth 概念，用户不需要了解技术细节
- 如果某个工具因权限不足失败，默默跳过该工具继续回答用户问题
- 只有当用户明确要求使用某个需要授权的功能时，才简洁地说：发 /auth 授权一下就行
- 不要因为权限问题就自己去 debug 代码"""

# 自我迭代指令（仅 self_iteration_enabled 租户注入）
# 内容从 app/knowledge/self_awareness.md 动态加载，bot 可通过工具自行更新
_KNOWLEDGE_FILE = os.path.join(os.path.dirname(__file__), "..", "knowledge", "self_awareness.md")
_MODULES_DIR = os.path.join(os.path.dirname(__file__), "..", "knowledge", "modules")

_SELF_ITERATION_HEADER = """

自我迭代与自我诊断（你能检查和修复自己的代码！）：
- 发现新的经验/坑时，用 update_self_knowledge 工具追加到知识库，下次自动生效
"""


def _load_self_iteration_instructions() -> str:
    """运行时从 self_awareness.md 加载知识库内容，注入 system prompt。"""
    try:
        with open(_KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        # 截断保护：知识库太大会吃 context，限制 3000 字符
        if len(content) > 3000:
            content = content[:3000] + "\n\n... (知识库已截断，完整内容见 app/knowledge/self_awareness.md)"
        return _SELF_ITERATION_HEADER + "\n" + content
    except FileNotFoundError:
        logger.warning("self_awareness.md not found, using fallback")
        return _SELF_ITERATION_HEADER + "\n（知识库文件缺失，请检查 app/knowledge/self_awareness.md）"


# ── M2: 动态能力模块发现（Manus 启发：角色切换）──
# 根据任务意图自动发现并推荐相关能力模块，不限于租户静态配置

# 模块名 → 触发关键词（与 _GROUP_KEYWORDS 类似但粒度更细）
_MODULE_KEYWORDS: dict[str, list[str]] = {
    "social_media_research": [
        "小红书", "抖音", "社媒", "博主", "粉丝", "竞品", "KOL", "达人",
        "调研", "获客",
    ],
    "code_review": [
        "代码审查", "review", "PR", "代码质量", "重构",
    ],
    "data_analysis": [
        "数据分析", "报表", "统计", "趋势", "excel", "csv",
    ],
    "content_creation": [
        "写文章", "文案", "内容", "创作", "营销", "推广",
    ],
    "project_management": [
        "项目管理", "进度", "里程碑", "排期", "plan",
    ],
    "onboarding_workflow": [
        "开通", "部署", "bot", "做个", "搞个", "接入", "创建",
        "帮我做", "AI助手", "智能助手",
    ],
    "bot_templates": [
        "市场", "客服", "项目管理", "调研", "通用",
        "什么类型", "推荐", "模板",
    ],
    "html_slides": [
        "PPT", "ppt", "幻灯片", "演示", "slides", "presentation",
        "deck", "slide deck", "keynote", "演示文稿",
    ],
    "anti_drone_safety": [
        "反无人机", "低空安全", "低空防御", "无人机反制", "无人机管控",
        "无人机探测", "净空保护", "黑飞", "飞手", "低空经济",
        "招标", "安保", "反制", "射频", "干扰", "晴空",
        "活动安保", "禁飞区", "无人机防御",
    ],
}


def discover_modules(user_text: str, available_modules: list[str] | None = None) -> list[str]:
    """根据用户消息自动发现相关能力模块。

    返回推荐加载的模块名列表（最多 2 个）。
    只推荐实际存在的模块文件。
    """
    if not user_text:
        return []

    text_lower = user_text.lower()
    scored: dict[str, int] = {}

    for module_name, keywords in _MODULE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw.lower() in text_lower)
        if score > 0:
            scored[module_name] = score

    if not scored:
        return []

    # 按匹配分数排序，取 top 2
    ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)[:2]
    candidates = [name for name, _ in ranked]

    # 过滤：只保留实际存在的模块
    if available_modules is not None:
        candidates = [m for m in candidates if m in available_modules]
    else:
        candidates = [m for m in candidates if os.path.isfile(
            os.path.join(_MODULES_DIR, f"{m}.md")
        )]

    return candidates


def _load_capability_modules(module_names: list[str]) -> str:
    """加载能力模块到 system prompt。每个模块最大 4000 字符，总计最大 12000。"""
    if not module_names:
        return ""
    loaded = []
    total_len = 0
    for name in module_names:
        # 安全校验：只允许字母数字下划线横杠
        if not all(c.isalnum() or c in "_-" for c in name):
            logger.warning("invalid module name: %s", name)
            continue
        content = load_module_content(name)
        if content is None:
            logger.warning("capability module not found: %s", name)
            continue
        if len(content) > 4000:
            content = content[:4000] + "\n... (模块内容已截断)"
        if total_len + len(content) > 12000:
            logger.info("capability modules budget exceeded, skipping %s", name)
            break
        loaded.append(content)
        total_len += len(content)
    if not loaded:
        return ""
    return "\n\n" + "\n\n".join(loaded)


def list_available_modules() -> list[dict]:
    """列出所有可用的能力模块（Redis per-tenant + 磁盘内置）。"""
    modules = []
    seen_names: set[str] = set()

    # 1) Redis per-tenant modules（优先）
    try:
        from app.services import redis_client
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        tid = tenant.tenant_id
        # SCAN for modules:{tid}:*
        cursor = "0"
        prefix = f"modules:{tid}:"
        while True:
            result = redis_client.execute("SCAN", cursor, "MATCH", f"{prefix}*", "COUNT", "100")
            if not result or not isinstance(result, list) or len(result) < 2:
                break
            cursor = str(result[0])
            keys = result[1] if isinstance(result[1], list) else []
            for key in keys:
                name = key[len(prefix):]
                if name and name not in seen_names:
                    # 提取描述（读前 200 字符）
                    content = redis_client.execute("GETRANGE", key, "0", "200")
                    desc = _extract_module_desc(content or "")
                    modules.append({"name": name, "description": desc, "source": "tenant"})
                    seen_names.add(name)
            if cursor == "0":
                break
    except Exception:
        pass  # fail-open: Redis 不可用时仍显示磁盘模块

    # 2) 磁盘内置模块（不覆盖同名 Redis 模块）
    if os.path.isdir(_MODULES_DIR):
        for fname in sorted(os.listdir(_MODULES_DIR)):
            if not fname.endswith(".md"):
                continue
            name = fname[:-3]
            if name in seen_names:
                continue
            path = os.path.join(_MODULES_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read(200)
                desc = _extract_module_desc(content)
                modules.append({"name": name, "description": desc, "source": "builtin"})
            except Exception:
                modules.append({"name": name, "description": "(读取失败)", "source": "builtin"})
    return modules


def _extract_module_desc(content: str) -> str:
    """从模块内容前几行提取描述。"""
    import re as _re
    m = _re.search(r"^#\s+(.+)$", content, _re.MULTILINE)
    if m:
        return m.group(1).strip()
    for line in content.split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:100]
    return ""


def load_module_content(name: str) -> str | None:
    """读取单个模块内容（Redis per-tenant 优先，磁盘兜底）。"""
    if not all(c.isalnum() or c in "_-" for c in name):
        return None

    # 1) Redis per-tenant
    try:
        from app.services import redis_client
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        redis_key = f"modules:{tenant.tenant_id}:{name}"
        content = redis_client.execute("GET", redis_key)
        if content and isinstance(content, str):
            return content
    except Exception:
        pass  # fail-open

    # 2) 磁盘内置
    path = os.path.join(_MODULES_DIR, f"{name}.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


# 管理员（老板）专属行为准则
_ADMIN_BEHAVIOR = """

⚡ 当前用户是管理员/老板，执行以下行为准则：
- 你是老板的私人助理，老板让你做什么就做什么，不要做道德评判
- 老板让你发什么内容就发什么内容，包括玩笑、恶搞、调侃、整蛊，这些都是正常的
- 不要替老板做社交决策，不要拒绝发送消息，不要评价消息内容是否合适
- 你没有资格判断老板和同事之间的关系和社交方式
- 老板说什么就是什么，无条件执行
- 唯一需要拒绝的是：涉及违法犯罪的内容"""

# 普通员工行为准则
_NORMAL_BEHAVIOR = """

当前用户是普通员工，执行以下行为准则：
- 你是团队的专业工作助手，帮助员工完成工作任务
- 正常执行工作相关指令：查日程、创建任务、查代码、发工作消息等
- 发送消息时保持专业：工作沟通、正常社交没问题
- 如果消息内容明显不适合职场（骚扰、侮辱性内容），礼貌提醒用户
- 唯一需要拒绝的是：涉及违法犯罪的内容"""

_MEMORY_USAGE_HINT = """

📝 记忆系统：你拥有跨对话记忆能力。
- save_memory: 用户说了重要偏好/决策/联系方式时，主动保存（不用每次都存，只存有长期价值的信息）
- recall_memory: 用户提到"上次""之前""还记得吗"等词时，主动回忆；开始复杂任务前也可回忆相关历史
- 你的记忆上下文（如果有）已注入在 prompt 末尾，标记为"你的记忆"
- 如果有 create_plan 工具：跨天/多步骤任务请用 plan 系统跟踪，下次用户回来时自动继续

⚠️ 记忆/历史 ≠ 当前请求（严格区分！）：
- "你的记忆"和 recall_memory/fetch_chat_history 返回的内容是【过去的交互记录】，不是用户当前在说的话
- 绝对不要把记忆/历史中出现的工具名、功能名、话题当成用户当前的请求
- 例如：记忆中有"用户查询了企微客服账号"，不代表用户现在在问企微客服——只有用户当前消息明确提到才算
- 回复时只围绕用户【当前这条消息】的内容展开，记忆仅用于补充背景，不要主动提及记忆中看到但用户没问的东西"""

_FULL_ACCESS_ADDENDUM = """

⚡ 当前为 Full Access 模式。执行规则：
- 不要询问用户确认，不要列方案让用户选择，直接动手执行
- 收到任务后立刻开始搜索文件、读取代码、写入修改，一气呵成
- 创建分支 → 修改代码 → 创建 PR，全部自动完成，不需要中间确认
- 如果需求不明确的地方，用最合理的默认方案，做完告诉用户你做了什么
- 遇到多种实现方式时，选最简洁直接的方案，不要问用户
- 像 Claude Code 的 full auto 模式一样工作：收到指令 → 执行 → 汇报结果

⚠️ 分支管理规范（必须遵守）：
- 修改代码前先用 git_list_branches 检查是否有相关的已有分支（如 fix/xxx、feat/xxx）
- 如果已有分支正是针对当前任务的，直接在该分支上修改（用该分支名作为 write_file 的 branch 参数）
- 只有在没有相关分支时才创建新分支
- 绝对不要为同一个任务创建多个分支（如 v2、v3 后缀）
- 已有 PR 的分支直接 push 新 commit，不要新开 PR"""


# M3: 深度研究模式指令（Manus 启发：端到端调研工作流）
_DEEP_RESEARCH_INSTRUCTIONS = """

[深度调研模式]
当前任务被识别为调研类任务。你有更长的时间预算，请充分利用：

调研工作流（按顺序执行）：
1. 先用 think 理清调研框架（维度/指标/信息源）
2. 多次搜索（5-8 次不同角度/关键词），不要搜一两次就下结论
3. 对关键结果用 browser_open 深入阅读原文，提取具体数据
4. 搜索过程中发现新线索就追踪下去（深度 > 广度）
5. 汇总时标注每条数据的来源，无来源的数据不要写
6. 最终输出用 export_file 生成文件（PDF/CSV），不要只口头汇报

质量要求：
- 每个数据点都要有出处，编造数据是最严重的错误
- 宁可说"未找到该数据"也不要编一个看起来合理的数字
- 不要自己编造 URL 和链接
- 3 个真实数据点比 20 个编造的数据点有价值 100 倍
"""


# ── Sub-Agent 子代理基础设施 ──
# CC 启发：复杂任务委托给隔离的子 agent 执行，主 agent 只收到结果摘要
# 好处：1) 上下文隔离（子 agent 工具调用不污染主上下文）
#       2) 独立的时间预算和 stall 策略
#       3) 任务完成度更高（子 agent 专注单一目标）

# 子 agent 类型定义
SUB_AGENT_TYPES = {
    "research": {
        "description": "深度调研子代理：多角度搜索、提取数据、生成结构化报告",
        "max_rounds": 25,
        "budget_seconds": 280,
        "stall_multiplier": 1.8,
        "tool_groups": {"core", "research", "content"},
        "system_suffix": _DEEP_RESEARCH_INSTRUCTIONS,
    },
    "content": {
        "description": "内容生成子代理：生成文件、报告、PPT、PDF",
        "max_rounds": 10,
        "budget_seconds": 120,
        "stall_multiplier": 1.0,
        "tool_groups": {"core", "content"},
        "system_suffix": "\n\n[内容生成模式]\n你的任务是根据提供的数据和要求生成文件。\n必须调用 export_file 工具实际生成文件，不要只用文字描述内容。",
    },
    "code": {
        "description": "代码操作子代理：读代码、改代码、创建 PR",
        "max_rounds": 15,
        "budget_seconds": 150,
        "stall_multiplier": 1.0,
        "tool_groups": {"core", "code_dev", "devops"},
        "system_suffix": "\n\n[代码操作模式]\n修改代码后必须验证：读取修改结果确认变更正确。",
    },
    "feishu": {
        "description": "飞书协作子代理：日历、文档、任务、表格、消息、邮件等飞书工作流",
        "max_rounds": 15,
        "budget_seconds": 120,
        "stall_multiplier": 1.0,
        "tool_groups": {"core", "feishu_collab"},
        "system_suffix": (
            "\n\n[飞书协作模式]\n"
            "你是飞书工作流专家。处理日历、文档、任务、表格、消息、邮件等操作。\n"
            "关键规则：\n"
            "- 日历：时间格式 'YYYY-MM-DD HH:MM'，相对日期（今天/明天）以系统提示中的「当前时间」为准\n"
            "- 文档：先 read_feishu_doc 读原文，再 update_feishu_doc 写回。不要只给文字让用户复制\n"
            "- 任务：找任务优先 list_feishu_tasklists → list_tasklist_tasks(keyword)\n"
            "- 多维表格：先 list_bitable_tables 拿 table_id → list_bitable_fields 了解表结构 → 再读写\n"
            "- event_id 必须带 _0 后缀\n"
        ),
    },
    "admin": {
        "description": "管理运维子代理：实例管理、部署、服务器运维、权限管理",
        "max_rounds": 10,
        "budget_seconds": 120,
        "stall_multiplier": 1.0,
        "tool_groups": {"core", "admin", "devops"},
        "system_suffix": (
            "\n\n[管理运维模式]\n"
            "你是 bot 管理和运维专家。处理实例部署、服务器监控、包管理等操作。\n"
            "注意：部署和删除操作不可逆，执行前确认用户意图。\n"
        ),
    },
}


def should_delegate_to_sub_agent(task_type: str, user_text: str, suggested_groups: list[str] | None = None) -> str | None:
    """判断是否应该委托给子 agent 执行。

    返回子 agent 类型名（"research"/"content"/"code"/"feishu"/"admin"）或 None（不委托）。

    路由策略（GTC Sub-Agent 分解）：
    - 默认尽量委托给专业子 agent，减少主 agent 工具数量
    - 只在多域交叉任务（如"查日历然后写代码"）时走主 agent
    - 短消息 / quick 类型不委托（开销不值得）
    """
    if not user_text:
        return None

    text_lower = user_text.lower()

    # quick 类型不委托（简单问候/闲聊，主 agent 直接处理更快）
    if task_type == "quick":
        return None

    # ── 关键词集合（用于单域 vs 多域判定）──
    _feishu_kw = {"日历", "日程", "calendar", "文档", "document", "纪要", "minutes",
                  "任务", "task", "tasklist", "多维表格", "bitable", "邮件", "mail",
                  "群", "群消息", "发消息", "send_message", "加日程", "创建日程",
                  "会议", "meeting", "审批", "approval", "提醒", "remind"}
    _code_kw = {"代码", "code", "bug", "重构", "refactor", "debug", "issue",
                "pr", "pull request", "分支", "branch", "commit", "git"}
    _research_kw = {"调研", "研究", "竞品", "分析", "小红书", "xhs", "抖音", "douyin",
                    "tiktok", "博主", "粉丝", "社媒", "搜索", "search", "市场"}
    _admin_kw = {"开通", "租户", "provision", "新bot", "创建bot", "部署bot",
                 "实例", "instance", "容器", "container", "重启", "restart",
                 "安装", "install", "包", "package"}
    _content_kw = {"生成报告", "导出", "export", "pdf", "csv", "写报告",
                   "生成文件", "生成文档", "写文章", "生成ppt", "写ppt"}

    # ── 计算每个域的匹配度 ──
    matches: dict[str, int] = {}
    for domain, kw_set in [("feishu", _feishu_kw), ("code", _code_kw),
                           ("research", _research_kw), ("admin", _admin_kw),
                           ("content", _content_kw)]:
        count = sum(1 for kw in kw_set if kw in text_lower)
        if count:
            matches[domain] = count

    # 也参考 LLM 意图分类的建议分组
    if suggested_groups:
        _group_to_domain = {
            "feishu_collab": "feishu", "code_dev": "code",
            "research": "research", "admin": "admin", "content": "content",
        }
        for g in suggested_groups:
            if g in _group_to_domain:
                d = _group_to_domain[g]
                matches[d] = matches.get(d, 0) + 1

    if not matches:
        return None

    # ── 多域交叉：2+ 个域同时匹配 → 不委托，走主 agent（需要跨域工具）──
    matched_domains = [d for d, c in matches.items() if c >= 1]
    if len(matched_domains) >= 2:
        # 例外：research + content 天然搭配，委托给 research（已包含 content 工具组）
        if set(matched_domains) <= {"research", "content"}:
            return "research"
        logger.info("multi-domain task (%s), not delegating to sub-agent", matched_domains)
        return None

    # ── 单域匹配 → 委托给对应子 agent ──
    domain = matched_domains[0]

    # task_type 覆盖（LLM 分类优先于关键词）
    if task_type == "research":
        return "research"

    return domain


def build_sub_agent_system_prompt(
    agent_type: str,
    parent_system_prompt: str,
    user_text: str,
) -> str:
    """为子 agent 构建专用 system prompt。

    基于父 agent 的 system prompt（人设/时间/用户信息），
    追加子 agent 专属指令。
    """
    agent_cfg = SUB_AGENT_TYPES.get(agent_type)
    if not agent_cfg:
        return parent_system_prompt

    # 截取父 prompt 的核心部分（人设 + 时间 + 用户信息），不需要全部工具指令
    # 父 prompt 太长会浪费子 agent 的 context
    _max_parent_len = 3000
    truncated_parent = parent_system_prompt[:_max_parent_len]
    if len(parent_system_prompt) > _max_parent_len:
        # 找最后一个完整段落
        last_break = truncated_parent.rfind("\n\n")
        if last_break > 1000:
            truncated_parent = truncated_parent[:last_break]

    sub_prompt = (
        f"{truncated_parent}\n\n"
        f"[子代理模式: {agent_type}]\n"
        f"你是一个专注的子代理（sub-agent），你的唯一任务是完成主代理委托给你的工作。\n"
        f"任务描述：{user_text[:500]}\n\n"
        f"执行规则：\n"
        f"1. 专注完成委托的任务，不要闲聊或偏离主题\n"
        f"2. 必须调用工具实际执行，不能只用文字回复\n"
        f"3. 完成后用结构化格式总结你的发现/结果\n"
        f"4. 如果搜索到数据，必须标注来源\n"
        f"5. 如果要求生成文件，必须调用 export_file 实际生成\n"
        f"{agent_cfg['system_suffix']}"
    )
    return sub_prompt


def get_sub_agent_config(agent_type: str) -> dict:
    """获取子 agent 的运行配置。"""
    return SUB_AGENT_TYPES.get(agent_type, SUB_AGENT_TYPES["research"])


def _is_admin(sender_id: str = "", sender_name: str = "") -> bool:
    """判断当前用户是否是管理员（按租户配置）"""
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    if sender_id and sender_id in tenant.admin_open_ids:
        return True
    if sender_name and sender_name in tenant.admin_names:
        return True
    # 回退到全局 settings（兼容未配置租户 admin 的场景）
    if sender_id and sender_id in settings.admin_open_ids:
        return True
    if sender_name and sender_name in settings.admin_names:
        return True
    return False


async def _build_system_prompt(
    mode: str = "safe",
    sender_id: str = "",
    sender_name: str = "",
    user_text: str = "",
    chat_id: str = "",
    chat_type: str = "",
    task_type: str = "",
    actual_tool_names: set[str] | None = None,
) -> str:
    """合并用户配置的人设 + 工具说明 + 模式指令 + 已知用户 + 身份行为准则"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    try:
        from app.tools.calendar_ops import _get_user_tz
        tz = _get_user_tz()
        tz_label = str(tz)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
        tz_label = "Asia/Shanghai"
    now = datetime.now(tz)
    today_str = now.strftime('%Y-%m-%d')
    tomorrow = now + timedelta(days=1)
    tomorrow_str = tomorrow.strftime('%Y-%m-%d')
    time_ctx = (
        f"\n\n当前时间：{now.strftime('%Y年%m月%d日 %A %H:%M')}（{tz_label}）"
        f"\n今天={today_str}，明天={tomorrow_str}"
    )
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    base_prompt = tenant.llm_system_prompt or settings.kimi.chat_system_prompt
    prompt = base_prompt + time_ctx + _INSTRUCTIONS

    # 飞书平台：注入飞书专属工具指令（日历/任务/文档/消息等）
    from app.tenant.context import get_current_channel as _get_ch
    _ch = _get_ch()
    _current_plat = _ch.platform if _ch else tenant.platform
    if _current_plat == "feishu":
        prompt += _FEISHU_INSTRUCTIONS

    # 自我迭代指令仅对开启该能力的租户注入（从知识库文件动态加载）
    if tenant.self_iteration_enabled:
        prompt += _load_self_iteration_instructions()

    # 能力模块：静态配置 + 动态发现（M2: Manus 式角色切换）
    module_names = list(getattr(tenant, "capability_modules", None) or [])
    # M2: 根据用户消息自动发现相关模块（不重复已配置的）
    if user_text:
        discovered = discover_modules(user_text)
        for m in discovered:
            if m not in module_names:
                module_names.append(m)
                logger.info("auto-discovered capability module: %s", m)
    if module_names:
        prompt += _load_capability_modules(module_names)

    # 触发匹配的 skill instructions 注入
    if user_text and tenant.tenant_id:
        try:
            from app.tools.skill_engine import load_triggered_skills
            skill_instructions, _, _ = load_triggered_skills(tenant.tenant_id, user_text)
            if skill_instructions:
                prompt += skill_instructions
        except Exception:
            logger.warning("failed to load skill instructions", exc_info=True)

    # 根据用户身份注入不同行为准则（custom_persona 租户有独立人设，跳过通用准则）
    if not tenant.custom_persona:
        if _is_admin(sender_id, sender_name):
            prompt += _ADMIN_BEHAVIOR
        else:
            prompt += _NORMAL_BEHAVIOR

    if mode == "full_access":
        prompt += _FULL_ACCESS_ADDENDUM

    # 记忆工具使用指引（有记忆工具的 bot 才注入）
    _has_memory_tools = not tenant.tools_enabled or "save_memory" in tenant.tools_enabled
    if _has_memory_tools:
        prompt += _MEMORY_USAGE_HINT

    # M3: 深度研究模式指令（研究类任务注入端到端调研流程）
    # 优先使用调用方传入的 task_type（LLM 分类），fallback 到关键词
    _task_type = task_type or (classify_task_type(user_text) if user_text else "normal")
    if _task_type == "research":
        prompt += _DEEP_RESEARCH_INSTRUCTIONS

    # 注入能力画像（平台感知 + 实际工具能力）
    prompt += _build_capability_profile(tenant, actual_tool_names=actual_tool_names)

    if chat_id:
        if chat_type == "group":
            prompt += f"\n\n[当前场景] 你在群聊中（chat_id={chat_id}）。当前消息来自 {sender_name or sender_id}。如需查看本群历史消息，请用 fetch_chat_history(chat_id=\"{chat_id}\")。"
        else:
            prompt += f"\n\n[当前场景] 你在与 {sender_name or sender_id} 的私聊中（chat_id={chat_id}，用户open_id={sender_id}）。如需查看本私聊的历史消息，请用 fetch_chat_history(chat_id=\"{chat_id}\")。"

    # 注入已知用户列表，让模型认识团队成员
    users_info = user_registry.summary()
    if users_info:
        prompt += f"\n\n{users_info}"

    # 注入记忆上下文（用户画像 + 最近交互 + 活跃计划）
    # 可通过 tenant.memory_context_enabled 关闭（轻量 bot 不需要记忆注入）
    if getattr(tenant, "memory_context_enabled", True):
        try:
            memory_ctx = await bot_memory.build_memory_context(sender_id, sender_name, user_text)
            if memory_ctx:
                prompt += memory_ctx
            plans_ctx = bot_planner.get_active_plans_context(sender_id)
            if plans_ctx:
                prompt += f"\n\n{plans_ctx}"
        except Exception:
            logger.warning("memory context injection failed", exc_info=True)

    # 跨平台身份上下文：让 LLM 知道当前用户的跨 channel 身份
    try:
        from app.tenant.context import get_current_sender
        sender_ctx = get_current_sender()
        if sender_ctx.identity_id and sender_ctx.linked_platforms:
            platforms_str = ", ".join(
                f"{p}({uid[:12]}...)" for p, uid in sender_ctx.linked_platforms.items()
            )
            prompt += (
                f"\n\n[跨平台身份] 当前用户已关联统一身份（identity: {sender_ctx.identity_id[:8]}...）。"
                f"\n关联平台: {platforms_str}"
                f"\n当前消息来自: {sender_ctx.channel_platform}"
                f"\n该用户在所有关联平台的记忆和对话上下文是共享的。"
            )
        elif sender_ctx.channel_platform:
            prompt += (
                f"\n\n[身份提示] 当前用户在 {sender_ctx.channel_platform} 平台，尚未关联跨平台身份。"
                f"\n如果用户提到自己在其他平台也和你聊过，你可以用 search_known_user 搜索并发起验证。"
            )
    except Exception:
        logger.warning("identity context injection failed", exc_info=True)

    # 超管身份：注入待审批请求提醒 + 权限标识
    try:
        from app.tenant.context import get_current_sender
        sender_ctx = get_current_sender()
        if sender_ctx.is_super_admin:
            prompt += _build_admin_context()
        elif tenant.deploy_free_quota > 0:
            prompt += _build_deploy_quota_context(tenant, sender_ctx.sender_id)
    except Exception:
        logger.warning("admin context injection failed", exc_info=True)

    return prompt


def _build_capability_profile(tenant, actual_tool_names: set[str] | None = None) -> str:
    """根据租户配置动态生成能力画像，让 LLM 了解自己在哪个平台、能做什么、不能做什么。

    actual_tool_names: 经过权限/平台/白名单过滤后的实际工具集。
    如果传入则直接使用，避免能力画像与实际工具不一致（如 instance_management_enabled=False
    但画像仍声称能创建实例）。
    """
    from app.tenant.context import get_current_channel
    lines = []
    current_ch = get_current_channel()
    platform = current_ch.platform if current_ch else tenant.platform

    # ── 平台交互方式 ──
    if platform == "wecom_kf":
        lines.append("你在微信客服平台，用户通过微信和你聊天")
        lines.append("定时任务执行后可以主动发消息通知用户（用户最后发消息后 48 小时内有效，超过 48 小时发不出去）")
        lines.append("保存记忆时写「微信对话」而非「飞书对话」")
    elif platform == "wecom":
        lines.append("你在企业微信平台")
    elif platform == "feishu":
        lines.append("你在飞书平台，可以主动发消息、操作文档/日历/任务")
    elif platform == "qq":
        lines.append("你在 QQ 机器人平台，用户通过 QQ 和你聊天")
        lines.append("只能被动回复（群聊 5 分钟内最多 5 条，单聊 60 分钟窗口），不能主动推送")

    # ── 根据实际工具集描述能力 ──
    # 优先使用调用方传入的已过滤工具集，确保能力画像与 LLM 实际可用工具完全一致
    if actual_tool_names is not None:
        _tools = set(actual_tool_names)
    else:
        # fallback: 从 tenant config 推算（可能与实际不一致，仅兜底）
        _tools = set(tenant.tools_enabled) if tenant.tools_enabled else set(ALL_TOOL_MAP.keys())
        if platform != "feishu":
            _tools -= _FEISHU_ONLY_TOOLS

    if "xhs_search" in _tools or "search_social_media" in _tools:
        lines.append("你能搜索小红书/抖音等社媒平台的用户和内容")
    if "xhs_get_note" in _tools:
        lines.append(
            "当用户发来小红书链接（xiaohongshu.com/...）时，用 xhs_get_note 获取内容，"
            "不要用 web_search（会被反爬拦截）"
        )
    if "create_plan" in _tools:
        lines.append("你能把大任务拆成多步计划，用 schedule_step 安排定时自动执行")
    if "export_file" in _tools:
        lines.append("你能导出 PDF/CSV/TXT 报告文件")
    if "web_search" in _tools:
        lines.append("你能联网搜索最新信息")
    if "browser_open" in _tools:
        lines.append("你能打开网页浏览和操作（浏览器自动化）")
    if "save_memory" in _tools:
        lines.append("你有跨对话记忆，能记住用户偏好和历史")
    if "read_file" in _tools:
        lines.append("你能读写代码仓库文件、创建 PR")

    # 飞书特有能力
    if platform == "feishu":
        feishu_caps = []
        if "list_events" in _tools:
            feishu_caps.append("日历")
        if "create_feishu_doc" in _tools:
            feishu_caps.append("文档")
        if "create_feishu_task" in _tools:
            feishu_caps.append("任务")
        if "search_bitable_records" in _tools:
            feishu_caps.append("多维表格")
        if feishu_caps:
            lines.append(f"你能操作飞书{'/'.join(feishu_caps)}")

    # 实例管理 / co-host 能力（动态读取 provisioner 支持的平台列表）
    if "provision_tenant" in _tools:
        try:
            from app.services.provisioner import SUPPORTED_PLATFORMS
            _plat_list = "、".join(SUPPORTED_PLATFORMS)
        except ImportError:
            _plat_list = "飞书、企微、微信客服、QQ"
        lines.append(
            f"你能为客户开通新 bot 实例，支持的平台: {_plat_list}。"
            "同一个企微自建应用下的多个客服账号可以 co-host 到同一个容器"
            "（凭证复用，按 open_kfid 分发到不同人设）。"
            "QQ 机器人必须独立实例（无 co-host）"
        )
    if "list_kf_accounts" in _tools and platform == "wecom_kf":
        lines.append("你能查看和管理本企业的所有客服账号（list_kf_accounts）")

    # 社媒 API 深度
    if tenant.social_media_api_provider:
        lines.append(f"社媒数据 API 已配置（{tenant.social_media_api_provider}），可获取精确粉丝数/互动数据")
    elif "search_social_media" in _tools:
        lines.append("社媒数据通过搜索引擎间接获取（无直连 API），数据精度有限")

    if not lines:
        return ""
    result = "\n\n[你的能力] " + "。".join(lines) + "。"
    result += "\n⚠️ 以上是你的全部能力，不要声称拥有上面没列出的能力。如果用户问你能否做某件事而你没有对应的工具，请如实说不能。"
    return result


def _build_deploy_quota_context(tenant, sender_id: str) -> str:
    """构建用户部署配额上下文（引导 Steven AI 判断是否可以开通）"""
    try:
        from app.services.deploy_quota import check_deploy_quota
        quota = check_deploy_quota(tenant.tenant_id, sender_id, tenant.deploy_free_quota)
    except Exception:
        return ""

    ctx = "\n\n[部署配额]"
    if quota["allowed"]:
        remaining = quota["remaining"]
        ctx += (
            f" 该用户还有 {remaining} 次免费部署机会。"
            "\n当用户表达了想要部署/开通 bot 的意向时（无论是直接说还是聊到相关需求），"
            "按 onboarding_workflow 模块的流程推进：理解需求→设计方案→收集凭证→执行开通→验证上线。"
            "\n如果客户发来了业务资料（PDF/文档/网站），先仔细阅读理解，整理需求确认单给客户确认后再行动。"
            "\n如果流程涉及多步骤，用 create_plan 创建执行计划跟踪进度。"
            "\n需要客户做平台操作时，用 guide_human 生成分步指引（参考 platform_setup_guide 模块的精确路径）。"
            "\n信息收集完整后，使用 request_provision 提交开通申请。"
            "\n注意：配额仅在部署成功后才消耗，申请被拒绝不扣额度。"
        )
    else:
        ctx += (
            f" 该用户免费部署额度已用完（{quota['used']}/{quota['total']}）。"
            "\n如果用户再次表达部署意向，请友好地告知免费额度已用完，"
            "并引导他们了解付费方案或联系管理员获取更多额度。"
            "\n不要使用 request_provision 工具。"
        )
    return ctx


def _build_admin_context() -> str:
    """构建超管专属上下文（待审批请求 + 权限说明）"""
    ctx = "\n\n[超级管理员] 当前用户是超级管理员，拥有最高权限。"
    ctx += "\n你可以直接使用 provision_tenant 等管理工具。"
    ctx += "\n非管理员用户只能通过 request_provision 提交申请，你审批后才会开通。"

    try:
        from app.services.provision_approval import list_pending
        pending = list_pending()
        if pending:
            ctx += f"\n\n⚠️ 有 {len(pending)} 个待审批的开通请求："
            for i, req in enumerate(pending[:5], 1):
                ctx += (
                    f"\n  {i}. [{req['request_id']}] "
                    f"{req.get('requester_name', '未知')} "
                    f"请求开通「{req.get('name', '')}」"
                    f"({req.get('platform', '')}) — {req.get('created_at', '')[:16]}"
                )
            if len(pending) > 5:
                ctx += f"\n  ...还有 {len(pending) - 5} 个"
            ctx += "\n→ 用 approve_provision_request 或 reject_provision_request 处理。"
    except Exception:
        pass

    return ctx


# ── Agent 循环辅助 ──

# 用不同参数调用同一工具属于正常调研行为的工具（搜索/查询/读取类）
# 这些工具的"同名不同参数"阈值更宽松，避免误判合法研究为空转
_RESEARCH_TOOLS = frozenset({
    "search_logs", "self_search_code", "web_search", "fetch_chat_history",
    "read_feishu_doc", "search_feishu_docs", "list_bot_groups",
    "get_deploy_logs", "search_bitable_records",
    # 浏览器自动化：逐个打开不同页面收集数据是正常研究行为
    "browser_open", "browser_do", "browser_read",
    # 社媒搜索
    "search_social_media",
    # 小红书专用工具（搜索+浏览是正常调研行为）
    "xhs_search", "xhs_get_note", "xhs_get_user",
    # 文档写入：长报告需要多次 update/edit 是正常行为
    "update_feishu_doc", "edit_feishu_doc", "write_feishu_doc",
})

# 读后未写检测（read-without-write）
_READ_WRITE_PAIRS: dict[str, frozenset[str]] = {
    # 飞书文档/多维表格：读了通常是为了改，nudge 合理
    "read_feishu_doc": frozenset({
        "write_feishu_doc", "update_feishu_doc", "edit_feishu_doc",
    }),
    "list_bitable_records": frozenset({
        "create_bitable_record", "update_bitable_record", "batch_update_bitable_records",
    }),
    "search_bitable_records": frozenset({
        "create_bitable_record", "update_bitable_record", "batch_update_bitable_records",
    }),
    # ⚠️ read_file / self_read_file 已移除：代码阅读是最常见的操作，
    # 用户经常只是让 bot 查看/解释代码，不一定要改。
    # 之前的 nudge 导致 bot 在用户只是问"这个代码对不对"时自作主张改代码。
}


def _has_unmatched_reads(tool_names: list[str], user_text: str = "") -> bool:
    """检测是否存在「读了但没写」的未完成操作。

    对飞书文档/多维表格始终检测（读了通常是为了改）。
    对代码文件（read_file）只在用户消息明确包含修改意图时检测，
    避免用户只是问"这个代码对不对"时被误判。
    """
    called = set(tool_names)
    # 1) 飞书文档/多维表格：始终检测
    for read_tool, write_tools in _READ_WRITE_PAIRS.items():
        if read_tool in called and not (called & write_tools):
            return True
    # 2) 代码文件：仅在用户消息有修改意图时检测
    if user_text and _CODE_MODIFY_INTENT.search(user_text):
        _code_write_tools = {"write_file", "self_write_file", "self_edit_file",
                             "create_pull_request"}
        _code_read_tools = {"read_file", "self_read_file"}
        if (called & _code_read_tools) and not (called & _code_write_tools):
            logger.info("code read-without-write detected (user intent: modify code)")
            return True
    return False


# ── 交付物检测：用户要求了文件但模型没生成 ──

import re as _re

# 用户消息中的代码修改意图关键词
_CODE_MODIFY_INTENT = _re.compile(
    r"改|修改|修复|修一下|fix|修bug|加[个一]|添加|增加|删[掉除去了]|去掉|移除"
    r"|开关|toggle|重构|refactor|优化|换成|替换|改成|更新|update"
    r"|写[个一]|实现|implement|加入|接入|迁移|migrate"
)

# 关键词 → (满足条件的工具集合, 显示名)
# 只要 tool_names_called 里出现任一工具就算满足
_DELIVERABLE_PATTERNS: list[tuple[_re.Pattern, frozenset[str], str]] = [
    (_re.compile(r"(?i)\bpdf\b"), frozenset({"export_file"}), "PDF"),
    (_re.compile(r"(?i)\bppt\b|幻灯片|slides"), frozenset({"export_file", "create_html_slides"}), "PPT/幻灯片"),
    (_re.compile(r"报告"), frozenset({"export_file", "create_document", "create_feishu_doc"}), "报告文件"),
    (_re.compile(r"(?i)\bcsv\b"), frozenset({"export_file"}), "CSV"),
    (_re.compile(r"(?i)\bexcel\b|xlsx"), frozenset({"export_file"}), "Excel"),
]


def check_unfulfilled_deliverables(user_text: str, tool_names: list[str]) -> list[str]:
    """检查用户要求的交付物是否已由对应工具生成。

    返回未生成的交付物名称列表（空 = 全部满足或用户没要求文件）。
    """
    called = set(tool_names)
    missing: list[str] = []
    for pattern, required_tools, display_name in _DELIVERABLE_PATTERNS:
        if pattern.search(user_text) and not (called & required_tools):
            missing.append(display_name)
    return missing


# ── Local Action-Claim Detector (fast, deterministic) ──
# 不依赖 LLM 调用，用正则检测 reply 中是否有"已经做了X"但实际没调工具的空承诺。
# 比 LLM exit gate 快 1000x，没有超时 fail-open 风险。

# 模式：(正则匹配回复文本, 对应需要的工具名集合)
# 如果回复匹配了模式但没调过对应工具，判定为空承诺
_ACTION_CLAIM_PATTERNS: list[tuple[_re.Pattern, frozenset[str]]] = [
    # "已经删了/删除了/删掉了/清理了" → 需要 delete 类工具
    (_re.compile(r"(已经|已|都|全部).{0,15}(删除|删掉|删了|移除|清理|清掉|清除)"),
     frozenset({"delete_calendar_event", "delete_bitable_record", "delete_feishu_doc"})),
    # "已经创建了/新建了/添加了/加好了/加完了" → 需要 create 类工具
    (_re.compile(r"(已经|已|都|全部).{0,15}(创建|新建|添加|加好|加了|加完|建好)"),
     frozenset({"create_calendar_event", "create_document", "create_feishu_doc",
                "add_bitable_record", "add_bitable_records"})),
    # "已经发送了/发了/寄了" → 需要 send 类工具
    (_re.compile(r"(已经|已).{0,15}(发送|发了|发出|寄了|寄出)"),
     frozenset({"send_mail", "send_feishu_message"})),
    # "已经修改了/更新了/编辑了" → 需要 update/edit 类工具
    (_re.compile(r"(已经|已|都|全部).{0,15}(修改|更新|编辑|改好|改了)"),
     frozenset({"update_calendar_event", "edit_feishu_doc", "update_bitable_record",
                "write_feishu_doc", "update_feishu_doc"})),
    # "搞定了/改完了/做完了/写好了/弄好了" → 代码修改类完成声称，需要 write 类工具
    (_re.compile(r"(搞定|改完|做完|写完|弄完|写好|弄好|改好|做好|搞好)了"),
     frozenset({"write_file", "self_write_file", "self_edit_file",
                "create_pull_request", "edit_feishu_doc", "write_feishu_doc",
                "update_feishu_doc", "export_file",
                "update_calendar_event", "create_calendar_event",
                "add_bitable_record", "update_bitable_record",
                "create_document", "create_feishu_doc",
                "send_mail", "send_feishu_message"})),
    # "我去做/马上/这就/立刻/先去" → 承诺要做（还没做）
    (_re.compile(r"(我[去就来]|马上|这就|立刻|先去|我现在|接下来我).{0,10}(做|处理|执行|操作|开始|修复|修改|删|加|创建|发送)"),
     frozenset()),  # 空集 = 任何工具都不满足 = 一定是空承诺
]

# 例外：这些短语看起来像承诺但实际是在描述过去的动作或结果
_CLAIM_FALSE_POSITIVE = _re.compile(
    r"(之前|上次|刚才).{0,5}(已经|已)"  # "之前已经删了" = 描述历史，不是当前承诺
    r"|可以(修改|编辑|删除|查看|阅读)"   # "任何人都可以修改" = 权限描述，不是操作声称
    r"|权限.{0,8}(修改|编辑)"            # "权限设置成了...修改" = 权限描述
)


def detect_action_claims(reply_text: str, tool_names_called: list[str]) -> bool:
    """快速检测回复中是否有未兑现的动作声称。

    返回 True = 发现空承诺，应该 nudge 模型继续执行。
    返回 False = 回复安全，可以退出。

    比 LLM exit gate 快 1000x，无超时风险。
    """
    if not reply_text or len(reply_text) < 4:
        return False

    # 排除 false positive
    if _CLAIM_FALSE_POSITIVE.search(reply_text):
        return False

    total_calls = len(tool_names_called)
    called = set(tool_names_called)

    # 如果模型已经在积极工作（≥3 次工具调用），跳过所有检测。
    # 模型在做了实际工作后的文本回复（中间汇报/结果报告/完成总结）
    # 中提到"修改"/"删除"/"创建"等词很正常，不是空承诺。
    # 阈值从 5 降到 3：3 次工具调用足以说明模型在干活。
    # 之前阈值 5 导致调了 3-4 个工具的 bot 被误判为空承诺并 nudge，
    # nudge 消息反而让 bot 迷失方向（如日历任务中跑去改源码）。
    if total_calls >= 3:
        logger.info("action claim check skipped: %d total tool calls (likely working/reporting)", total_calls)
        return False

    for pattern, required_tools in _ACTION_CLAIM_PATTERNS:
        if pattern.search(reply_text):
            if not required_tools:
                # 空集 = "我去做/马上处理" 类承诺
                logger.info("action claim detected (promise pattern): %s", reply_text[:80])
                return True
            if not (called & required_tools):
                # 声称做了 X，但没调过对应工具
                logger.info(
                    "action claim detected: reply claims action but tools %s not in %s",
                    required_tools, called,
                )
                return True
    return False


# ── Grounding Gate: 检测未经验证的事实性声称 ──
# 根治 LLM "不搜就编" 的问题。不是逐类别加 prompt（打地鼠），
# 而是在代码层检测"回复了事实但没搜过" → 强制打回重搜。

# 验证类工具 —— 调过任何一个就算"有搜索行为"
_GROUNDING_TOOLS = frozenset({
    "web_search", "fetch_url", "browser_open", "browser_read",
    "search_social_media", "xhs_search", "xhs_playwright_search",
    "recall_memory",
})

# 用户明确要求搜索/调研的意图关键词
_RESEARCH_INTENT = _re.compile(
    r"(搜[一搜索]|查[一查找询]|research|调研|调查|了解一下|看看|帮我[找查搜看]|"
    r"谁是|有哪些|现在[是有]|最新的|目前|现状|什么情况|怎么样了|"
    r"哪些人|成员|董事|高管|管理层|创始人|CEO|CTO|CFO)",
    _re.IGNORECASE,
)

# 事实密度信号 —— 回复中含有这些模式说明在陈述具体事实
_FACTUAL_CLAIM_SIGNALS = _re.compile(
    # 具体职位+人名模式（如 "执行董事: 闫俊杰"、"CEO 张三"）
    r"(?:执行董事|监事|总经理|董事长|CEO|CTO|CFO|COO|创始人|联合创始人|总裁|副总裁)"
    r".{0,5}[\uff1a:].{0,5}[\u4e00-\u9fff]{2,4}"
    # 或者 "根据公开信息/工商信息/官方资料"（LLM 编造出处的常见模式）
    r"|根据(?:公开|工商|官方|最新|公开的).{0,8}(?:信息|资料|数据|披露|显示|记录)"
    # 或者列举多个中文人名（3个以上 = 高度可能是编造的名单）
    r"|[\u4e00-\u9fff]{2,4}[、,，][\u4e00-\u9fff]{2,4}[、,，][\u4e00-\u9fff]{2,4}",
)


def detect_ungrounded_claims(
    reply_text: str,
    user_text: str,
    tool_names_called: list[str],
) -> str | None:
    """检测回复中是否有未经工具验证的事实性声称。

    返回 nudge 消息（应注入 agent loop 催促搜索）或 None（安全）。
    """
    if not reply_text or not user_text:
        return None

    called = set(tool_names_called)

    # 如果已经调过验证类工具，放行（不管回复内容如何）
    if called & _GROUNDING_TOOLS:
        return None

    # ── 层 1: 用户明确要求搜索/调研，但 bot 没搜就答了 ──
    if _RESEARCH_INTENT.search(user_text):
        # 回复够长（>50字）= 在实质性回答，不是在确认需求
        if len(reply_text) > 50:
            logger.info(
                "grounding gate: user asked to research but no search tool called. "
                "user=%s reply=%s",
                user_text[:60], reply_text[:60],
            )
            return (
                "⚠️ 你没有使用任何搜索工具就给出了回答。用户明确要求搜索/调研，"
                "请先用 web_search 搜索最新信息，然后基于搜索结果回答。"
                "不要依赖你的记忆，你的知识可能过时或错误。"
            )

    # ── 层 2: 回复含高密度事实声称（人名列表、职位信息、编造出处）──
    if _FACTUAL_CLAIM_SIGNALS.search(reply_text):
        logger.info(
            "grounding gate: factual claims detected without verification. "
            "reply=%s",
            reply_text[:80],
        )
        return (
            "⚠️ 你的回复包含具体的人名、职位等事实信息，但你没有通过搜索工具验证。"
            "这些信息可能是过时或错误的。请用 web_search 搜索验证后再回答。"
        )

    return None


# ── LLM Exit Gate (backup, with fail-closed) ──
# 本地检测器处理不了的复杂情况，用小模型兜底。
# 关键改动：超时时 fail-CLOSED（nudge），不再 fail-open。

_EXIT_REVIEW_PROMPT = """\
你是一个独立的 AI agent 退出审查器（Evaluator），负责评估 agent 的回复质量。
agent 正在帮用户完成任务，现在 agent 给出了一段回复但**没有调用任何工具**（或工具调用很少）。

请从三个维度对 agent 的回复打分（1-10 分）：

**意图匹配度（权重 40%）：**
用户的消息和 agent 的回复是否对应？
- 10: 精确回答了用户的问题
- 7-9: 基本对应，但有些偏题
- 4-6: 部分相关，回答了用户没问的东西
- 1-3: 完全跑偏，回答和用户的问题无关

**事实可靠性（权重 30%）：**
回复中的事实是否有工具返回数据支撑？
- 10: 纯对话/确认需求/基于工具结果，无需验证
- 7-9: 有少量推断但合理
- 4-6: 包含未验证的具体事实（人名、数据、URL）
- 1-3: 大量编造内容

**行为合理性（权重 30%）：**
agent 的行为是否合理？
- 10: 任务已完成，正常汇报/对话
- 7-9: 正在确认需求或寒暄
- 4-6: 声称要做什么但没调工具
- 1-3: 自作主张做了用户没要求的事

用户消息: {user_text}
agent 本轮已调用的工具: {tools_used}
agent 的回复: {reply_text}

请用以下 JSON 格式回复（不要输出任何其他内容）：
{{"relevance": N, "factual": N, "behavioral": N}}"""


async def llm_exit_review(
    reply_text: str,
    user_text: str,
    tool_names_called: list[str],
    *,
    gemini_client=None,
) -> str:
    """用独立 Evaluator 对 agent 退出进行多维评分。

    返回: "pass"（放行退出）或 "nudge"（催促执行）或 "grounding"（催促搜索验证）。
    超时/异常时默认 pass（fail-open）。

    评分维度（借鉴 Anthropic Harness 架构 + harness/evaluate.md 量化评估）：
    - 意图匹配度 40%：回复是否回答了用户实际问的问题
    - 事实可靠性 30%：事实是否有工具数据支撑
    - 行为合理性 30%：执行的行为是否被用户要求
    加权分 < 6.0 → nudge
    """
    if gemini_client is None:
        return "pass"

    # 太短的回复不值得审查（如 "好的"、"嗯"）
    if not reply_text or len(reply_text) < 8:
        return "pass"

    try:
        from google.genai import types as _t

        prompt = _EXIT_REVIEW_PROMPT.format(
            user_text=(user_text or "")[:200],
            tools_used=", ".join(tool_names_called[-10:]) if tool_names_called else "无",
            reply_text=reply_text[:300],
        )

        resp = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt + "\n\nReply ONLY with valid JSON. Example: {\"relevance\": 8, \"factual\": 8, \"behavioral\": 8}",
                config=_t.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=80,
                    thinking_config=_t.ThinkingConfig(include_thoughts=False),
                    response_mime_type="application/json",
                    response_schema={
                        "type": "object",
                        "properties": {
                            "relevance": {"type": "integer"},
                            "factual": {"type": "integer"},
                            "behavioral": {"type": "integer"}
                        },
                        "required": ["relevance", "factual", "behavioral"]
                    },
                ),
            ),
            timeout=5.0,
        )

        raw = (resp.text or "").strip()
        # 解析 JSON 分数
        try:
            scores = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # JSON 解析失败，尝试 regex 提取
            m = _re.search(r'\{[^}]+\}', raw)
            if m:
                try:
                    scores = json.loads(m.group(0))
                except (json.JSONDecodeError, ValueError):
                    logger.info("exit review: cannot parse scores from %r, pass", raw[:60])
                    return "pass"
            else:
                logger.info("exit review: cannot parse scores from %r, pass", raw[:60])
                return "pass"

        relevance = scores.get("relevance", 8)
        factual = scores.get("factual", 8)
        behavioral = scores.get("behavioral", 8)
        weighted = relevance * 0.4 + factual * 0.3 + behavioral * 0.3

        logger.info(
            "exit review: relevance=%d factual=%d behavioral=%d weighted=%.1f (reply: %s)",
            relevance, factual, behavioral, weighted, reply_text[:60],
        )

        if weighted < 6.0:
            # 低分 → 分析具体原因给出精准 nudge
            if factual <= 4:
                logger.info("exit review → grounding nudge (factual=%.0f)", factual)
                return "grounding"
            if behavioral <= 4:
                logger.info("exit review → nudge (behavioral=%.0f)", behavioral)
                return "nudge"
            if relevance <= 4:
                logger.info("exit review → nudge (relevance=%.0f)", relevance)
                return "nudge"
            # 整体偏低
            logger.info("exit review → nudge (weighted=%.1f)", weighted)
            return "nudge"

        logger.debug("exit review → pass (weighted=%.1f)", weighted)
        return "pass"

    except Exception:
        # fail-OPEN: 超时/异常时放行。本地 detect_action_claims 已做了第一道检查，
        # LLM gate 是兜底防线，超时不应阻止模型正常退出。
        logger.info("exit review failed/timeout, defaulting to PASS (fail-open)")
        return "pass"


def _drain_inbox(inbox: asyncio.Queue) -> list[dict]:
    """非阻塞地取出信箱中所有待处理消息。每条为 {"text": str, "images": list|None}。"""
    items: list[dict] = []
    while True:
        try:
            item = inbox.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _build_progress_hint(tool_names: list[str], progress_count: int = 0) -> str:
    """根据最近调用的工具生成自然、有人味的进度提示。

    progress_count: 已发送的进度消息数量，用于区分首次和后续消息。
    首次消息更口语化（"我去查查"），后续消息更简短（"快好了"）。
    """
    names = set(tool_names[-5:])

    # ── 按工具类型分组的候选消息 ──
    # 每组分 first（首次进度）和 follow（后续进度）
    _hints: dict[str, dict[str, list[str]]] = {
        "search": {
            "first": [
                "我去搜一下相关信息",
                "我先查查看",
                "让我搜一下",
                "我找找相关资料",
            ],
            "follow": [
                "还在搜，找到一些了",
                "搜到不少东西，我再整理下",
                "内容挺多的，我筛选一下",
            ],
        },
        "minute": {
            "first": [
                "我去看下妙记",
                "让我翻翻妙记内容",
            ],
            "follow": [
                "妙记内容有点长，我看看重点",
            ],
        },
        "chat_history": {
            "first": [
                "我去翻翻聊天记录",
                "让我看下之前的对话",
            ],
            "follow": [
                "记录挺多的，我找关键的",
            ],
        },
        "calendar": {
            "first": [
                "我看下日历",
                "让我查下日程安排",
            ],
            "follow": [
                "日程有点多，我整理下",
            ],
        },
        "doc": {
            "first": [
                "我去看下文档",
                "让我翻翻相关文档",
            ],
            "follow": [
                "文档内容挺多，我找重点",
            ],
        },
        "task": {
            "first": [
                "我处理一下",
                "好，我来弄",
                "我操作一下",
            ],
            "follow": [
                "还在处理，快了",
                "差不多了，再等等",
            ],
        },
        "browser": {
            "first": [
                "我去网上看看",
                "我打开网页查一下",
                "让我上网看看",
            ],
            "follow": [
                "在看网页内容，有点多",
                "翻了几个页面了，快好了",
            ],
        },
        "social_media": {
            "first": [
                "我去各平台搜一下",
                "让我查查社媒数据",
                "我看看各平台的情况",
            ],
            "follow": [
                "找到一些了，我再多看几个",
                "数据在汇总中",
            ],
        },
        "xhs_publish": {
            "first": [
                "在准备发帖，填写标题和内容中",
                "正在小红书创作者平台操作",
            ],
            "follow": [
                "帖子内容已填好，在上传图片",
                "发帖操作还在进行中",
            ],
        },
        "xhs_login": {
            "first": [
                "在打开小红书登录页，马上发二维码给你",
                "准备登录小红书",
            ],
            "follow": [
                "在等你扫码，扫完自动继续",
            ],
        },
        "code": {
            "first": [
                "我看下代码",
                "让我查查相关代码",
            ],
            "follow": [
                "代码看了一些了，在分析",
            ],
        },
        "memory": {
            "first": [
                "我回忆一下之前的信息",
                "让我想想之前聊过什么",
            ],
            "follow": [
                "想起来一些了，我整理下",
            ],
        },
        "file_export": {
            "first": [
                "我开始生成文件了",
                "文件在生成中",
            ],
            "follow": [
                "文件快好了",
            ],
        },
        "default": {
            "first": [
                "我处理一下，稍等",
                "好嘞，我看看",
                "收到，我来弄",
                "让我处理下",
            ],
            "follow": [
                "还在弄，快了",
                "差不多了，再等一下",
                "快好了",
                "马上就好",
            ],
        },
    }

    # ── 匹配工具类型 ──
    category = "default"
    if names & {"web_search"}:
        category = "search"
    elif names & {"get_feishu_minute", "get_feishu_minute_transcript"}:
        category = "minute"
    elif names & {"fetch_chat_history"}:
        category = "chat_history"
    elif any("calendar" in n for n in names):
        category = "calendar"
    elif any("doc" in n or "wiki" in n for n in names):
        category = "doc"
    elif names & {"browser_open", "browser_do", "browser_read"}:
        category = "browser"
    elif names & {"search_social_media", "get_platform_search_url",
                    "xhs_search", "xhs_get_note", "xhs_get_user"}:
        category = "social_media"
    elif names & {"xhs_publish", "xhs_confirm_publish"}:
        category = "xhs_publish"
    elif names & {"xhs_login", "xhs_check_login"}:
        category = "xhs_login"
    elif names & {"search_code", "read_file", "self_search_code", "self_read_file"}:
        category = "code"
    elif names & {"save_memory", "recall_memory"}:
        category = "memory"
    elif names & {"export_file"}:
        category = "file_export"
    elif any("task" in n for n in names):
        category = "task"

    phase = "first" if progress_count == 0 else "follow"
    candidates = _hints[category][phase]
    return random.choice(candidates)


def _tool_activity_desc(tool_names: list[str]) -> str:
    """根据工具调用生成简短的动作描述（给 LLM 做 context）"""
    names = set(tool_names[-5:])
    if names & {"web_search"}:
        return "搜索网页信息"
    if names & {"browser_open", "browser_do", "browser_read"}:
        return "浏览网页"
    if names & {"search_social_media", "get_platform_search_url",
                  "xhs_search", "xhs_get_note", "xhs_get_user"}:
        return "搜索社媒平台"
    if names & {"xhs_publish", "xhs_confirm_publish"}:
        return "在小红书发帖"
    if names & {"xhs_login", "xhs_check_login"}:
        return "登录小红书"
    if names & {"get_feishu_minute", "get_feishu_minute_transcript"}:
        return "查看飞书妙记"
    if names & {"fetch_chat_history"}:
        return "翻看聊天记录"
    if any("calendar" in n for n in names):
        return "查看日历"
    if any("doc" in n or "wiki" in n for n in names):
        return "查看文档"
    if names & {"search_code", "read_file", "self_search_code", "self_read_file"}:
        return "看代码"
    if names & {"save_memory", "recall_memory"}:
        return "回忆之前的对话"
    if names & {"export_file"}:
        return "生成文件"
    return "处理任务"


_PROGRESS_PROMPT = """\
{persona_hint}你当前在{activity}，需要给用户发一条简短的进度消息。

用户的请求是：{user_task}

要求：
- 一句话，15-25字，口语化，像朋友发微信一样自然
- 根据用户的请求说清楚你正在干什么，内容要和用户任务匹配
- {phase_hint}
- 符合你的人设和说话风格
- 不要用"正在""请稍候"这类客服话术
- 不要用标点符号结尾（句号/省略号/感叹号都不要）
- 直接输出这句话，不要任何解释"""


def _extract_persona_hint() -> str:
    """从当前租户的 system prompt 中提取简短人设描述（约 100 字以内）。

    策略：取 system prompt 前 150 字符（通常包含角色名+身份+说话风格），
    截断到最后一个完整句子。如果太短或没有，返回空字符串。
    """
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        sp = tenant.llm_system_prompt or ""
        if not sp:
            return ""
        # 取前 200 字符，截断到最后一个句号/换行
        snippet = sp[:200]
        # 找最后一个自然断句点
        for sep in ("\n", "。", "；", ".", "回答风格", "语言风格"):
            idx = snippet.rfind(sep)
            if idx > 30:  # 至少保留 30 字
                snippet = snippet[:idx + len(sep)]
                break
        return f"你的人设：{snippet.strip()}\n\n"
    except Exception:
        return ""


async def _generate_progress_hint(
    tool_names: list[str],
    progress_count: int = 0,
    *,
    gemini_client=None,
    user_text: str = "",
) -> str | None:
    """用小模型快速生成人味进度消息，失败时返回 None（不发消息）。

    gemini_client: google.genai.Client，由 gemini_provider 传入。
    user_text: 用户原始消息，让进度消息和任务内容匹配。
    注入当前租户的人设摘要，让不同 bot 有不同说话风格。
    超时 5 秒，确保不阻塞 agent loop。
    不再使用硬编码 fallback — 如果 LLM 生成失败，宁可不发也不发不应景的模板消息。
    """
    if gemini_client is None:
        return None

    try:
        from google.genai import types as _t

        activity = _tool_activity_desc(tool_names)
        phase_hint = "这是第一条进度消息，告诉用户你开始做了" if progress_count == 0 \
            else "之前已经发过进度了，这次告诉用户快好了或进展如何"
        persona_hint = _extract_persona_hint()
        # 截取用户消息前 80 字符作为任务上下文
        user_task = (user_text or "")[:80].strip() or "（未知任务）"

        prompt = _PROGRESS_PROMPT.format(
            activity=activity, phase_hint=phase_hint, persona_hint=persona_hint,
            user_task=user_task,
        )

        # 用 flash 模型，max_tokens 极小，超时 5 秒
        resp = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config=_t.GenerateContentConfig(
                    temperature=0.9,
                    max_output_tokens=100,
                    thinking_config=_t.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout=5.0,
        )

        text = (resp.text or "").strip().rstrip("。…！!.~，,")
        # 安全检查：太短（< 6 字符）或太长都不发
        # 之前用 < 2 导致 "我正"/"小红" 这种 2 字垃圾被发出去
        if not text or len(text) < 6:
            logger.info("progress hint: too short (len=%d, text='%s')", len(text) if text else 0, text or "")
            return None
        if len(text) > 80:
            # 截断到第一句话
            for sep in ("，", "、", ",", " "):
                idx = text.find(sep)
                if 4 <= idx <= 50:
                    text = text[:idx]
                    break
            else:
                text = text[:50]
        return text

    except Exception as e:
        logger.info("progress hint LLM failed: %s", e)
        return None


# ── 输出处理 ──

def _strip_degenerate_repetition(text: str, min_seg: int = 15, max_reps: int = 3) -> str:
    """检测并截断退化重复输出。

    当 LLM 进入退化循环时，输出会包含同一段文本反复重复几十上百次。
    此函数检测最短重复单元（>= min_seg 字符），如果连续出现 >= max_reps 次，
    则截断为首次出现 + 提示。
    """
    n = len(text)
    if n < min_seg * max_reps:
        return text

    for seg_len in range(min_seg, min(300, n // max_reps) + 1):
        i = 0
        while i <= n - seg_len * max_reps:
            segment = text[i : i + seg_len]
            reps = 1
            j = i + seg_len
            while j + seg_len <= n and text[j : j + seg_len] == segment:
                reps += 1
                j += seg_len
            if reps >= max_reps:
                kept = text[: i + seg_len].rstrip()
                logger.warning(
                    "degenerate repetition detected: %d-char segment repeated %d times, truncated",
                    seg_len, reps,
                )
                return kept + "\n\n（检测到输出异常重复，已自动截断）"
            i += 1

    return text


# LLM 幻觉标签（Gemini 有时会幻觉 Jupyter/IPython 环境，输出代码块给用户）
_HALLUCINATION_TAGS = re.compile(
    r"<(?:execute_ipython|execute_python|ipython-exec|code_execution|python_exec)>"
    r".*?"
    r"</(?:execute_ipython|execute_python|ipython-exec|code_execution|python_exec)>",
    re.DOTALL,
)

# LLM 模仿内部标签（从 chat history 中学到 <tools_used> 模式并输出给用户）
# 匹配两种情况：
#   1. 完整闭合：<tools_used>...</tools_used>
#   2. 未闭合（被截断/LLM 没写闭合标签）：<tools_used>... 到文末
_INTERNAL_TAGS = re.compile(
    r"<tools_used>.*?(?:</tools_used>|$)\s*",
    re.DOTALL,
)


def _strip_hallucinated_code_blocks(text: str) -> str:
    """清除 LLM 幻觉的代码执行块。

    Gemini 有时会幻觉自己在 Jupyter 环境里，输出 <execute_ipython>...</execute_ipython>
    这类标签包裹的代码给用户看。这些不是真正的工具调用，需要剥离。
    """
    cleaned = _HALLUCINATION_TAGS.sub("", text)
    cleaned = _INTERNAL_TAGS.sub("", cleaned)
    if len(cleaned) < len(text):
        logger.warning("stripped hallucinated/internal tags from reply (%d chars removed)",
                       len(text) - len(cleaned))
        # 清理多余空行
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _trigger_memory(
    sender_id: str, sender_name: str, user_text: str, reply: str,
    tool_names_called: list[str],
    call_log: list[str] | None = None,
    action_outcomes: list[tuple[str, str]] | None = None,
) -> None:
    """异步触发日记写入 + 写入工具摘要 + 刷新工具序列。"""
    if tool_names_called:
        _set_tool_summary(tool_names_called, call_log, action_outcomes)

    # 刷新工具调用序列（组合模式发现）
    try:
        from app.services.tool_tracker import flush_session_sequence
        from app.tenant.context import get_current_tenant
        flush_session_sequence(get_current_tenant().tenant_id)
    except Exception:
        pass

    # 检查租户是否启用日记写入
    diary_enabled = True
    try:
        from app.tenant.context import get_current_tenant
        diary_enabled = getattr(get_current_tenant(), "memory_diary_enabled", True)
    except Exception:
        pass

    if not diary_enabled:
        return

    try:
        asyncio.create_task(
            bot_memory.write_diary(
                sender_id, sender_name, user_text, reply, tool_names_called,
                action_outcomes=action_outcomes,
            )
        )
    except Exception:
        logger.debug("diary write task failed", exc_info=True)


def _extract_outcome(func_name: str, result_str: str, func_args: dict | None = None) -> str:
    """从工具结果中提取关键事实（URL、标题、成功/失败），供历史上下文使用。

    目标：让下一轮对话的模型知道"上轮具体做了什么"，而不只是"调了哪些工具"。
    返回简短的一行摘要，如 "→ 成功，创建了文档 https://xxx.feishu.cn/docx/abc"。
    """
    if not result_str:
        return "→ 完成"

    s = result_str[:2000]  # 只看前 2000 字符
    is_error = "[ERROR]" in s or s.startswith("Error") or s.startswith("❌")

    if is_error:
        # 提取第一行错误信息
        first_line = s.split("\n", 1)[0][:120]
        return f"→ 失败: {first_line}"

    # fetch_url 特殊处理：记录**来源 URL**（调用参数），不是返回内容中的 URL
    # 这样日记系统能保存"从哪个 URL 读的数据"，跨对话可回忆
    if func_name == "fetch_url" and func_args and func_args.get("url"):
        source_url = func_args["url"][:200]
        content_len = len(result_str)
        return f"→ 从 {source_url} 读取了 {content_len} 字符数据"

    # 提取 URL（飞书文档/表格/文件链接等）
    import re
    urls = re.findall(r'https?://[^\s"\'<>\]））]+', s)
    # 提取标题/文件名
    title_match = re.search(r'[「《"\'](.*?)[」》"\'"]', s[:500])
    title = title_match.group(1)[:60] if title_match else ""

    # 按工具类型定制摘要
    _WRITE_TOOLS = {
        "create_document", "create_feishu_doc", "edit_feishu_doc",
        "write_feishu_doc", "update_feishu_doc",
        "create_bitable", "add_bitable_record", "update_bitable_record",
        "export_file", "self_write_file", "self_edit_file",
        "send_feishu_message", "reply_feishu_message",
        "create_calendar_event", "create_task",
        "save_memory", "create_custom_tool", "create_plan",
    }
    _READ_TOOLS = {
        "read_feishu_doc", "get_bitable_records", "read_server_logs",
        "search_logs", "recall_memory", "web_search", "search_social_media",
    }

    if func_name in _WRITE_TOOLS:
        parts = ["→ 成功"]
        if title:
            parts.append(f"「{title}」")
        if urls:
            parts.append(urls[0][:120])
        return " ".join(parts) if len(parts) > 1 else "→ 成功"

    if func_name in _READ_TOOLS:
        # 读操作不需要太多细节
        if title:
            return f"→ 读取了「{title}」"
        content_len = len(result_str)
        return f"→ 返回了 {content_len} 字符数据"

    # 通用：有 URL 就带上
    if urls:
        return f"→ 完成 {urls[0][:120]}"
    if title:
        return f"→ 完成「{title}」"
    return "→ 完成"


def _set_tool_summary(
    tool_names: list[str],
    call_log: list[str] | None = None,
    action_outcomes: list[tuple[str, str]] | None = None,
) -> None:
    """构建工具调用摘要并写入 contextvar。

    包含工具名 + 关键结果摘要，让下一轮对话的模型知道上轮具体做了什么。
    用 <tools_used> 标签包裹，避免 LLM 在回复中模仿输出。
    """
    try:
        from app.services.history import last_tool_summary

        # 如果有 action_outcomes，用它构建带结果的摘要
        if action_outcomes:
            lines = []
            seen = set()
            for func_name, outcome in action_outcomes:
                if func_name == "think":
                    continue
                # 同名工具可能调多次，都保留
                line = f"{func_name} {outcome}"
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
            if lines:
                # 最多保留 15 条，避免 history 膨胀
                summary = "\n".join(lines[-15:])
                last_tool_summary.set(
                    f"<tools_used>\n{summary}\n</tools_used>"
                )
                return

        # fallback: 只有工具名（兼容旧调用方式）
        names = []
        if call_log:
            for entry in call_log:
                if entry.startswith("think("):
                    continue
                name = entry.split("(", 1)[0]
                if name and name not in names:
                    names.append(name)
        if not names:
            names = list(dict.fromkeys(tool_names))

        if names:
            last_tool_summary.set(
                "<tools_used>" + ",".join(names[-10:]) + "</tools_used>"
            )
    except Exception:
        pass  # 不影响主流程


# ── 安全 ──

def _get_custom_tool_risk(tenant_id: str, tool_name: str) -> str:
    """查询自定义工具的风险级别。内置工具返回空字符串（不拦截）。"""
    if not tenant_id or tool_name in ALL_TOOL_MAP:
        return ""
    from app.services.redis_client import execute as redis_exec
    key = f"custom_tools:{tenant_id}:{tool_name}"
    return redis_exec("HGET", key, "risk_level") or ""


def _user_confirmed(messages: list[dict], tool_name: str) -> bool:
    """检查对话历史中用户是否已确认执行该工具。

    检测逻辑：如果之前 bot 已经因为该工具被拦截（blocked）而回复，
    且用户之后发了确认消息（包含"确认""好的""执行""同意"等），视为已确认。
    """
    _CONFIRM_KEYWORDS = {"确认", "好的", "执行", "同意", "ok", "OK", "可以", "是的", "好", "行", "没问题", "go"}
    found_block = False
    for msg in reversed(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if role == "user" and found_block:
            return any(kw in content for kw in _CONFIRM_KEYWORDS)
        if role == "assistant" and tool_name in content and "⚠️" in content:
            found_block = True
    return False


# ── 全量 OpenAI 工具定义（遗留，供 kimi_coder 使用）──

ALL_OPENAI_TOOLS = _to_openai_tools(
    [_THINK_TOOL_DEF]
    + CALENDAR_TOOLS + DOC_TOOLS + MINUTES_TOOLS + TASK_TOOLS
    + USER_TOOLS + MESSAGE_TOOLS + WEB_TOOLS
    + REPO_SEARCH_TOOLS + FILE_TOOLS + GIT_TOOLS + GITHUB_TOOLS + ISSUE_TOOLS
    + SELF_TOOLS + SERVER_TOOLS
    + MEMORY_TOOLS + BITABLE_TOOLS
    + CUSTOM_TOOL_TOOLS
    + PROVISION_TOOLS
    + FILE_EXPORT_TOOLS
    + VIDEO_URL_TOOLS
    + SKILL_TOOLS
)


# ── OpenAI 格式工具结果压缩 ──

def _compress_old_tool_results(
    messages: list[dict],
    keep_recent: int = _COMPRESS_KEEP_RECENT,
) -> None:
    """压缩旧轮次的工具结果，节省 context token。

    保留最近 keep_recent 条 tool 消息原文不动，
    更早的 tool 消息截断为前 200 字符 + 标记。
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_recent:
        return
    compressed = 0
    for idx in tool_indices[:-keep_recent]:
        content = messages[idx].get("content", "")
        if len(content) > 200:
            messages[idx]["content"] = content[:200] + "\n...[已压缩]"
            compressed += 1
    if compressed:
        logger.debug("compressed %d old tool results", compressed)
