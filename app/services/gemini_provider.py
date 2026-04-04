"""Gemini 原生多模态 Provider

使用 google-genai SDK 实现原生视频/音频/图片理解。
一个 API 调用即可同时处理文本+图片+视频+音频，无需 ffmpeg 或 STT。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from typing import Callable, Awaitable

from google import genai
from google.genai import types

from app.harness import (
    build_tool_settle_nudge,
    compress_gemini_function_results,
    infer_turn_mode,
    normalize_inbox_item,
    sanitize_suggested_groups,
    should_compact_history,
    should_run_code_preflight,
    should_nudge_unmatched_reads,
)
from app.services.base_agent import (
    _build_system_prompt,
    _strip_degenerate_repetition,
    _trigger_memory,
    _is_admin,
    _get_tenant_tools,
    _expand_tool_group,
    _get_custom_tool_risk,
    _has_unmatched_reads,
    check_unfulfilled_deliverables,
    detect_action_claims,
    detect_ungrounded_claims,
    llm_exit_review,
    _CUSTOM_TOOL_META_NAMES,
    _GROUP_DESCRIPTIONS,
    classify_task_type,
    should_delegate_to_sub_agent,
    build_sub_agent_system_prompt,
    get_sub_agent_config,
    ALL_TOOL_MAP,
    _MAX_ROUNDS,
    _MAX_TOOL_RESULT_LEN,
    _COMPRESS_KEEP_RECENT,
    _COMPRESS_AFTER_ROUND,
    _drain_inbox,
    _generate_progress_hint,
    _strip_hallucinated_code_blocks,
    _extract_outcome,
    ProgressCallback,
    extract_urls,
    check_url_provenance,
    check_write_intent,
    record_agent_progress,
    reset_agent_progress,
    build_timeout_message,
)
from app.tools.tool_result import ToolResult
from app.services.error_log import record_error
from app.services.tool_tracker import (
    record_tool_call, build_experience_hint, build_combo_hint,
    record_tool_sequence, flush_session_sequence, reset_session_failures,
)

logger = logging.getLogger(__name__)

# ── LLM 意图分类（替代硬编码关键词） ──

# 快速消息关键词（这些不需要 LLM 判断，直接走 quick 路径）
_QUICK_KEYWORDS = frozenset({
    "你好", "谢谢", "好的", "收到", "嗯", "ok", "hi", "hello",
    "再见", "拜拜", "没事了", "算了",
})

_CLASSIFY_PROMPT = """\
Classify this user message into ONE task type. Reply with ONLY a JSON object.

Task types:
- quick: greeting, thanks, acknowledgment, casual short reply
- normal: general question, conversation, simple request
- research: in-depth research, competitor analysis, social media analysis, market research, data collection
- deep: code changes, deployment, architecture, bug fix, complex multi-step technical task
- provision: create/deploy/manage bot instance, tenant onboarding, configure new service

Also list which tool groups the task needs (pick 1-3):
- core: basic tools (search, memory, export)
- feishu_collab: Feishu calendar/docs/tasks/messages
- code_dev: code files, Git, GitHub
- devops: server ops, deployment, logs
- research: social media, browser automation
- content: file export, video analysis, PDF/PPT
- admin: instance management, provisioning, package install
- extension: custom tools, skills

JSON format: {"type": "<type>", "groups": ["core", ...]}

User message:
"""


_FEISHU_CONTEXT_HINT_RE = re.compile(
    r"(飞书|日历|日程|会议|calendar|任务|task|tasklist|文档|document|纪要|minutes|"
    r"多维表格|bitable|邮件|mail|群消息|聊天记录|刚才|刚刚|上条|上一条|"
    r"我发了什么|我刚发|发给你|消息记录|chat history)",
    re.IGNORECASE,
)


def _extract_first_json_object(raw: str) -> str | None:
    """Extract the first balanced JSON object from model output."""
    if not raw:
        return None
    text = raw.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                text = candidate
                break
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _needs_feishu_collab_context(user_text: str) -> bool:
    return bool(_FEISHU_CONTEXT_HINT_RE.search(user_text or ""))


def _classify_intent_keywords(user_text: str) -> dict:
    """关键词 fallback 分类器 —— 当 LLM 分类失败时使用，保证总能返回有效结果。

    P3 改进：CF Worker 代理不传 response_mime_type 导致 LLM 分类几乎每次都 fallback
    到这里。因此这个分类器必须足够智能，不能只返回 core 组导致工具不全。

    核心原则：宁可多加组（多几个工具 LLM 可以忽略）也不要少加（缺工具 LLM 无法完成任务）。
    """
    inferred = infer_turn_mode(user_text)
    groups = list(inferred.groups)
    if len(groups) == 1 and _needs_feishu_collab_context(user_text):
        groups.append("feishu_collab")
    return {"type": inferred.task_type, "groups": list(dict.fromkeys(groups))}


async def _classify_intent_llm(
    client: genai.Client,
    model_name: str,
    user_text: str,
) -> dict | None:
    """Use LLM to classify user intent. Returns {"type": ..., "groups": [...]} or None on failure."""
    # 语音消息跳过 LLM 意图分类 —— 分类器只看文本（"[语音消息]..."），
    # 看不到实际音频内容，LLM 分类必然错误。直接返回通用类型让主 agent 理解语音。
    text_stripped = user_text.strip()
    if "[语音消息]" in text_stripped or "[音频]" in text_stripped:
        logger.info("LLM intent classification: voice message detected, skipping classification")
        return {"type": "normal", "groups": ["core", "research"]}

    # 快速消息跳过 LLM 调用
    if len(text_stripped) < 5:
        return {"type": "quick", "groups": ["core"]}
    text_lower = text_stripped.lower()
    for kw in _QUICK_KEYWORDS:
        if text_lower == kw or (len(text_lower) < 15 and kw in text_lower):
            return {"type": "quick", "groups": ["core"]}

    try:
        # 截断长消息（分类只需要前 200 字）
        truncated = text_stripped[:200]
        # 在 prompt 尾部追加 JSON 强制指令，防止 CF Worker 代理不传递 response_mime_type
        classification_input = _CLASSIFY_PROMPT + truncated + "\n\nReply ONLY with valid JSON, no other text. Example: {\"type\": \"normal\", \"groups\": [\"core\"]}"
        resp = await client.aio.models.generate_content(
            model=model_name,
            contents=classification_input,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=150,
                # 禁止 thinking，否则输出可能被 thinking 消耗导致 text 为空
                thinking_config=types.ThinkingConfig(include_thoughts=False),
                # 强制 JSON 输出（CF Worker 代理可能不传递此参数，靠 prompt 兜底）
                response_mime_type="application/json",
                # 定义精确的 response schema，增强 JSON 输出的可靠性
                response_schema={
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["quick", "normal", "research", "deep", "provision"]},
                        "groups": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["type", "groups"]
                },
            ),
        )
        raw = (resp.text or "").strip()
        if not raw:
            logger.warning("LLM intent classification returned empty text, using keyword fallback")
            return _classify_intent_keywords(user_text)
        # response_mime_type="application/json" 保证输出是纯 JSON
        # 但 CF Worker 代理可能不传递 mime_type，模型返回非 JSON（如 "Here is"）
        # 多层防御：code block → regex JSON 提取 → 直接 parse → keyword fallback
        raw = _extract_first_json_object(raw) or raw
        if not raw:
            logger.warning("LLM intent classification returned empty JSON after stripping code block")
            return _classify_intent_keywords(user_text)
        if not raw.startswith("{"):
            logger.warning("LLM intent classification: no JSON found in raw=%r, using keyword fallback", raw[:100])
            return _classify_intent_keywords(user_text)
        result = json.loads(raw)
        task_type = result.get("type", "normal")
        groups = result.get("groups", ["core"])
        # 校验
        valid_types = {"quick", "normal", "research", "deep", "provision"}
        if task_type not in valid_types:
            task_type = "normal"
        valid_groups = {"core", "feishu_collab", "code_dev", "devops",
                        "research", "content", "admin", "extension"}
        groups = [g for g in groups if g in valid_groups]
        if "core" not in groups:
            groups.insert(0, "core")
        logger.info("LLM intent classification: type=%s, groups=%s", task_type, groups)
        return {"type": task_type, "groups": groups}
    except json.JSONDecodeError as e:
        logger.warning("LLM intent classification: invalid JSON (%s), raw=%r, using keyword fallback", e, raw[:100] if raw else "(empty)")
        return _classify_intent_keywords(user_text)
    except Exception as e:
        logger.warning("LLM intent classification failed (%s), using keyword fallback", e)
        return _classify_intent_keywords(user_text)


# ── Code Preflight: 代码修改任务的上下文预加载 ──
#
# 第一性原理: CC 之所以改代码不遗漏，是因为它在动手前自然地 grep 了整个项目。
# Bot 有 search_code 工具但不会主动用——因为每次 API 调用都"贵"（慢）。
# Preflight 在 agent loop 之前自动完成探索，给模型一个 CC 级别的起跑线。

# 代码修改意图关键词（用于从用户消息中提取要搜索的标识符）
_CODE_IDENT_RE = re.compile(
    r'\b([A-Z][a-zA-Z0-9]{2,}(?:Manager|Controller|UI|System|Handler|Service|Data|Config|View)?)\b'
    r'|'
    r'\b([a-z][a-zA-Z0-9]{3,}(?:List\d*|Array|Map|Dict|Data|Config|UI)?)\b'
)

# 不搜这些（太泛）
_PREFLIGHT_STOP_WORDS = frozenset({
    "Unity", "GameObject", "Transform", "Component", "MonoBehaviour",
    "String", "Boolean", "Integer", "Float", "Double", "Object",
    "List", "Array", "Dict", "Data", "Config", "True", "False",
    "None", "Null", "This", "Class", "Type", "View", "System",
    "Vector2", "Vector3", "Quaternion", "Color", "Rect",
    "Task", "Async", "Await", "Event", "Action", "Func",
    "Debug", "Console", "Logger", "Error", "Exception",
})


async def _code_preflight_context(
    user_text: str,
    history: list[dict] | None = None,
) -> str | None:
    """代码修改任务的上下文预加载。

    从用户消息中提取关键标识符（类名、变量名等），
    并行调用 search_code 获取所有引用，
    返回格式化的上下文字符串注入到 agent contents 中。

    Returns None if no code identifiers found or search fails.
    """
    from app.tools.repo_search import search_code

    # 1. 提取标识符
    identifiers: set[str] = set()
    for m in _CODE_IDENT_RE.finditer(user_text):
        name = m.group(1) or m.group(2)
        if name and name not in _PREFLIGHT_STOP_WORDS and len(name) >= 4:
            identifiers.add(name)

    # 也从最近的对话历史中提取（用户可能说"改一下那个 XXX"）
    if history:
        for msg in history[-4:]:  # 最近 2 轮
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            for m in _CODE_IDENT_RE.finditer(str(content)):
                name = m.group(1) or m.group(2)
                if name and name not in _PREFLIGHT_STOP_WORDS and len(name) >= 4:
                    identifiers.add(name)

    if not identifiers:
        logger.info("code preflight: no identifiers extracted from user message")
        return None

    # 限制搜索数量（最多 5 个，避免太慢）
    search_terms = sorted(identifiers, key=len, reverse=True)[:5]
    logger.info("code preflight: searching for %s", search_terms)

    # 2. 并行搜索
    loop = asyncio.get_event_loop()
    results: list[tuple[str, str]] = []

    async def _search_one(term: str) -> tuple[str, str]:
        try:
            result = await loop.run_in_executor(None, search_code, term)
            if isinstance(result, ToolResult):
                return (term, result.content if result.ok else "")
            return (term, str(result or ""))
        except Exception as e:
            logger.warning("code preflight search failed for %r: %s", term, e)
            return (term, "")

    search_tasks = [_search_one(term) for term in search_terms]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    for item in search_results:
        if isinstance(item, tuple):
            term, data = item
            if data and "没有找到" not in data:
                results.append((term, data))

    if not results:
        logger.info("code preflight: no search results found")
        return None

    # 3. 格式化上下文
    lines = ["[代码上下文预加载] 以下是与你的任务相关的代码引用，在修改前仔细阅读：\n"]
    total_len = 0
    for term, data in results:
        section = f"── search_code(\"{term}\") ──\n{data}\n"
        if total_len + len(section) > 6000:  # 限制总长度
            lines.append(f"（还有更多结果被截断，请用 search_code 继续搜索）")
            break
        lines.append(section)
        total_len += len(section)

    lines.append(
        "\n⚠️ 重要：上面是 preflight 自动搜索的结果。"
        "修改代码前，确认所有相关文件都已覆盖。"
        "如果看到名字相似的变量/方法（如 listA 和 listB），它们通常需要一起改。"
    )

    context = "\n".join(lines)
    logger.info("code preflight: injected %d chars of context (%d search terms)",
                len(context), len(results))
    return context


# 超管专属工具名 — 非超管调用时硬拦截（不依赖 LLM 遵守 system prompt）
_SUPER_ADMIN_ONLY_TOOLS = frozenset({
    "provision_tenant", "list_instances", "get_instance_status",
    "restart_instance", "destroy_instance",
})

# 限制 Gemini API 并发
_api_semaphore = asyncio.Semaphore(5)

# 内联视频最大尺寸（字节）：超过则回退到抽帧
MAX_INLINE_VIDEO_SIZE = 15 * 1024 * 1024  # 15MB


def _parse_data_url(data_url: str) -> tuple[str, bytes]:
    """解析 data:mime;base64,xxx 为 (mime_type, raw_bytes)"""
    match = re.match(r"data:([^;]+);base64,(.+)", data_url, re.DOTALL)
    if not match:
        return "application/octet-stream", b""
    return match.group(1), base64.b64decode(match.group(2))


async def _maybe_compress_inline(mime_type: str, raw_bytes: bytes) -> tuple[str, bytes]:
    """对大媒体做 inline_data 压缩，返回 (new_mime, new_bytes)。

    仅在代理模式（无 File API）下由调用方触发。
    - 大音频 (>1MB): 压缩为 64kbps OGG Opus
    - 大视频 (>4MB): 压缩为 720p CRF32 MP4
    小文件原样返回。
    """
    if mime_type.startswith("audio/") and len(raw_bytes) > 1024 * 1024:
        from app.services.media_processor import compress_audio_for_inline
        compressed = await compress_audio_for_inline(raw_bytes)
        if compressed is not raw_bytes:
            return "audio/ogg", compressed
    elif mime_type.startswith("video/") and len(raw_bytes) > 4 * 1024 * 1024:
        from app.services.media_processor import compress_video_for_inline
        compressed = await compress_video_for_inline(raw_bytes)
        if compressed and len(compressed) < len(raw_bytes):
            return "video/mp4", compressed
    return mime_type, raw_bytes


# mime 到文件扩展名映射（用于 File API 上传）
_MIME_EXT = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/avi": ".avi",
    "audio/ogg": ".ogg", "audio/amr": ".amr", "audio/wav": ".wav",
    "audio/mpeg": ".mp3", "audio/flac": ".flac",
    "application/pdf": ".pdf",
}


async def _upload_to_file_api(
    client: genai.Client, data: bytes, mime_type: str,
) -> object | None:
    """上传媒体到 Gemini File API，等待处理完成后返回 file 对象

    File API 能让 Gemini 真正理解视频（多帧+音频）和音频内容，
    而 inline_data 只能看到一帧静态画面。
    """
    ext = _MIME_EXT.get(mime_type, ".bin")
    try:
        file = await client.aio.files.upload(
            file=io.BytesIO(data),
            config={"mime_type": mime_type, "display_name": f"upload{ext}"},
        )
        logger.info("file API upload: name=%s state=%s size=%dKB",
                     file.name, file.state, len(data) // 1024)

        # 等待服务端处理（视频需要抽帧等），最多等 60 秒
        for _ in range(30):
            if not file.state or file.state.name != "PROCESSING":
                break
            await asyncio.sleep(2)
            file = await client.aio.files.get(name=file.name)

        if file.state and file.state.name == "FAILED":
            logger.warning("file API upload FAILED: %s", file.name)
            return None

        logger.info("file API ready: name=%s uri=%s", file.name, file.uri)
        return file
    except Exception:
        logger.warning("file API upload error", exc_info=True)
        return None


def _openai_tools_to_gemini(openai_tools: list[dict]) -> list[dict]:
    """将 OpenAI function-calling 工具格式转为 Gemini function_declarations"""
    decls = []
    for t in openai_tools:
        f = t.get("function", {})
        params = dict(f.get("parameters", {}))
        # Gemini 不支持这些 JSON Schema 扩展字段
        params.pop("additionalProperties", None)
        params.pop("$schema", None)
        decls.append({
            "name": f["name"],
            "description": f.get("description", ""),
            "parameters": params,
        })
    return decls


def _compress_old_gemini_results(
    contents: list[types.Content],
    keep_recent: int = _COMPRESS_KEEP_RECENT,
) -> None:
    """压缩 Gemini contents 中旧轮次的 function_response 结果。"""
    compress_gemini_function_results(contents, keep_recent=keep_recent, logger=logger)


def _clean_error_msg(raw: str, max_len: int = 120) -> str:
    """清理错误消息：去掉 HTML 标签，截断过长内容，避免把 CF 504 整页 HTML 吐给用户。"""
    # 去掉 HTML 标签
    cleaned = re.sub(r"<[^>]+>", "", raw)
    # 压缩连续空白
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "…"
    return cleaned or raw[:max_len]


def _maybe_record_watchdog(
    user_text: str,
    tool_names_called: list[str],
    sender_id: str,
    sender_name: str,
    chat_id: str,
    chat_type: str,
    reply: str,
) -> None:
    """检查是否有未完成交付物，有则记录到 watchdog 供后台重试。"""
    missing = check_unfulfilled_deliverables(user_text, tool_names_called)
    if not missing:
        return
    try:
        import time as _t
        from app.tenant.context import get_current_tenant
        from app.services.task_watchdog import IncompleteTask, record_incomplete_task
        tenant = get_current_tenant()
        record_incomplete_task(IncompleteTask(
            tenant_id=tenant.tenant_id,
            platform=tenant.platform,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=chat_id,
            chat_type=chat_type,
            user_text=user_text[:500],
            tools_called=tool_names_called,
            missing_deliverables=missing,
            reply_sent=reply[:200],
            recorded_at=_t.time(),
        ))
    except Exception:
        logger.warning("watchdog record failed", exc_info=True)


def _record_sub_agent_metrics(
    tenant_id: str, agent_type: str, rounds: int, tool_calls: int,
    elapsed_s: float, result_len: int, outcome: str,
    tools_used: list[str] | None = None,
) -> None:
    """火速记录子 agent 指标（fail-open）。"""
    try:
        from app.services.metering import record_sub_agent_run
        record_sub_agent_run(
            tenant_id, agent_type, rounds, tool_calls,
            elapsed_s, result_len, outcome, tools_used,
        )
    except Exception:
        logger.warning("sub-agent metrics record failed", exc_info=True)


async def _run_sub_agent(
    client: genai.Client,
    model_name: str,
    strong_model: str | None,
    sub_agent_type: str,
    user_text: str,
    parent_system_prompt: str,
    tenant,
    sender_id: str = "",
    sender_name: str = "",
    on_progress: ProgressCallback | None = None,
    history: list[dict] | None = None,
) -> str:
    """在隔离的上下文中运行子 agent，返回结构化结果摘要。

    子 agent 有独立的：
    - system prompt（专注于委托任务）
    - 时间预算和 stall 策略
    - 工具集（只加载任务相关工具组）
    - contents（不污染主 agent 上下文，但继承最近对话历史）

    返回子 agent 的最终文本结果（摘要），供主 agent 使用。
    """
    agent_cfg = get_sub_agent_config(sub_agent_type)
    sub_system_prompt = build_sub_agent_system_prompt(
        sub_agent_type, parent_system_prompt, user_text,
    )

    # 子 agent 只加载声明的工具组（精确隔离）
    openai_tools, tool_map = _get_tenant_tools(
        tenant,
        user_text=user_text,
        override_groups=agent_cfg.get("tool_groups"),
    )

    # 构建子 agent 的 Gemini config
    gemini_decls = _openai_tools_to_gemini(openai_tools)
    config = types.GenerateContentConfig(
        system_instruction=sub_system_prompt,
        tools=[types.Tool(function_declarations=gemini_decls)],
        temperature=1.0,
        max_output_tokens=32768,
        thinking_config=types.ThinkingConfig(include_thoughts=False),
    )
    # 子 agent 独立的 contents（继承最近对话历史，提供上下文）
    contents: list[types.Content] = []

    # 注入最近 3 轮对话历史（避免子 agent "失忆"）
    # Gemini 要求 user/model 严格交替，需要合并连续同角色消息
    _SUB_AGENT_HISTORY_ROUNDS = 3  # 最多 6 条消息（3 user + 3 assistant）
    if history:
        recent = history[-(2 * _SUB_AGENT_HISTORY_ROUNDS):]
        for msg in recent:
            role = "model" if msg.get("role") == "assistant" else "user"
            text = msg.get("content", "")
            if isinstance(text, list):
                text = " ".join(
                    p.get("text", "") for p in text if isinstance(p, dict)
                )
            if not text:
                continue
            # Gemini 要求角色交替：连续同角色 → 合并到上一条
            if contents and contents[-1].role == role:
                prev_text = contents[-1].parts[0].text if contents[-1].parts else ""
                contents[-1] = types.Content(
                    role=role,
                    parts=[types.Part(text=f"{prev_text}\n{text}")],
                )
            else:
                contents.append(types.Content(
                    role=role,
                    parts=[types.Part(text=text)],
                ))
        # Gemini 要求第一条必须是 user，如果历史以 model 开头则丢弃前面的 model
        while contents and contents[0].role == "model":
            contents.pop(0)
        # 确保历史最后一条不是 user（否则和下面的 user_text 冲突）
        # 如果是 user → 追加一个 model 占位
        if contents and contents[-1].role == "user":
            contents.append(types.Content(
                role="model",
                parts=[types.Part(text="好的。")],
            ))

    contents.append(types.Content(
            role="user",
            parts=[types.Part(text=user_text)],
        ),
    )

    max_rounds = agent_cfg["max_rounds"]
    budget = agent_cfg["budget_seconds"]
    call_log: list[str] = []
    tool_names_called: list[str] = []
    action_outcomes: list[tuple[str, str]] = []
    _loop_start = time.monotonic()
    _escalated = False
    _pro_failures = 0
    _exit_nudged = False  # 防止 exit gate nudge 死循环，最多 nudge 一次
    # Sub-agent URL 溯源
    _sub_seen_urls: set[str] = extract_urls(user_text)
    _sub_blocked_urls: set[str] = set()
    if history:
        for msg in history:
            if isinstance(msg, dict) and msg.get("content"):
                _sub_seen_urls.update(extract_urls(str(msg["content"])))

    logger.info(
        "sub-agent [%s] started: budget=%ds, max_rounds=%d",
        sub_agent_type, budget, max_rounds,
    )

    _sub_outcome = "success"  # 默认成功，各 break 点覆盖

    round_num = 0  # 在循环外初始化（异常恢复时需要访问）
    try:
      for round_num in range(max_rounds):
        # 模型选择（复杂子 agent 也支持升级）
        current_model = model_name
        _pro_ok = (strong_model and strong_model != model_name
                   and _pro_failures < 2)
        if _pro_ok and (_escalated or round_num >= 8):
            current_model = strong_model

        logger.info("sub-agent [%s] round %d (model=%s)",
                    sub_agent_type, round_num + 1, current_model)

        # API 调用（简化重试，子 agent 不需要和主 agent 一样复杂的重试逻辑）
        try:
            async with _api_semaphore:
                _timeout = 90 if current_model == model_name else 120
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=current_model,
                        contents=contents,
                        config=config,
                    ),
                    timeout=_timeout,
                )
        except asyncio.TimeoutError:
            if current_model != model_name:
                _pro_failures += 1
                current_model = model_name
                continue
            logger.warning("sub-agent [%s] timeout at round %d", sub_agent_type, round_num + 1)
            _sub_outcome = "timeout"
            break
        except Exception as exc:
            logger.warning("sub-agent [%s] API error at round %d: %s",
                           sub_agent_type, round_num + 1, exc)
            _sub_outcome = "error"
            break

        if not response.candidates:
            break

        candidate = response.candidates[0]
        content_obj = candidate.content
        if not content_obj or not content_obj.parts:
            break

        # 分离工具调用和文本
        function_calls = []
        text_parts = []
        for part in content_obj.parts:
            if part.function_call:
                function_calls.append(part)
            elif part.text and not getattr(part, 'thought', False):
                text_parts.append(part.text)

        # 没有工具调用 → 子 agent 可能完成了工作，需要验证
        if not function_calls:
            reply = "\n".join(text_parts).strip()
            if reply:
                # ── Sub-agent exit gate: 用 LLM 判断是否真的完成 ──
                # 防止 sub-agent 在第一轮就返回"搞定了/请查看附件"但实际没调工具
                # 只 nudge 一次，避免死循环
                if not _exit_nudged:
                    _exit_verdict = await llm_exit_review(
                        reply, user_text, tool_names_called,
                        gemini_client=client,
                    )
                    if _exit_verdict == "nudge":
                        _exit_nudged = True
                        logger.info(
                            "sub-agent [%s] exit gate: nudging (reply claims action, "
                            "tools_called=%s)", sub_agent_type, tool_names_called,
                        )
                        # 把模型的回复加入上下文，再追加 nudge 消息让它实际执行
                        contents.append(content_obj)
                        contents.append(types.Content(
                            role="user",
                            parts=[types.Part(text=(
                                "你刚才描述了要做的事情，但还没有实际调用工具执行。"
                                "请现在调用相应的工具来完成任务，不要只是描述。"
                            ))],
                        ))
                        continue  # 回到 loop 让模型重新生成（这次带工具调用）

                _elapsed = time.monotonic() - _loop_start
                logger.info(
                    "sub-agent [%s] completed in %d rounds (%.0fs), reply=%d chars",
                    sub_agent_type, round_num + 1, _elapsed, len(reply),
                )
                _record_sub_agent_metrics(
                    tenant.tenant_id, sub_agent_type, round_num + 1,
                    len(tool_names_called), _elapsed, len(reply),
                    "success", tool_names_called,
                )
                return _strip_hallucinated_code_blocks(
                    _strip_degenerate_repetition(reply)
                )
            break

        # ── 执行工具调用（独立工具并行，有状态工具串行） ──
        contents.append(content_obj)

        # 准备所有工具调用的参数
        _fc_items: list[tuple[str, dict, str]] = []  # (name, args, call_key)
        for fc_part in function_calls:
            fc = fc_part.function_call
            func_name = fc.name
            func_args = dict(fc.args) if fc.args else {}

            # 自动注入 tenant_id / sender 上下文
            if func_name in _CUSTOM_TOOL_META_NAMES and "tenant_id" not in func_args:
                func_args["tenant_id"] = tenant.tenant_id
            if func_name in ("save_memory", "recall_memory"):
                if not func_args.get("user_id"):
                    func_args["user_id"] = sender_id
                if not func_args.get("user_name"):
                    func_args["user_name"] = sender_name

            call_key = f"{func_name}({json.dumps(func_args, ensure_ascii=False)})"
            call_log.append(call_key)
            if func_name != "think":
                tool_names_called.append(func_name)
            _fc_items.append((func_name, func_args, call_key))

        async def _exec_one(name: str, args: dict) -> str:
            """执行单个工具调用，返回结果字符串。"""
            # URL 溯源验证（sub-agent 也需要）
            _url_warn, _flagged = check_url_provenance(
                name, args, _sub_seen_urls, _sub_blocked_urls,
            )
            if _url_warn:
                logger.warning("sub-agent URL provenance failed for %s: %s", name, _url_warn[:200])
                _sub_blocked_urls.update(_flagged)
                return f"[ERROR] {_url_warn}"
            # 写操作意图验证（独立 evaluator）
            _intent_block = check_write_intent(
                name, args, user_text, tool_names_called,
            )
            if _intent_block:
                logger.warning("sub-agent write intent blocked: %s", _intent_block[:200])
                return f"[ERROR] {_intent_block}"
            handler = tool_map.get(name)
            if not handler:
                return f"[ERROR] 工具 '{name}' 不存在"
            try:
                result = handler(args) if not asyncio.iscoroutinefunction(handler) else await handler(args)
                record_agent_progress(name)
                if isinstance(result, ToolResult):
                    return result.content
                return str(result) if result is not None else "OK"
            except Exception as exc:
                logger.warning("sub-agent tool error %s: %s", name, exc)
                return f"[ERROR] {exc}"

        # 并行执行：同一轮多个独立工具调用用 gather 并发
        # （同轮调用天然独立——有依赖的工具 LLM 会分到不同轮次）
        if len(_fc_items) > 1:
            logger.info("sub-agent [%s] parallel exec: %d tools [%s]",
                        sub_agent_type, len(_fc_items),
                        ", ".join(n for n, _, _ in _fc_items))
            _results = await asyncio.gather(
                *(_exec_one(n, a) for n, a, _ in _fc_items),
                return_exceptions=True,
            )
            result_strs = [
                str(r) if not isinstance(r, BaseException) else f"[ERROR] {r}"
                for r in _results
            ]
        else:
            # 单个工具直接 await（省 gather 开销）
            n, a, _ = _fc_items[0]
            logger.info("sub-agent [%s] tool: %s(%s)",
                        sub_agent_type, n, a)
            result_strs = [await _exec_one(n, a)]

        # 组装 response_parts + 记录
        response_parts: list[types.Part] = []
        for (func_name, func_args, _), result_str in zip(_fc_items, result_strs):
            # Sub-agent URL 溯源：在截断前从完整数据中提取 URL
            _sub_seen_urls.update(extract_urls(result_str))

            if len(result_str) > _MAX_TOOL_RESULT_LEN:
                result_str = result_str[:_MAX_TOOL_RESULT_LEN] + "\n...[结果已截断]"

            outcome = _extract_outcome(func_name, result_str, func_args)
            action_outcomes.append((func_name, outcome))

            response_parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=func_name,
                    response={"result": result_str},
                )
            ))

            try:
                is_error = result_str.startswith("[ERROR]")
                record_tool_call(tenant.tenant_id, func_name, not is_error)
            except Exception:
                pass

        contents.append(types.Content(role="user", parts=response_parts))

        # ── 中间结果流式通知 ──
        # 每 4 轮发一次进度（从 round 3 开始），避免频繁刷屏
        _real_tools = [n for n, _, _ in _fc_items if n != "think"]
        if on_progress and _real_tools and round_num >= 5 and round_num % 6 == 5:
            try:
                _progress_msg = await _generate_progress_hint(
                    tool_names_called, round_num // 6,
                    gemini_client=client,
                    user_text=user_text,
                )
                if _progress_msg:
                    await on_progress(_progress_msg)
            except Exception:
                pass

        # 压缩旧结果
        if round_num >= 6:
            _compress_old_gemini_results(contents)

    except Exception as exc:
        # ── 失败状态恢复：保留中间结果，不白跑 ──
        _sub_outcome = "crash"
        _elapsed = time.monotonic() - _loop_start
        logger.error(
            "sub-agent [%s] crashed at round %d (%.0fs): %s, "
            "recovering %d partial tool results",
            sub_agent_type, round_num + 1, _elapsed, exc,
            len(action_outcomes), exc_info=True,
        )
        # 通知用户出错但有部分结果
        if on_progress and action_outcomes:
            try:
                _done = ", ".join(dict.fromkeys(n for n, _ in action_outcomes))
                await on_progress(f"处理中遇到异常，正在整理已完成的部分结果（{_done}）")
            except Exception:
                pass

    # 循环结束（正常退出 / break / 异常恢复）→ 用 LLM 综合答案
    _elapsed = time.monotonic() - _loop_start
    logger.info(
        "sub-agent [%s] forcing final summary after %d rounds (%.0fs), outcome=%s, tools=%s",
        sub_agent_type, round_num + 1, _elapsed, _sub_outcome, tool_names_called[-10:],
    )
    # 让 LLM 从已收集的数据综合出用户需要的答案，而非 dump 原始工具日志
    try:
        _summary_prompt = (
            "你之前在帮用户完成任务，已经收集了很多数据，但因为轮次/时间限制被中断了。\n"
            "请根据你在对话中已经收集到的所有信息，给用户一个尽可能完整的答案。\n"
            "注意：\n"
            "- 只输出对用户有用的信息（搜索结果、数据、分析等），不要输出工具调用细节\n"
            "- 不要说'我还没做完'或'你可以说继续'——直接给出你已有的答案\n"
            "- 如果信息确实不完整，在答案末尾简短说明哪些部分还没查到\n"
            "- 保持你的人设和说话风格\n"
        )
        contents.append(types.Content(
            role="user",
            parts=[types.Part(text=_summary_prompt)],
        ))
        _summary_resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=sub_system_prompt,
                    temperature=0.7,
                    max_output_tokens=2000,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout=15.0,
        )
        _summary_text = (_summary_resp.text or "").strip()
        if _summary_text and len(_summary_text) > 20:
            reply = _strip_hallucinated_code_blocks(
                _strip_degenerate_repetition(_summary_text)
            )
            logger.info("sub-agent [%s] LLM summary: %d chars", sub_agent_type, len(reply))
        else:
            reply = _build_factual_summary(
                tool_names_called, action_outcomes,
                f"子任务执行结束（{_sub_outcome}）。" if _sub_outcome != "success" else "",
            )
    except Exception as e:
        logger.warning("sub-agent [%s] LLM summary failed: %s, using factual fallback", sub_agent_type, e)
        reply = _build_factual_summary(
            tool_names_called, action_outcomes,
            f"子任务执行结束（{_sub_outcome}）。" if _sub_outcome != "success" else "",
        )

    _record_sub_agent_metrics(
        tenant.tenant_id, sub_agent_type, round_num + 1,
        len(tool_names_called), _elapsed, len(reply) if reply else 0,
        _sub_outcome, tool_names_called,
    )
    return reply


async def handle_message(
    user_text: str,
    history: list[dict] | None = None,
    sender_name: str = "",
    sender_id: str = "",
    on_progress: ProgressCallback | None = None,
    image_urls: list[str] | None = None,
    mode: str = "safe",
    chat_context: str = "",
    inbox: asyncio.Queue | None = None,
    chat_id: str = "",
    chat_type: str = "",
) -> str:
    """Gemini 原生多模态消息处理

    接口与 kimi_coder.handle_message 完全一致，可直接替换。
    image_urls 支持 data:image/..., data:video/..., data:audio/... 等任意媒体类型。
    """
    from app.tenant.context import get_current_tenant, set_current_sender
    from app.tools.source_registry import reset as _reset_source_registry
    reset_agent_progress()  # 每个请求开始时重置进度跟踪
    tenant = get_current_tenant()

    # 设置发送者上下文（供工具层权限检查读取）
    set_current_sender(sender_id, sender_name)

    # 重置来源注册表（每轮对话重新收集搜索来源）
    _reset_source_registry()

    # ── 构建 Gemini Client（支持代理 / 自定义 base_url）──
    #
    # 优先级：
    # 1. tenant.llm_base_url（仅当明确为 Gemini 反代地址时）
    # 2. GOOGLE_GEMINI_BASE_URL 环境变量（需要手动读取，SDK 不会自动使用）
    # 3. GEMINI_PROXY 环境变量 → httpx 代理
    # 4. 默认直连 generativelanguage.googleapis.com
    http_options: dict = {}
    _custom_base = tenant.llm_base_url
    # 排除 OpenAI / Kimi 等非 Gemini base_url（默认值残留）
    if _custom_base and "moonshot" not in _custom_base and "openai.com" not in _custom_base:
        http_options["base_url"] = _custom_base
    elif os.getenv("GOOGLE_GEMINI_BASE_URL"):
        http_options["base_url"] = os.getenv("GOOGLE_GEMINI_BASE_URL")
    proxy_url = os.getenv("GEMINI_PROXY", "")
    # ── 连接超时保护（防止代理挂掉时连接风暴）──
    # connect=5s: 代理不通时 5 秒快速失败，不要 hang 120 秒堆积连接
    # read=120s: LLM 生成可能很慢，保持长读取超时
    import httpx as _httpx
    _client_args: dict = {
        "timeout": _httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
    }
    if proxy_url:
        _client_args["proxy"] = proxy_url
    http_options["async_client_args"] = _client_args
    http_options["timeout"] = 120_000  # SDK 层总超时 ms
    logger.info("gemini client: base_url=%s model=%s",
                http_options.get("base_url", "(direct)"),
                tenant.llm_model or "gemini-3-flash-preview")
    client = genai.Client(
        api_key=tenant.llm_api_key,
        http_options=http_options,
    )
    model_name = tenant.llm_model or "gemini-3-flash-preview"
    strong_model = tenant.llm_model_strong  # 复杂任务自动升级（如 gemini-2.5-pro）

    # ── LLM 意图分类（在加载工具之前，用分类结果指导工具组选择）──
    _intent = await _classify_intent_llm(client, model_name, user_text)
    _llm_groups: set[str] | None = None
    if _intent:
        _task_type = _intent["type"]
        _llm_groups = set(sanitize_suggested_groups(user_text, _intent["groups"]))
        # provision 映射到 deep 预算（多步骤流程）
        if _task_type == "provision":
            _task_type = "deep"
            _llm_groups.add("admin")
    else:
        _task_type = classify_task_type(user_text)

    openai_tools, tool_map = _get_tenant_tools(
        tenant, user_text=user_text, suggested_groups=_llm_groups,
    )
    # 跟踪当前已加载的工具名（用于 request_more_tools 动态扩展）
    _loaded_tool_names: set[str] = {
        t["function"]["name"] for t in openai_tools if "function" in t
    }
    system_prompt = await _build_system_prompt(
        mode, sender_id=sender_id, sender_name=sender_name,
        user_text=user_text, chat_id=chat_id, chat_type=chat_type,
        task_type=_task_type, actual_tool_names=_loaded_tool_names,
    )
    # 注入工具使用经验（基于历史调用成功/失败率 + 经验教训 + 常用组合）
    try:
        exp_hint = build_experience_hint(tenant.tenant_id, _loaded_tool_names)
        if exp_hint:
            system_prompt += exp_hint
        combo_hint = build_combo_hint(tenant.tenant_id, _loaded_tool_names)
        if combo_hint:
            system_prompt += combo_hint
    except Exception:
        pass

    # 重置会话级追踪（新对话开始）
    reset_session_failures(tenant.tenant_id)

    # ── 构建 Gemini contents ──
    contents: list[types.Content] = []

    # 聊天记录上下文（群聊用）
    if chat_context:
        if len(chat_context) > 2000:
            chat_context = chat_context[:2000]
        contents.append(types.Content(
            role="user",
            parts=[types.Part(text=f"[聊天记录上下文]\n{chat_context}")]
        ))
        contents.append(types.Content(
            role="model",
            parts=[types.Part(text="好的，我已了解聊天上下文。")]
        ))

    # 对话历史（OpenAI 格式 → Gemini 格式）
    if history:
        for msg in history:
            role = "model" if msg.get("role") == "assistant" else "user"
            text = msg.get("content", "")
            if isinstance(text, list):
                text = " ".join(
                    p.get("text", "") for p in text if isinstance(p, dict)
                )
            if not text:
                continue
            contents.append(types.Content(
                role=role,
                parts=[types.Part(text=text)]
            ))

    # ── Code Preflight: 代码任务自动预加载上下文 ──
    # 当 LLM 分类为 code_dev 组时，在 agent loop 开始前自动搜索
    # 用户消息中提到的标识符，把所有引用位置注入到上下文中。
    # 效果：模型在第一轮就看到完整的引用图谱，不需要自己去搜。
    _preflight_ctx: str | None = None
    if should_run_code_preflight(user_text, _llm_groups):
        try:
            _preflight_ctx = await _code_preflight_context(user_text, history)
        except Exception as e:
            logger.warning("code preflight failed: %s", e)
    if _preflight_ctx:
        contents.append(types.Content(
            role="user",
            parts=[types.Part(text=_preflight_ctx)],
        ))
        contents.append(types.Content(
            role="model",
            parts=[types.Part(text="好的，我已了解代码上下文。在修改前我会确认所有相关引用都已覆盖。")],
        ))

    # 用户消息（文本 + 多模态附件）
    user_parts: list[types.Part] = []
    display_text = f"[{sender_name}]: {user_text}" if sender_name else user_text
    user_parts.append(types.Part(text=display_text))

    _uploaded_files: list = []  # 用于结束后清理
    # 使用自定义 base_url（反代）时，File API 上传几乎必定超时，跳过以节省 60s
    _use_file_api = not http_options.get("base_url")

    # 保存用户图片为临时文件，供工具（如 xhs_publish）使用
    _user_image_paths: list[str] = []
    if image_urls:
        import tempfile
        for url in image_urls:
            mime_type, raw_bytes = _parse_data_url(url)
            if not raw_bytes:
                continue

            # 图片存为临时文件，供工具引用（如 xhs_publish 的 images 参数）
            if mime_type.startswith("image/"):
                ext = {
                    "image/png": ".png", "image/jpeg": ".jpg",
                    "image/gif": ".gif", "image/webp": ".webp",
                }.get(mime_type, ".png")
                with tempfile.NamedTemporaryFile(
                    suffix=ext, prefix="user_img_", delete=False,
                ) as f:
                    f.write(raw_bytes)
                    _user_image_paths.append(f.name)

            # 不支持 inline_data 的音频格式 → 用 ffmpeg 转为 OGG
            if mime_type == "audio/amr":
                from app.services.media_processor import convert_audio_to_ogg
                ogg_bytes = await convert_audio_to_ogg(raw_bytes)
                if ogg_bytes:
                    raw_bytes = ogg_bytes
                    mime_type = "audio/ogg"
                    logger.info("converted AMR → OGG (%dKB) for Gemini inline", len(ogg_bytes) // 1024)

            # 代理模式：大视频/音频压缩后再发（避免 payload 过大导致 500）
            if not _use_file_api:
                mime_type, raw_bytes = await _maybe_compress_inline(mime_type, raw_bytes)

            if (mime_type.startswith(("video/", "audio/")) or mime_type == "application/pdf") and _use_file_api:
                # 视频/音频：通过 File API 上传，让 Gemini 真正理解内容
                uploaded = await _upload_to_file_api(client, raw_bytes, mime_type)
                if uploaded:
                    _uploaded_files.append(uploaded)
                    user_parts.append(types.Part(
                        file_data=types.FileData(
                            file_uri=uploaded.uri,
                            mime_type=uploaded.mime_type,
                        )
                    ))
                    continue
                # File API 失败则回退到 inline_data
                logger.warning("file API failed, falling back to inline_data for %s", mime_type)
            elif mime_type.startswith(("video/", "audio/")) and not _use_file_api:
                logger.info("skipping file API (using proxy), inline_data for %s (%dKB)",
                            mime_type, len(raw_bytes) // 1024)

            user_parts.append(types.Part(
                inline_data=types.Blob(mime_type=mime_type, data=raw_bytes)
            ))

    # ── Payload 预检：inline_data 总量超过预算时逐个丢弃最大的媒体 ──
    _INLINE_BUDGET_BYTES = 8 * 1024 * 1024  # 8MB 总预算（代理模式）
    if not _use_file_api:
        media_parts = [
            (i, p) for i, p in enumerate(user_parts)
            if hasattr(p, "inline_data") and p.inline_data and p.inline_data.data
        ]
        total_inline = sum(len(p.inline_data.data) for _, p in media_parts)
        if total_inline > _INLINE_BUDGET_BYTES:
            logger.warning(
                "inline payload %dKB exceeds budget %dKB, trimming largest media",
                total_inline // 1024, _INLINE_BUDGET_BYTES // 1024,
            )
            # 按大小降序排列，逐个移除最大的直到总量在预算内
            media_parts.sort(key=lambda x: len(x[1].inline_data.data), reverse=True)
            drop_indices: set[int] = set()
            for idx, part in media_parts:
                if total_inline <= _INLINE_BUDGET_BYTES:
                    break
                total_inline -= len(part.inline_data.data)
                drop_indices.add(idx)
                logger.info("dropping inline media %dKB (%s) to fit budget",
                            len(part.inline_data.data) // 1024,
                            part.inline_data.mime_type)
            if drop_indices:
                user_parts = [p for i, p in enumerate(user_parts) if i not in drop_indices]
                # 告知模型有媒体被省略
                user_parts.append(types.Part(
                    text=f"[系统提示] 因媒体总量过大，{len(drop_indices)}个媒体文件被省略。请基于剩余媒体回复用户。"
                ))

    # 如果有用户图片保存为临时文件，在文本中附上路径提示
    if _user_image_paths:
        paths_str = ", ".join(_user_image_paths)
        user_parts.append(types.Part(
            text=f"[用户发送了 {len(_user_image_paths)} 张图片，"
                 f"本地路径: {paths_str}。"
                 f"如需将这些图片用于 xhs_publish 等工具，请将路径传入 images 参数。\n"
                 f"如需精确分析密集网格图（如 sprite sheet、icon atlas），"
                 f"可在自定义工具中使用 sandbox_caps 的 read_user_image(path) 读取文件 + "
                 f"slice_image_grid(data, rows, cols) 按行切片后逐行调 gemini_analyze_image 分析，"
                 f"精度远高于一次性分析整张图]"
        ))

    contents.append(types.Content(role="user", parts=user_parts))

    # ── 工具转换 ──
    gemini_decls = _openai_tools_to_gemini(openai_tools)
    # Gemini 3 系列推荐 temperature=1.0（默认值），低值可能导致 looping
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[types.Tool(function_declarations=gemini_decls)],
        temperature=1.0,
        max_output_tokens=32768,
        thinking_config=types.ThinkingConfig(include_thoughts=False),
    )
    _progress_count = 0
    _MAX_PROGRESS_MSGS = 2
    _loop_start = time.monotonic()
    _last_progress_at = _loop_start
    # _task_type 已在 LLM intent classification 阶段确定（无 budget/stall 限制）

    # ── Sub-Agent 委托：复杂任务交给隔离的子 agent 执行 ──
    # 纯文本任务（无多模态附件）且匹配子 agent 类型 → 委托执行
    # 子 agent 有独立上下文、独立预算，工具调用不污染主 agent
    _sub_agent_type: str | None = None
    if not image_urls:
        _sub_agent_type = should_delegate_to_sub_agent(_task_type, user_text, _llm_groups)

    if _sub_agent_type:
        if _sub_agent_type:
            logger.info("delegating to sub-agent [%s] for task type '%s'",
                        _sub_agent_type, _task_type)
            try:
                sub_result = await _run_sub_agent(
                    client=client,
                    model_name=model_name,
                    strong_model=strong_model,
                    sub_agent_type=_sub_agent_type,
                    user_text=user_text,
                    parent_system_prompt=system_prompt,
                    tenant=tenant,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    on_progress=on_progress,
                    history=history,
                )
                if sub_result and len(sub_result.strip()) > 20:
                    # 子 agent 返回了有效结果
                    # ── 检查 inbox：sub-agent 运行期间用户可能发了新消息 ──
                    if inbox is not None:
                        pending = _drain_inbox(inbox)
                        if pending:
                            # 有未处理消息 → 不直接返回，把 sub-agent 结果 + 新消息
                            # 一起送进主 loop 继续处理
                            inbox_texts = []
                            for item in pending:
                                t = item.get("text", "") if isinstance(item, dict) else str(item)
                                if t:
                                    inbox_texts.append(t)
                            logger.info(
                                "sub-agent done but inbox has %d messages, "
                                "falling through to main loop: %s",
                                len(pending),
                                "; ".join(t[:40] for t in inbox_texts),
                            )
                            # 把 sub-agent 结果作为前置上下文，inbox 消息作为新输入
                            user_text = (
                                f"[前置任务已完成]\n{sub_result}\n\n"
                                f"[用户新消息]\n" + "\n".join(inbox_texts)
                            )
                            # 继续到主 loop 处理
                        else:
                            _trigger_memory(
                                sender_id, sender_name, user_text, sub_result,
                                [],
                            )
                            return sub_result
                    else:
                        _trigger_memory(
                            sender_id, sender_name, user_text, sub_result,
                            [],
                        )
                        return sub_result
                # 子 agent 结果太短/为空 → fallback 到主 loop
                logger.warning(
                    "sub-agent [%s] returned insufficient result (%d chars), "
                    "falling back to main loop",
                    _sub_agent_type, len(sub_result) if sub_result else 0,
                )
            except Exception:
                logger.warning(
                    "sub-agent [%s] failed, falling back to main loop",
                    _sub_agent_type, exc_info=True,
                )

    call_log: list[str] = []
    tool_names_called: list[str] = []
    action_outcomes: list[tuple[str, str]] = []  # (tool_name, outcome_summary)
    # URL 溯源：收集所有工具返回中的 URL，用于验证 LLM 写操作参数中的 URL 真实性
    _seen_urls: set[str] = extract_urls(user_text)  # 用户消息中的 URL 也算已见
    _blocked_urls: set[str] = set()  # 已被拦截过的 URL（死循环保护：拦截 2 次后放行）
    # 从对话历史中提取 URL（用户之前分享的链接、之前工具返回的链接）
    if history:
        for msg in history:
            if isinstance(msg, dict) and msg.get("content"):
                _seen_urls.update(extract_urls(str(msg["content"])))
    _escalated = False  # 标记是否已提前升级到强模型
    # ── Hybrid Model 路由（GTC AI-Q 模式）──
    # 子 agent 已经委托走了 → Flash 足够（子 agent 自己有 Pro 升级逻辑）
    # 主 agent 处理多域任务（未委托）→ 提前用 Pro（编排决策需要更强推理）
    if (not _sub_agent_type and _task_type in ("deep", "normal")
            and _llm_groups and len(_llm_groups - {"core"}) >= 2
            and strong_model and strong_model != model_name):
        logger.info("hybrid routing: multi-domain task (%s), starting with Pro for orchestration",
                    _llm_groups)
        _escalated = True  # 直接用 Pro，不等 round 6
    _nudged = False  # 标记是否已追加过 unfulfilled promise 催促
    _deliverable_nudge_count = 0  # 交付物催促计数（允许最多 2 次）
    _MAX_DELIVERABLE_NUDGES = 2
    _exit_gate_nudge_count = 0  # LLM exit gate 催促计数（允许最多 2 次）
    _MAX_EXIT_GATE_NUDGES = 3
    _assertion_nudge_count = 0  # P2: 事实断言防幻觉守卫计数（最多 1 次）
    _custom_tool_thrashing_nudged = False  # P4: 自定义工具 thrashing 检测（最多 nudge 1 次）
    _empty_content_retries = 0  # 空响应重试计数（最多 3 次后用事实性总结退出）
    _pro_failures = 0  # 强模型连续 fallback 次数（熔断器）
    _PRO_CIRCUIT_BREAKER = 2  # 连续 fallback 2 次后禁用强模型
    _total_input_tokens = 0   # 跨轮次累计 input tokens
    _total_output_tokens = 0  # 跨轮次累计 output tokens

    # 触发立即升级的工具：涉及代码修改、部署、复杂推理
    _ESCALATION_TOOLS = frozenset({
        "self_write_file", "self_edit_file", "self_safe_deploy", "self_rollback",
        "bash_execute", "anthropic_code",
        "github_create_pr", "github_push_files",
        "create_plan",
    })

    for round_num in range(_MAX_ROUNDS):
        # 模型升级策略：
        # 1. 主动升级：首轮发现 3+ 并行工具调用或重活工具 → 立即切强模型
        # 2. 被动升级：跑满 6 轮说明任务确实复杂 → 切强模型
        # 3. 熔断：强模型连续 N 次 503 fallback → 本次对话不再尝试
        current_model = model_name
        _pro_ok = (strong_model and strong_model != model_name
                   and _pro_failures < _PRO_CIRCUIT_BREAKER)
        if _pro_ok and (_escalated or round_num >= 6):
            current_model = strong_model

        logger.info("gemini agent round %d (model=%s)", round_num + 1, current_model)

        # 连接错误 / 服务端错误自动重试（应对间歇性 DNS 污染 / 网络抖动 / 500）
        # 如果当前用的是升级后的强模型，重试耗尽后 fallback 回基础模型再试一次
        _max_retries = 2
        _attempt = 0
        _model_for_call = current_model
        while _attempt <= _max_retries:
            try:
                async with _api_semaphore:
                    # asyncio 级别超时兜底（独立于 SDK 的 HTTP timeout）
                    # 防止 CF Worker 代理导致 SDK timeout 不生效
                    _call_timeout = 90 if _model_for_call == model_name else 120
                    response = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=_model_for_call,
                            contents=contents,
                            config=config,
                        ),
                        timeout=_call_timeout,
                    )
                if _model_for_call != current_model:
                    # fallback 成功，后续轮次也用基础模型
                    current_model = _model_for_call
                elif _model_for_call == strong_model and _pro_failures > 0:
                    # pro 成功了，重置熔断计数
                    _pro_failures = 0
                # 累计 token 用量（每轮 API 调用后刷新）
                try:
                    _um = getattr(response, "usage_metadata", None)
                    if _um:
                        _total_input_tokens += getattr(_um, "prompt_token_count", 0) or 0
                        _total_output_tokens += getattr(_um, "candidates_token_count", 0) or 0
                        from app.services.metering import last_usage_tokens
                        last_usage_tokens.set((_total_input_tokens, _total_output_tokens))
                except Exception:
                    pass
                break  # 成功则跳出重试循环
            except asyncio.TimeoutError:
                logger.warning("generate_content asyncio timeout (%ds) model=%s round=%d attempt=%d",
                               _call_timeout, _model_for_call, round_num + 1, _attempt + 1)
                # 强模型超时 → fallback 到基础模型
                if _model_for_call != model_name:
                    _pro_failures += 1
                    logger.warning("strong model timeout, falling back to %s", model_name)
                    _model_for_call = model_name
                    _attempt = 0
                    continue
                if _attempt < _max_retries:
                    _attempt += 1
                    continue
                if tool_names_called:
                    return _build_factual_summary(
                        tool_names_called, action_outcomes,
                        "AI 服务响应超时，但之前的操作已完成：",
                    )
                return "AI 服务响应超时，请稍后再试。"
            except (ConnectionError, OSError) as exc:
                if _attempt < _max_retries:
                    wait = 2 ** (_attempt + 1)
                    logger.warning("Gemini connection error (attempt %d/%d), retry in %ds: %s",
                                   _attempt + 1, _max_retries + 1, wait, exc)
                    await asyncio.sleep(wait)
                    _attempt += 1
                    continue
                # 强模型连接失败 → fallback 回基础模型重试
                if _model_for_call != model_name:
                    _pro_failures += 1
                    logger.warning("strong model %s connection failed after %d attempts, "
                                   "falling back to %s (round %d, pro_failures=%d/%d)",
                                   _model_for_call, _max_retries + 1, model_name,
                                   round_num + 1, _pro_failures, _PRO_CIRCUIT_BREAKER)
                    _model_for_call = model_name
                    _attempt = 0
                    continue
                logger.exception("Gemini API call failed after %d attempts (round %d)",
                                 _max_retries + 1, round_num + 1)
                if tool_names_called:
                    return _build_factual_summary(
                        tool_names_called, action_outcomes,
                        "AI 服务连接失败，但之前的操作已完成：",
                    )
                return "AI 服务连接失败（可能是网络问题或 Gemini API 暂时不可用），请稍后再试。"
            except Exception as exc:
                exc_msg = str(exc).lower()
                # 判断是否为服务端/代理层可重试错误：
                # - 500/503/504 HTTP 状态码
                # - 499 CANCELLED（gRPC 请求被取消，通常是超时或连接断开）
                # - Cloudflare Worker 代理超时（gateway time-out）
                # - google.genai.errors.ServerError 异常类
                _server_keywords = ("500", "503", "504", "499", "overloaded", "unavailable",
                                    "internal", "timeout", "gateway", "timed out", "cancelled")
                _is_server_error = (
                    any(kw in exc_msg for kw in _server_keywords)
                    or type(exc).__name__ == "ServerError"
                )
                # 服务端错误（500/503/504）可重试
                if _attempt < _max_retries and _is_server_error:
                    wait = 2 ** (_attempt + 1)
                    logger.warning("Gemini server error (attempt %d/%d), retry in %ds: %s",
                                   _attempt + 1, _max_retries + 1, wait, exc)
                    await asyncio.sleep(wait)
                    _attempt += 1
                    continue
                # 强模型服务端错误重试耗尽 → fallback 回基础模型继续
                if _is_server_error and _model_for_call != model_name:
                    _pro_failures += 1
                    logger.warning("strong model %s server error after %d attempts, "
                                   "falling back to %s (round %d, pro_failures=%d/%d): %s",
                                   _model_for_call, _max_retries + 1, model_name,
                                   round_num + 1, _pro_failures, _PRO_CIRCUIT_BREAKER, exc)
                    if _pro_failures >= _PRO_CIRCUIT_BREAKER:
                        logger.warning("circuit breaker: disabling %s for rest of conversation",
                                       strong_model)
                    _model_for_call = model_name
                    _attempt = 0
                    continue
                # 特定错误给出更明确的提示
                if "output" in exc_msg and "token" in exc_msg:
                    logger.warning("Gemini output token limit exceeded (round %d): %s", round_num + 1, exc)
                    return "AI 输出内容超过长度限制。请尝试把任务拆小，分步完成。"
                if "quota" in exc_msg or "rate" in exc_msg:
                    logger.warning("Gemini rate/quota limit (round %d): %s", round_num + 1, exc)
                    return "AI API 配额不足或请求频繁，请稍后再试。"
                if "safety" in exc_msg or "blocked" in exc_msg:
                    return "该内容被安全过滤器拦截，请换个方式描述你的需求。"
                # 服务端错误（重试+fallback 都耗尽）给出简洁提示，不泄露 HTML
                if _is_server_error:
                    logger.error("Gemini server error exhausted all retries (round %d): %s",
                                 round_num + 1, exc)
                    # 区分超时和其他服务端错误
                    if "timeout" in exc_msg or "timed out" in exc_msg or "gateway" in exc_msg or "cancelled" in exc_msg:
                        if tool_names_called:
                            return _build_factual_summary(
                                tool_names_called, action_outcomes,
                                "AI 服务响应超时，但之前的操作已完成：",
                            )
                        return "AI 服务响应超时（可能是网络波动或 Gemini 服务繁忙），请稍后再试。"
                    if tool_names_called:
                        return _build_factual_summary(
                            tool_names_called, action_outcomes,
                            "AI 服务暂时不可用，但之前的操作已完成：",
                        )
                    return "AI 服务暂时不可用，请稍后再试。"
                logger.exception("Gemini API call failed (round %d)", round_num + 1)
                # 不向用户暴露原始错误细节
                return "AI 调用出了点问题，请稍后再试。如果持续出现，请联系管理员~"

        if not response.candidates:
            logger.error("Gemini returned empty candidates (round %d)", round_num + 1)
            if tool_names_called:
                # 交付物检查：模型返回空但还有未生成的文件 → 催促继续
                if _deliverable_nudge_count < _MAX_DELIVERABLE_NUDGES:
                    _missing = check_unfulfilled_deliverables(user_text, tool_names_called)
                    if _missing:
                        _deliverable_nudge_count += 1
                        logger.info("empty candidates + unfulfilled deliverables %s, nudging", _missing)
                        contents.append(types.Content(
                            role="user",
                            parts=[types.Part(text=(
                                f"你还没有生成用户要求的{'、'.join(_missing)}。"
                                "请立即调用 export_file 或对应工具完成文件生成，不要跳过。"
                            ))],
                        ))
                        continue
                logger.info("empty candidates but %d tools were called, building factual summary",
                            len(tool_names_called))
                reply = _build_factual_summary(tool_names_called, action_outcomes, "AI 返回了空结果。")
                _trigger_memory(sender_id, sender_name, user_text, reply, tool_names_called, call_log, action_outcomes)
                _maybe_record_watchdog(user_text, tool_names_called, sender_id, sender_name, chat_id, chat_type, reply)
                return reply
            return "AI 返回了空结果，请稍后再试。"

        candidate = response.candidates[0]

        # 安全过滤检查
        if candidate.finish_reason and candidate.finish_reason.name == "SAFETY":
            return "该内容被安全过滤器拦截，请换个方式描述你的需求。"

        content_obj = candidate.content
        if not content_obj or not content_obj.parts:
            if tool_names_called:
                # ── 空响应 nudge：模型返回了空 content，但之前调过工具 ──
                # 这通常意味着模型"想说什么但没说出来"，不应直接退出。
                # 给模型最多 3 次机会继续（保留工具能力），超过用事实性总结退出。
                _empty_content_retries += 1
                if _empty_content_retries <= 3:
                    # 交付物检查优先
                    if _deliverable_nudge_count < _MAX_DELIVERABLE_NUDGES:
                        _missing = check_unfulfilled_deliverables(user_text, tool_names_called)
                        if _missing:
                            _deliverable_nudge_count += 1
                            logger.info("empty content + unfulfilled deliverables %s, nudging", _missing)
                            contents.append(types.Content(
                                role="user",
                                parts=[types.Part(text=(
                                    f"你还没有生成用户要求的{'、'.join(_missing)}。"
                                    "请立即调用 export_file 或对应工具完成文件生成，不要跳过。"
                                ))],
                            ))
                            continue
                    # 通用 nudge：催模型继续执行
                    logger.info(
                        "empty content parts at round %d (retry %d/3, %d tools called), nudging to continue",
                        round_num + 1, _empty_content_retries, len(tool_names_called),
                    )
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            "你的回复为空。请继续执行任务——"
                            "如果还有操作要做，直接调用工具；如果已全部完成，用文字告诉用户结果。"
                            "不要只是承诺要做，直接做。"
                        ))],
                    ))
                    continue
                # 3 次空响应都没恢复 → 用事实性总结，不问 LLM（避免幻觉）
                logger.info("empty content parts after %d retries (%d tools called), building factual summary",
                            _empty_content_retries, len(tool_names_called))
                reply = _build_factual_summary(tool_names_called, action_outcomes, "AI 连续返回空响应。")
                _trigger_memory(sender_id, sender_name, user_text, reply, tool_names_called, call_log, action_outcomes)
                _maybe_record_watchdog(user_text, tool_names_called, sender_id, sender_name, chat_id, chat_type, reply)
                return reply
            return "AI 返回了空结果，请稍后再试。"

        # 分离 function_call 和文本（跳过 thinking parts，避免内心独白泄露）
        function_calls = []
        text_parts = []
        for part in content_obj.parts:
            if part.function_call:
                function_calls.append(part)
            elif part.text and not getattr(part, 'thought', False):
                text_parts.append(part.text)

        # 没有工具调用 → 准备退出循环
        if not function_calls:
            reply_text = "\n".join(text_parts).strip()

            # ── 退出前检查 1: inbox 里有未处理消息 ──
            # 用户在本轮 API 调用期间发了新消息，不能丢掉
            if inbox is not None:
                pending = _drain_inbox(inbox)
                if pending:
                    inbox_parts = []
                    for inbox_item in pending:
                        msg_text, msg_images = normalize_inbox_item(inbox_item)
                        logger.info(
                            "inbox drain before exit: %s (images=%d)",
                            msg_text[:60], len(msg_images) if msg_images else 0,
                        )
                        if msg_text:
                            inbox_parts.append(types.Part(text=f"[用户新消息] {msg_text}"))
                        if msg_images:
                            for url in msg_images:
                                mt, rb = _parse_data_url(url)
                                if rb:
                                    if not _use_file_api:
                                        mt, rb = await _maybe_compress_inline(mt, rb)
                                    inbox_parts.append(types.Part(
                                        inline_data=types.Blob(mime_type=mt, data=rb)
                                    ))
                    if inbox_parts:
                        # 把模型的文本回复和用户新消息都加入 contents，继续循环
                        contents.append(content_obj)
                        contents.append(types.Content(role="user", parts=inbox_parts))
                        logger.info("continuing loop: %d inbox messages pending", len(pending))
                        continue

            # ── 退出前检查 2: 读了数据但没写回 ──
            # 结构性判断：调了 read_feishu_doc 但没调 edit/update/write_feishu_doc
            # 说明模型读了文档准备改，却生成了纯文本就想退出
            # 跳过条件：涉及图片分析/自定义工具调试时，读操作是分析流程的一部分
            _vision_analysis_in_progress = bool(
                {"test_custom_tool", "assess_capability"} & set(tool_names_called)
            )
            if (
                not _vision_analysis_in_progress
                and should_nudge_unmatched_reads(
                    round_num=round_num,
                    already_nudged=_nudged,
                    tool_names_called=tool_names_called,
                    user_text=user_text,
                    has_unmatched_reads=_has_unmatched_reads,
                )
            ):
                _nudged = True
                logger.info(
                    "read-without-write at round %d (tools: %s), nudging",
                    round_num + 1, tool_names_called,
                )
                contents.append(content_obj)
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=(
                        "你已经读取了内容但还没有执行修改。"
                        "请调用对应的写入工具（如 write_file）完成操作，不要只是描述你会怎么改。"
                    ))],
                ))
                continue

            # ── 退出前检查 3: 用户要的文件没生成 ──
            # 用户说"生成 PDF 和 PPT"但 export_file 从未被调用 → 催模型继续
            if _deliverable_nudge_count < _MAX_DELIVERABLE_NUDGES and round_num >= 1:
                _missing = check_unfulfilled_deliverables(user_text, tool_names_called)
                if _missing:
                    _deliverable_nudge_count += 1
                    logger.info(
                        "text-exit but unfulfilled deliverables %s at round %d, nudging",
                        _missing, round_num + 1,
                    )
                    contents.append(content_obj)
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            f"你还没有生成用户要求的{'、'.join(_missing)}。"
                            "请立即调用 export_file 或对应工具完成文件生成，不要跳过这一步。"
                        ))],
                    ))
                    continue

            # ── 退出前检查 4a: 快速本地检测（无 LLM 调用，instant）──
            # 正则匹配"已经删了/加了/发了"等动作声称，对比实际工具调用
            # 涉及视觉分析/自定义工具调试时，放宽到 round 4+ 再检测（这类任务天然需要更多轮）
            _exit_gate_min_round = 4 if _vision_analysis_in_progress else 1
            if _exit_gate_nudge_count < _MAX_EXIT_GATE_NUDGES and reply_text and round_num >= _exit_gate_min_round:
                if detect_action_claims(
                    reply_text,
                    tool_names_called,
                    user_text=user_text,
                    action_outcomes=action_outcomes,
                ):
                    _exit_gate_nudge_count += 1
                    logger.info(
                        "local action-claim detector nudge #%d at round %d (reply: %s)",
                        _exit_gate_nudge_count, round_num + 1, reply_text[:80],
                    )
                    contents.append(content_obj)
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            "⛔ 你的回复声称已经执行了操作，但实际上没有调用任何工具。"
                            "如果当前用户这轮明确是在要你执行某件事，请立即调用对应工具完成操作。"
                            "如果当前用户这轮是在问解释、分析、总结、观点或资料，不要跳去执行旧任务，继续围绕当前问题回答。"
                            "不要用文字描述你做了什么——只在当前问题确实要求行动时再调用工具。"
                            "如果之前的操作失败了，请重新尝试。"
                        ))],
                    ))
                    continue

            # ── 退出前检查 4a2: Grounding gate（事实验证关卡）──
            # 检测"回复了事实但没搜过" → 强制打回重搜
            # 只在第一轮检测（防止后续轮重复 nudge），且只 nudge 一次
            if _exit_gate_nudge_count < _MAX_EXIT_GATE_NUDGES and reply_text and round_num <= 1:
                _grounding_nudge = detect_ungrounded_claims(
                    reply_text, user_text, tool_names_called,
                )
                if _grounding_nudge:
                    _exit_gate_nudge_count += 1
                    logger.info(
                        "grounding gate nudge at round %d (reply: %s)",
                        round_num + 1, reply_text[:80],
                    )
                    contents.append(content_obj)
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text=_grounding_nudge)],
                    ))
                    continue

            # ── 退出前检查 4b: LLM exit gate（兜底，处理本地检测不了的复杂情况）──
            # fail-OPEN：超时时放行（本地 detect_action_claims 已做了第一道检查）
            # ⚠️ 关键守卫：模型已调 ≥3 工具时跳过 LLM exit review。
            # 原因：模型在积极工作后给出的文本回复大概率是中间汇报/结果报告，
            # 而非空承诺。LLM reviewer 无法区分人格化语气（"我错了是我傻逼"）
            # 和真正的空承诺，误判会打断正常任务流导致模型迷失方向。
            # 本地 detect_action_claims 已覆盖"声称做了但没做"的场景，
            # LLM gate 只需在零工具调用时兜底。
            _skip_llm_gate = len(tool_names_called) >= 3
            if _skip_llm_gate:
                logger.debug(
                    "skipping LLM exit review: %d tools already called",
                    len(tool_names_called),
                )
            if (
                not _skip_llm_gate
                and _exit_gate_nudge_count < _MAX_EXIT_GATE_NUDGES
                and reply_text
                and round_num >= _exit_gate_min_round
            ):
                _gate = await llm_exit_review(
                    reply_text, user_text, tool_names_called,
                    gemini_client=client,
                )
                if _gate == "nudge":
                    _exit_gate_nudge_count += 1
                    logger.info(
                        "exit gate nudge #%d at round %d (reply: %s)",
                        _exit_gate_nudge_count, round_num + 1, reply_text[:80],
                    )
                    contents.append(content_obj)
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            "你说了要执行操作但实际上没有调用任何工具。"
                            "请立即调用对应的工具完成操作，不要只是说你会做——直接做。"
                        ))],
                    ))
                    continue
                if _gate == "grounding":
                    _exit_gate_nudge_count += 1
                    logger.info(
                        "exit gate grounding nudge #%d at round %d (reply: %s)",
                        _exit_gate_nudge_count, round_num + 1, reply_text[:80],
                    )
                    contents.append(content_obj)
                    contents.append(types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            "⚠️ 你的回复包含具体的事实信息（人名、公司、数据等），"
                            "但你没有调用任何搜索工具来验证。你的知识可能过时或错误。"
                            "请先用 web_search 搜索确认，然后基于搜索结果回答。"
                        ))],
                    ))
                    continue

            # ── P2: 事实断言防幻觉守卫（架构层） ──
            # 当模型声称"你之前说的是X"但从未调用 fetch_chat_history 验证时，
            # 强制打回让它先查聊天记录，避免编造用户未说过的话。
            # 仅在用户质疑（"不对"/"错了"/"日期不对"等）时触发，最多 nudge 1 次。
            if (
                reply_text
                and _assertion_nudge_count < 1
                and re.search(r"(你之前说|你说过|你提到过|你之前提到|你说的是|你原话)", reply_text)
                and "fetch_chat_history" not in tool_names_called
                and re.search(r"(不对|错了|不是|搞错|弄错|日期不对|时间不对|wrong)", user_text, re.IGNORECASE)
            ):
                _assertion_nudge_count += 1
                logger.info(
                    "P2 hallucination guard: reply asserts user's words without checking history, nudging (reply: %s)",
                    reply_text[:80],
                )
                contents.append(content_obj)
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=(
                        "⛔ 你声称用户之前说了某句话，但你没有用 fetch_chat_history 验证。"
                        "在断言用户说过什么之前，必须先调用 fetch_chat_history 查看原始对话记录。"
                        "不要凭记忆回忆用户的原话——记忆可能出错。请先查记录再回复。"
                    ))],
                ))
                continue

            if reply_text:
                reply = _strip_hallucinated_code_blocks(_strip_degenerate_repetition(reply_text))
            else:
                # 模型返回空文本（如全是 thinking parts 被过滤）→ 事实性总结，不再问 LLM
                reply = _build_factual_summary(tool_names_called, action_outcomes)
            _trigger_memory(sender_id, sender_name, user_text, reply, tool_names_called, call_log, action_outcomes)
            _maybe_record_watchdog(user_text, tool_names_called, sender_id, sender_name, chat_id, chat_type, reply)
            return reply

        # ── 智能进度通知 ──
        # 规则：第一次 30 秒+3 轮后发，之后每 60 秒最多再发一次，总共最多 2 条
        # ⚠️ 绝不用模型的中间文本做进度消息！模型可能输出 thinking/reasoning
        # 内容（如 "thought:thought: ..."），泄露给用户极其糟糕。
        # 用小模型单独生成进度消息，LLM 失败则不发（不用硬编码 fallback）。
        if on_progress and _progress_count < _MAX_PROGRESS_MSGS:
            now = time.monotonic()
            _elapsed = now - _loop_start
            _since_last = now - _last_progress_at
            send_progress = False
            if _progress_count == 0 and round_num >= 3 and _elapsed > 30:
                send_progress = True
            elif _progress_count > 0 and _since_last > 60:
                send_progress = True
            if send_progress:
                logger.info("progress hint: attempting (round=%d, elapsed=%.0fs, count=%d)",
                           round_num + 1, _elapsed, _progress_count)
                msg = await _generate_progress_hint(
                    tool_names_called, _progress_count,
                    gemini_client=client,
                    user_text=user_text,
                )
                if msg:
                    try:
                        await on_progress(msg)
                        _progress_count += 1
                        _last_progress_at = now
                        logger.info("progress hint: sent '%s'", msg)
                    except Exception:
                        logger.warning("on_progress callback failed", exc_info=True)
                else:
                    logger.info("progress hint: LLM returned None (generation failed or text invalid)")

        proposed_tool_names = [fc.function_call.name for fc in function_calls]
        settle_nudge = build_tool_settle_nudge(
            user_text,
            proposed_tool_names,
            action_outcomes,
        )
        if settle_nudge:
            logger.info(
                "tool escalation settle nudge at round %d (proposed=%s)",
                round_num + 1,
                proposed_tool_names,
            )
            contents.append(content_obj)
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text=settle_nudge)],
            ))
            continue

        # 将 model 响应加入 contents
        contents.append(content_obj)

        # ── 执行工具调用 ──
        response_parts: list[types.Part] = []

        for fc_part in function_calls:
            fc = fc_part.function_call
            func_name = fc.name
            func_args = dict(fc.args) if fc.args else {}

            # 自定义工具元操作：自动注入 tenant_id
            if func_name in _CUSTOM_TOOL_META_NAMES and "tenant_id" not in func_args:
                func_args["tenant_id"] = tenant.tenant_id

            # 记忆工具：自动注入 sender 上下文（LLM 不知道当前用户的 open_id）
            if func_name in ("save_memory", "recall_memory"):
                if not func_args.get("user_id"):
                    func_args["user_id"] = sender_id
                if not func_args.get("user_name"):
                    func_args["user_name"] = sender_name

            call_key = f"{func_name}({json.dumps(func_args, ensure_ascii=False)})"
            call_log.append(call_key)

            logger.info("tool call: %s(%s)", func_name, func_args)
            if func_name != "think":
                tool_names_called.append(func_name)

            # ── 硬限制：create_custom_tool 每对话最多 1 次 ──
            if func_name == "create_custom_tool":
                _custom_tool_creates = sum(1 for n in tool_names_called if n == "create_custom_tool")
                if _custom_tool_creates > 1:
                    result = ToolResult.error(
                        "每次对话最多创建 1 个自定义工具。"
                        "请先用好已有的工具（search_logs、export_file 等），"
                        "不要重复造轮子。",
                        code="rate_limited",
                    )
                    response_parts.append(types.Part(
                        function_response=types.FunctionResponse(
                            name=func_name,
                            response={"result": result.content},
                        )
                    ))
                    continue

            # ── 硬限制：provision_tenant 等敏感工具需超管身份 ──
            if func_name in _SUPER_ADMIN_ONLY_TOOLS:
                from app.tenant.context import get_current_sender
                _sender = get_current_sender()
                if not _sender.is_super_admin:
                    result = ToolResult.error(
                        f"该操作需要管理员权限。"
                        f"如需开通 bot 实例，请用 request_provision 提交申请。",
                        code="permission",
                    )
                    response_parts.append(types.Part(
                        function_response=types.FunctionResponse(
                            name=func_name,
                            response={"result": result.content},
                        )
                    ))
                    continue

            # ── 特殊处理：request_more_tools 动态扩展工具集 ──
            if func_name == "request_more_tools":
                group_name = func_args.get("group", "")
                reason = func_args.get("reason", "")
                logger.info("request_more_tools: group=%s reason=%s", group_name, reason)
                new_oai_tools, new_map = _expand_tool_group(
                    group_name, tenant, _loaded_tool_names,
                    _from_request_more_tools=True,
                )
                if new_oai_tools:
                    tool_map.update(new_map)
                    new_gemini = _openai_tools_to_gemini(new_oai_tools)
                    gemini_decls.extend(new_gemini)
                    new_names = [t["function"]["name"] for t in new_oai_tools]
                    _loaded_tool_names.update(new_names)
                    # 重建 config 使新工具在下一轮可用
                    config = types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=[types.Tool(function_declarations=gemini_decls)],
                        temperature=1.0,
                        max_output_tokens=8192,
                        thinking_config=types.ThinkingConfig(include_thoughts=False),
                    )
                    result_str = (
                        f"已加载工具组 [{group_name}]（{_GROUP_DESCRIPTIONS.get(group_name, '')}），"
                        f"新增 {len(new_names)} 个工具：{', '.join(new_names)}。"
                        f"你现在可以使用这些工具了。"
                    )
                    logger.info("expanded tools: +%d from group %s, total declarations: %d",
                                len(new_names), group_name, len(gemini_decls))
                else:
                    result_str = f"工具组 [{group_name}] 中的工具已全部加载，无需额外操作。"
                response_parts.append(types.Part(
                    function_response=types.FunctionResponse(
                        name=func_name,
                        response={"result": result_str},
                    )
                ))
                tool_names_called.append(func_name)
                call_log.append(f"request_more_tools({group_name})")
                continue

            handler = tool_map.get(func_name)
            if handler is None:
                result = ToolResult.error(f"unknown tool: {func_name}", code="not_found")
            else:
                # ── URL 溯源验证：写操作中的 URL 必须来自已见数据 ──
                _url_warning, _flagged = check_url_provenance(
                    func_name, func_args, _seen_urls, _blocked_urls,
                )
                if _url_warning:
                    logger.warning("URL provenance check failed for %s: %s",
                                   func_name, _url_warning[:200])
                    _blocked_urls.update(_flagged)  # 记录本次拦截的 URL
                    result = ToolResult.error(_url_warning, code="url_hallucination")
                    # 不执行工具，让 LLM 修正参数
                elif (_intent_block := check_write_intent(
                    func_name, func_args, user_text, tool_names_called,
                )):
                    logger.warning("write intent blocked: %s", _intent_block[:200])
                    result = ToolResult.error(_intent_block, code="write_intent_blocked")
                elif (risk := _get_custom_tool_risk(tenant.tenant_id, func_name)) in ("yellow", "red"):
                    risk_hint = "写操作" if risk == "yellow" else "批量/高风险操作"
                    result = ToolResult.blocked(
                        f"这是一个{risk_hint}工具（{risk}级别）。"
                        f"请告诉用户你要执行什么操作，等确认后再调用。"
                    )
                else:
                    try:
                        result = handler(func_args)
                        # 支持 async 工具（如 analyze_video_url）
                        if asyncio.iscoroutine(result):
                            result = await result
                    except Exception as exc:
                        logger.exception("tool %s failed", func_name)
                        result = ToolResult.error(str(exc), code="internal")
                        record_error("tool_exception", f"{func_name} 异常", exc=exc,
                                     tool_name=func_name, tool_args=func_args)

            # 记录工具执行进度（用于超时时构造智能消息）
            record_agent_progress(func_name)

            # 统一处理结果
            if isinstance(result, ToolResult):
                if not result.ok:
                    record_error(
                        "tool_error",
                        f"{func_name} → [{result.code}] {result.content[:300]}",
                        tool_name=func_name, tool_args=func_args,
                    )
                result_str = result.content
            elif isinstance(result, dict) and "image_refs" in result:
                result_str = result.get("text", "")
            elif isinstance(result, str):
                if "[ERROR]" in result:
                    record_error("tool_error", f"{func_name} → {result[:300]}",
                                 tool_name=func_name, tool_args=func_args)
                result_str = result
            else:
                result_str = str(result)

            # URL 溯源：在截断前从完整数据中提取所有 URL（截断后的 URL 仍可用于溯源验证）
            _seen_urls.update(extract_urls(result_str))

            if len(result_str) > _MAX_TOOL_RESULT_LEN:
                result_str = (
                    result_str[:_MAX_TOOL_RESULT_LEN]
                    + f"\n\n... (截断，原文 {len(result_str)} 字符)"
                )

            response_parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=func_name,
                    response={"result": result_str},
                )
            ))

            # 工具性能追踪（经验沉淀：成功/失败率 + 错误模式）
            _tool_ok = not (
                (isinstance(result, ToolResult) and not result.ok)
                or (isinstance(result, str) and "[ERROR]" in result)
            )
            _tool_err = ""
            if not _tool_ok:
                _tool_err = result_str[:200] if result_str else ""
            record_tool_call(tenant.tenant_id, func_name, _tool_ok, error_msg=_tool_err)
            record_tool_sequence(tenant.tenant_id, func_name)

            # 提取关键结果摘要（供下一轮对话历史使用）
            try:
                outcome = _extract_outcome(func_name, result_str, func_args)
                action_outcomes.append((func_name, outcome))
            except Exception:
                action_outcomes.append((func_name, "→ 完成"))

            # 记录 bot 行动日记（只记写操作，读操作跳过）
            try:
                from app.services import memory as bot_memory
                bot_memory.note_tool_action(func_name, func_args, result_str, sender_id, sender_name)
            except Exception:
                pass

        # ── 检查信箱：处理过程中用户发的新消息 ──
        if inbox is not None:
            for inbox_item in _drain_inbox(inbox):
                msg_text, msg_images = normalize_inbox_item(inbox_item)
                logger.info(
                    "inbox inject: %s (images=%d)",
                    msg_text[:60], len(msg_images) if msg_images else 0,
                )
                if msg_text:
                    response_parts.append(
                        types.Part(text=f"[用户新消息] {msg_text}")
                    )
                if msg_images:
                    for url in msg_images:
                        mt, rb = _parse_data_url(url)
                        if rb:
                            if not _use_file_api:
                                mt, rb = await _maybe_compress_inline(mt, rb)
                            response_parts.append(types.Part(
                                inline_data=types.Blob(mime_type=mt, data=rb)
                            ))

        # 将 function response 加入 contents（与 model 交替，保持 user/model 节奏）
        contents.append(types.Content(role="user", parts=response_parts))

        # ── P4: 自定义工具 thrashing 检测 ──
        # 连续 3+ 次 test_custom_tool/create_custom_tool 调用（多为失败重试），
        # 说明 LLM 在用错误的方式解决问题，注入 nudge 引导回到正轨。
        # 最多注入 1 次，避免反复打断。
        if not _custom_tool_thrashing_nudged:
            _recent_custom = [n for n in tool_names_called[-6:] if n in ("test_custom_tool", "create_custom_tool")]
            if len(_recent_custom) >= 3:
                _custom_tool_thrashing_nudged = True
                logger.info("P4: custom tool thrashing detected (%d recent), injecting nudge", len(_recent_custom))
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(text=(
                        "⚠️ 你已连续多次创建/测试自定义工具但都失败了。停下来换个思路：\n"
                        "1. 先用 fetch_chat_history 查看用户原始消息，确认需求\n"
                        "2. 检查已有工具列表，看有没有直接能用的（如 search_logs、export_file）\n"
                        "3. 如果之前某个工具报错，分析错误消息而不是重写一个新工具\n"
                        "大多数任务不需要自定义工具。"
                    ))],
                ))

        # ── 主动升级检测：首轮结果暗示任务复杂 → 后续轮次直接用强模型 ──
        if strong_model and not _escalated and strong_model != model_name:
            round_tool_names = [fc.function_call.name for fc in function_calls]
            has_heavy = bool(set(round_tool_names) & _ESCALATION_TOOLS)
            many_parallel = len(function_calls) >= 3
            if has_heavy or many_parallel:
                _escalated = True
                logger.info(
                    "early model escalation after round %d: %d calls, heavy=%s → %s",
                    round_num + 1, len(function_calls), has_heavy, strong_model,
                )

        # 压缩旧工具结果，防止 context 膨胀
        if should_compact_history(round_num, _COMPRESS_AFTER_ROUND):
            _compress_old_gemini_results(contents)

    # 轮次耗尽（安全网：_MAX_ROUNDS=50）→ 事实性总结，不问 LLM
    logger.warning("max rounds (%d) reached", _MAX_ROUNDS)
    reply = _build_factual_summary(tool_names_called, action_outcomes, "已达到最大执行轮次。")
    _trigger_memory(sender_id, sender_name, user_text, reply, tool_names_called, call_log, action_outcomes)
    _maybe_record_watchdog(user_text, tool_names_called, sender_id, sender_name, chat_id, chat_type, reply)
    return reply


def _build_factual_summary(
    tool_names_called: list[str],
    action_outcomes: list[tuple[str, str]],
    reason: str = "",
) -> str:
    """从 action_outcomes 构建事实性总结，不涉及 LLM。

    当模型返回空或轮次耗尽时，
    用代码层面的已知事实拼总结，杜绝 LLM 幻觉。
    """
    if not tool_names_called and not action_outcomes:
        return "抱歉，处理过程中遇到了问题，没能完成你的请求。可以再试一次~"

    parts: list[str] = []
    if reason:
        parts.append(reason)

    if action_outcomes:
        parts.append("以下是已完成的操作：")
        for name, outcome in action_outcomes[-15:]:
            parts.append(f"  - {name}: {outcome[:300]}")
    elif tool_names_called:
        unique_tools = list(dict.fromkeys(tool_names_called[-15:]))
        parts.append(f"已调用的工具：{', '.join(unique_tools)}")

    parts.append('\n如果任务还没完成，你可以说"继续"让我接着做。')
    return "\n".join(parts)
