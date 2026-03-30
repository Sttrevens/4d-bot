"""Kimi 统一 Agent（function calling + 对话）

单一 agent 同时处理：
- 普通对话 / 问答 / 闲聊 → 直接文本回复（不调用工具）
- 仓库操作 / 编码任务 → 自动调用 GitHub API 工具

不再需要意图分类器，由模型自行决定是否调用工具。
轮次充足（20轮），但有空转检测：检测到重复调用同一工具时自动刹车。

共享基础设施（工具注册/system prompt/辅助函数）已提取到 base_agent.py。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from openai import AsyncOpenAI, AuthenticationError, RateLimitError

from app.config import settings
from app.services.kimi import _is_k2_model, _extra_body
from app.services.error_log import record_error

# ── 从 base_agent 导入共享基础设施 ──
from app.harness import append_openai_inbox_messages, should_compact_history, should_nudge_unmatched_reads
from app.services.base_agent import (
    # 工具注册表
    ALL_TOOL_MAP,
    _CUSTOM_TOOL_META_NAMES,
    _get_tenant_tools,
    _to_openai_tools,
    ALL_OPENAI_TOOLS,
    # System prompt
    _build_system_prompt,
    _is_admin,
    # Agent 循环辅助
    _has_unmatched_reads,
    _drain_inbox,
    _build_progress_hint,
    # 输出处理
    _strip_degenerate_repetition,
    _strip_hallucinated_code_blocks,
    _trigger_memory,
    # 安全
    _get_custom_tool_risk,
    _user_confirmed,
    # 常量
    _MAX_ROUNDS,
    _MAX_TOOL_RESULT_LEN,
    _COMPRESS_KEEP_RECENT,
    _COMPRESS_AFTER_ROUND,
    # 类型
    ProgressCallback,
    # 工具结果压缩（OpenAI 格式）
    _compress_old_tool_results,
    # 模块操作（被 module_ops.py 导入）
    list_available_modules,
    load_module_content,
)

logger = logging.getLogger(__name__)

# 限制 Kimi API 并发数，避免多人同时聊天时撞限流
_api_semaphore = asyncio.Semaphore(5)


async def _final_call(client: AsyncOpenAI, messages: list[dict]) -> str:
    """不带工具的最终调用，让模型生成总结。

    不传 tools 参数，模型自然无法再调工具，无需注入假 user 消息。
    """
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    try:
        kwargs: dict = dict(
            model=tenant.llm_model or settings.kimi.model,
            messages=messages,
            temperature=1 if _is_k2_model() else 0,
            max_tokens=4096,
        )
        extra = _extra_body()
        if extra:
            kwargs["extra_body"] = extra
        async with _api_semaphore:
            resp = await client.chat.completions.create(**kwargs)
        reply = resp.choices[0].message.content or "操作已完成，建议到 GitHub 检查结果。"
        return _strip_hallucinated_code_blocks(_strip_degenerate_repetition(reply))
    except RateLimitError:
        return (
            "今日 AI 额度已用完（Kimi API 每日 token 上限），请明天再试。\n"
            "如果急需使用，可以联系管理员升级 API 额度。"
        )
    except Exception:
        return "操作过程较复杂，已执行部分步骤，建议到 GitHub 上检查结果。"


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
    *,
    model_override: str = "",
    api_key_override: str = "",
    base_url_override: str = "",
    chat_id: str = "",
    chat_type: str = "",
) -> str:
    """统一消息处理：模型自行决定是对话还是调用工具

    - 最多 20 轮工具调用（足够完成复杂任务）
    - 空转检测：同一工具连续调用 3 次 → 自动刹车让模型总结
    - 仅第一轮发一条中间消息，避免刷屏
    - image_urls: 可选的 base64 data URL 列表（用于视觉输入）
    - mode: safe（默认，先分析再执行）或 full_access（直接执行）
    - chat_context: 当前窗口的最近聊天记录（用于上下文理解）
    - inbox: 实时消息信箱，agent 循环中检查用户插入的新消息
    - model_override: 覆盖模型（用于意图路由，如编码任务走 K2.5）
    - api_key_override: 覆盖 API key
    - base_url_override: 覆盖 base URL
    - chat_id: 当前聊天 ID（p2p 或群聊）
    - chat_type: 聊天类型（p2p / group）
    """
    from app.tenant.context import get_current_tenant
    from app.tools.source_registry import reset as _reset_source_registry
    tenant = get_current_tenant()

    # 重置来源注册表（每轮对话重新收集搜索来源）
    _reset_source_registry()

    # model_override 说明走的是 coding_model 路径（如 K2.5），
    # 此时 fallback 应该到 settings.kimi（全局 Kimi key），
    # 而不是 tenant.llm_api_key（那是 Gemini key）。
    if model_override:
        api_key = api_key_override or settings.kimi.api_key
        base_url = base_url_override or settings.kimi.base_url
    else:
        api_key = api_key_override or tenant.llm_api_key or settings.kimi.api_key
        base_url = base_url_override or tenant.llm_base_url or settings.kimi.base_url
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    admin = _is_admin(sender_id, sender_name)

    # 按租户过滤工具集（客户租户不含 self_*/server_*）
    openai_tools, tool_map = _get_tenant_tools(tenant, user_text=user_text)
    _loaded_tool_names: set[str] = {
        t["function"]["name"] for t in openai_tools if "function" in t
    }

    system_prompt = await _build_system_prompt(
        mode, sender_id=sender_id, sender_name=sender_name,
        user_text=user_text, chat_id=chat_id, chat_type=chat_type,
        actual_tool_names=_loaded_tool_names,
    )
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
    ]

    # 聊天记录作为独立的 system message，不拼进主 system prompt
    # 避免外部用户输入污染系统指令
    if chat_context:
        if len(chat_context) > 2000:
            chat_context = chat_context[:2000]
        messages.append({"role": "system", "content": chat_context})

    if history:
        messages.extend(history)

    # 构建用户消息：用 name 字段标识发送者（协议原生），不在文本中前缀
    user_msg: dict = {"role": "user", "content": user_text}
    if sender_name:
        user_msg["name"] = sender_name

    if image_urls:
        content_parts: list[dict] = [{"type": "text", "text": user_text}]
        for url in image_urls:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": url},
            })
        user_msg["content"] = content_parts

    messages.append(user_msg)

    _progress_count = 0
    _MAX_PROGRESS_MSGS = 2
    _loop_start = time.monotonic()
    _last_progress_at = _loop_start
    call_log: list[str] = []  # 记录工具调用名，用于空转检测
    tool_names_called: list[str] = []  # 记录实际调用的工具名，用于记忆系统
    _nudged = False  # 标记是否已追加过 unfulfilled promise 催促

    _model = model_override or tenant.llm_model or settings.kimi.model
    _is_k2 = "k2" in _model.lower()

    for round_num in range(_MAX_ROUNDS):
        logger.info("kimi agent round %d (model=%s)", round_num + 1, _model)

        kwargs: dict = dict(
            model=_model,
            messages=messages,
            tools=openai_tools,
            temperature=1 if _is_k2 else 0,
            max_tokens=4096,
        )
        if _is_k2:
            kwargs["extra_body"] = {"chat_template_kwargs": {"thinking": False}}

        try:
            async with _api_semaphore:
                resp = await client.chat.completions.create(**kwargs)
        except RateLimitError as exc:
            logger.warning("Kimi API rate limit hit: %s", exc)
            return (
                "今日 AI 额度已用完（Kimi API 每日 token 上限），请明天再试。\n"
                "如果急需使用，可以联系管理员升级 API 额度。"
            )
        except AuthenticationError as exc:
            logger.error("Kimi API auth failed (round %d): %s", round_num + 1, exc)
            return "AI 服务认证异常，管理员正在处理中，请稍后再试~"
        except Exception as exc:
            logger.exception("Kimi API call failed (round %d)", round_num + 1)
            return "AI 调用出了点问题，请稍后再试。如果持续出现，请联系管理员~"

        if not resp.choices:
            logger.error("Kimi API returned empty choices: %s", resp)
            return "AI 返回了空结果，请稍后再试。"

        choice = resp.choices[0]
        message = choice.message

        # 没有工具调用 → 准备退出循环
        if not message.tool_calls:
            reply_text = message.content or "任务完成。"

            # ── 退出前检查 1: inbox 里有未处理消息 ──
            if inbox is not None:
                pending = _drain_inbox(inbox)
                if pending:
                    messages.append({"role": "assistant", "content": reply_text})
                    append_openai_inbox_messages(
                        messages,
                        pending,
                        logger=logger,
                        log_label="inbox drain before exit",
                    )
                    logger.info("continuing loop: %d inbox messages pending", len(pending))
                    continue

            # ── 退出前检查 2: 读了数据但没写回 ──
            if should_nudge_unmatched_reads(
                round_num=round_num,
                already_nudged=_nudged,
                tool_names_called=tool_names_called,
                user_text=user_text,
                has_unmatched_reads=_has_unmatched_reads,
            ):
                _nudged = True
                logger.info(
                    "read-without-write at round %d (tools: %s), nudging",
                    round_num + 1, tool_names_called,
                )
                messages.append({"role": "assistant", "content": reply_text})
                messages.append({
                    "role": "user",
                    "content": "你已经读取了内容但还没有执行修改。请调用对应的写入工具（如 write_file）完成操作，不要只是描述你会怎么改。",
                })
                continue

            reply = _strip_hallucinated_code_blocks(_strip_degenerate_repetition(reply_text))
            _trigger_memory(sender_id, sender_name, user_text, reply, tool_names_called, call_log)
            return reply

        # ── 智能进度通知 ──
        if on_progress and _progress_count < _MAX_PROGRESS_MSGS:
            now = time.monotonic()
            send_progress = False
            if _progress_count == 0 and round_num >= 2 and (now - _loop_start) > 8:
                send_progress = True
            elif _progress_count > 0 and (now - _last_progress_at) > 20:
                send_progress = True
            if send_progress:
                msg = message.content if message.content else _build_progress_hint(tool_names_called, _progress_count)
                try:
                    await on_progress(_strip_degenerate_repetition(msg))
                    _progress_count += 1
                    _last_progress_at = now
                except Exception:
                    logger.warning("on_progress callback failed", exc_info=True)

        messages.append(message.model_dump())

        # 执行每个工具调用
        for tool_call in message.tool_calls:
            func_name = tool_call.function.name

            try:
                func_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                func_args = {}

            # 自定义工具元操作：自动注入 tenant_id，bot 不需要猜
            if func_name in _CUSTOM_TOOL_META_NAMES and "tenant_id" not in func_args:
                func_args["tenant_id"] = tenant.tenant_id

            # 记忆工具：自动注入 sender 上下文（LLM 不知道当前用户的 open_id）
            if func_name in ("save_memory", "recall_memory"):
                if not func_args.get("user_id"):
                    func_args["user_id"] = sender_id
                if not func_args.get("user_name"):
                    func_args["user_name"] = sender_name

            # 空转检测用 name(args) 作 key，区分批量操作和真正循环
            call_key = f"{func_name}({tool_call.function.arguments})"
            call_log.append(call_key)

            logger.info("tool call: %s(%s)", func_name, func_args)
            if func_name != "think":
                tool_names_called.append(func_name)

            from app.tools.tool_result import ToolResult

            handler = tool_map.get(func_name)
            if handler is None:
                result = ToolResult.error(f"unknown tool: {func_name}", code="not_found")
            else:
                # 自定义工具风险确认：yellow/red 级别需用户在消息中明确同意
                risk = _get_custom_tool_risk(tenant.tenant_id, func_name)
                if risk in ("yellow", "red") and not _user_confirmed(messages, func_name):
                    risk_hint = "写操作" if risk == "yellow" else "批量/高风险操作"
                    result = ToolResult.blocked(
                        f"⚠️ 这是一个{risk_hint}工具（{risk}级别）。"
                        f"请在回复中告诉用户你要执行什么操作、影响范围，"
                        f"等用户确认后再调用。用户回复'好的''确认''执行'等即视为同意。"
                    )
                else:
                    try:
                        result = handler(func_args)
                        # 支持 async 工具（如 analyze_video_url）
                        import asyncio as _aio
                        if _aio.iscoroutine(result):
                            result = await result
                    except Exception as exc:
                        logger.exception("tool %s failed", func_name)
                        result = ToolResult.error(str(exc), code="internal")
                        record_error("tool_exception", f"{func_name} 异常", exc=exc,
                                     tool_name=func_name, tool_args=func_args)

            # 统一处理结果：ToolResult / dict / str 都归一化
            pending_image_refs: list[dict] = []
            if isinstance(result, ToolResult):
                if not result.ok:
                    record_error(
                        "tool_error",
                        f"{func_name}({func_args}) → [{result.code}] {result.content[:300]}",
                        tool_name=func_name, tool_args=func_args,
                    )
                result_str = result.content
            elif isinstance(result, dict) and "image_refs" in result:
                # fetch_chat_history 返回 dict，含图片引用需要特殊处理
                pending_image_refs = result.get("image_refs", [])[:5]
                result_str = result.get("text", "")
            elif isinstance(result, str):
                # 向后兼容：未迁移的工具仍返回 str
                if "[ERROR]" in result:
                    record_error(
                        "tool_error",
                        f"{func_name}({func_args}) → {result[:300]}",
                        tool_name=func_name, tool_args=func_args,
                    )
                result_str = result
            else:
                result_str = str(result)
            if len(result_str) > _MAX_TOOL_RESULT_LEN:
                result_str = (
                    result_str[:_MAX_TOOL_RESULT_LEN]
                    + f"\n\n... (结果已截断，原文 {len(result_str)} 字符，仅展示前 {_MAX_TOOL_RESULT_LEN})"
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_str,
                }
            )

            # 记录 bot 行动日记（只记写操作，读操作跳过）
            try:
                from app.services import memory as bot_memory
                bot_memory.note_tool_action(func_name, func_args, result_str, sender_id, sender_name)
            except Exception:
                pass

            # 下载聊天记录中的图片，作为 multimodal 用户消息注入，让 LLM 看到
            if pending_image_refs:
                from app.services.feishu import feishu_client
                img_parts: list[dict] = []
                for ref in pending_image_refs:
                    try:
                        data_url = await feishu_client.download_image(
                            ref["message_id"], ref["image_key"],
                        )
                        if data_url:
                            img_parts.append({
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            })
                    except Exception:
                        logger.debug("failed to download history image %s", ref.get("image_key", ""))
                if img_parts:
                    img_parts.insert(0, {
                        "type": "text",
                        "text": f"（以下是聊天记录中 {len(img_parts)} 张图片，对应上面文本中的 [图片N] 编号）",
                    })
                    messages.append({"role": "user", "content": img_parts})
                    logger.info("injected %d history images from tool call", len(img_parts) - 1)

        # ── 检查信箱：用户是否在执行过程中发了新消息 ──
        # 直接作为 user message 注入，模型从对话结构自然理解这是中途插入
        if inbox is not None:
            append_openai_inbox_messages(
                messages,
                _drain_inbox(inbox),
                logger=logger,
                log_label="inbox inject",
            )

        # 压缩旧工具结果，防止 context 膨胀
        if should_compact_history(round_num, _COMPRESS_AFTER_ROUND):
            _compress_old_tool_results(messages)

    # 轮次耗尽
    logger.warning("max rounds (%d) reached", _MAX_ROUNDS)
    reply = await _final_call(client, messages)
    _trigger_memory(sender_id, sender_name, user_text, reply, tool_names_called, call_log)
    return reply
