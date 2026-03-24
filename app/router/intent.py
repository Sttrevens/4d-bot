"""消息路由

根据租户 LLM 配置，将消息路由到对应的 provider：
- 配置 coding_model 时：非多模态消息全部走 K2.5，多模态走 Gemini
- 多模态判定（三层）：图片/视频附件 > 已知视频平台 URL > 任意 URL + 媒体关键词
- 未配置时：全部走主 provider（向后兼容）

前置检查（在路由之前）：
- 配额检查：月度 API 调用/token 上限
- 限流检查：per-tenant / per-user 滑动窗口

后置记录：
- 用量计量：token 数、工具调用次数、耗时等
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from app.services.history import chat_history, last_tool_summary
from app.services.kimi_coder import handle_message as kimi_handle_message
from app.services.base_agent import ProgressCallback
from app.services.rate_limiter import check_rate_limit
from app.services.metering import check_quota, record_usage, UsageRecord, last_usage_tokens
from app.services.trial import check_trial, check_user_token_quota, record_user_tokens
from app.tools.feishu_api import set_current_user
from app.tenant.context import SenderContext

logger = logging.getLogger(__name__)


# ── 多模态检测（三层策略，从精确到模糊）──

# 层 1: 已知视频/媒体平台（精确匹配，无需关键词辅助）
_VIDEO_PLATFORM_RE = re.compile(
    r"https?://(www\.|m\.)?"
    r"(youtube\.com/(watch|shorts|live)|youtu\.be/"
    r"|bilibili\.com/video|b23\.tv/"
    r"|vimeo\.com/|twitch\.tv/"
    r"|tiktok\.com/|douyin\.com/"
    r"|v\.qq\.com/|ixigua\.com/"
    r"|xiaohongshu\.com/(explore|discovery/item)"
    r"|xhslink\.com/)",
    re.IGNORECASE,
)

# 层 2: 媒体文件直链（URL 以媒体扩展名结尾）
_MEDIA_FILE_RE = re.compile(
    r"https?://\S+\."
    r"(mp4|webm|avi|mov|mkv|flv"       # 视频
    r"|mp3|wav|ogg|flac|aac|m4a"        # 音频
    r"|jpg|jpeg|png|gif|webp|bmp|svg"   # 图片
    r")(\?|\s|$)",
    re.IGNORECASE,
)

# 层 3: 任意 URL + 媒体意图关键词（灵活兜底）
_ANY_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MEDIA_KEYWORDS_RE = re.compile(
    r"(视频|图片|图像|照片|截图|看看|看一下|看下|看这|帮我看|帮我分析"
    r"|播放|分析.{0,4}(链接|网页|页面|内容)|打开.{0,4}看"
    r"|video|image|photo|screenshot|watch|analyze)"
    r"",
    re.IGNORECASE,
)


# ── 上下文重置检测（fresh start）──
# 当用户表达"重新来/重做/改错了重改"等意图时，
# 清除对话历史和近期记忆，让 bot 从干净状态开始。
_FRESH_START_RE = re.compile(
    r"(重新[改做来写]|从头[开来再]|重来|重做|推倒重来"
    r"|忘[掉了].{0,6}重[改做新]|不[对要行].{0,6}重[改做新来写]"
    r"|别改了.{0,6}重新|放弃.{0,6}重[改做新来]"
    r"|之前.{0,6}[错乱].{0,6}重|清空.{0,6}重[改做新]"
    r"|start\s*over|redo|from\s*scratch)",
    re.IGNORECASE,
)


def _is_fresh_start(text: str) -> bool:
    """检测用户是否要求重新开始（清除上下文污染）。"""
    return bool(_FRESH_START_RE.search(text))


def _is_multimodal(text: str, image_urls: list[str] | None) -> tuple[bool, str]:
    """判断消息是否涉及多模态内容。

    Returns:
        (is_multimodal, reason) — reason 用于日志。
    """
    if image_urls:
        return True, "image_attachments"
    if _VIDEO_PLATFORM_RE.search(text):
        return True, "video_platform_url"
    if _MEDIA_FILE_RE.search(text):
        return True, "media_file_url"
    if _ANY_URL_RE.search(text) and _MEDIA_KEYWORDS_RE.search(text):
        return True, "url+media_keyword"
    return False, ""


async def route_message(
    user_text: str,
    sender_id: str,
    sender_name: str = "",
    on_progress: ProgressCallback | None = None,
    image_urls: list[str] | None = None,
    mode: str = "safe",
    chat_context: str = "",
    chat_id: str = "",
    chat_type: str = "",
    inbox: asyncio.Queue[str] | None = None,
) -> str:
    """将用户消息交给统一 agent 处理"""
    from app.tenant.context import get_current_tenant, get_current_channel, set_current_sender, get_current_sender
    tenant = get_current_tenant()

    # ── 跨平台身份解析 ──
    current_ch = get_current_channel()
    channel_platform = current_ch.platform if current_ch else tenant.platform

    # ── Agent Profile 路由（借鉴 OpenClaw binding 系统）──
    _apply_agent_profile(tenant, channel_platform, chat_id, chat_type, sender_id)
    _resolve_identity(tenant, channel_platform, sender_id, sender_name)

    # ── 前置检查：白名单访问控制 ──
    if tenant.allowed_users:
        allowed_ids = {u.get("external_userid", "") for u in tenant.allowed_users if isinstance(u, dict)}
        if sender_id not in allowed_ids:
            logger.warning("access denied: tenant=%s sender=%s not in allowed_users",
                           tenant.tenant_id, sender_id[:12])
            return tenant.access_deny_msg

    # ── 前置检查：配额 ──
    quota_ok, quota_reason = check_quota(tenant.tenant_id)
    if not quota_ok:
        logger.warning("quota exceeded: tenant=%s reason=%s", tenant.tenant_id, quota_reason)
        return f"抱歉，{quota_reason}。请联系管理员升级配额。"

    # ── 前置检查：限流 ──
    rate_ok, rate_reason = check_rate_limit(
        tenant.tenant_id,
        sender_id,
        tenant_rpm=tenant.rate_limit_rpm,
        user_rpm=tenant.rate_limit_user_rpm,
    )
    if not rate_ok:
        logger.warning("rate limited: tenant=%s sender=%s reason=%s",
                       tenant.tenant_id, sender_id[:12], rate_reason)
        return rate_reason

    # ── 前置检查：试用期 ──
    if tenant.trial_enabled:
        trial_ok, trial_reason = check_trial(
            tenant.tenant_id, sender_id, tenant.trial_duration_hours,
            display_name=sender_name,
        )
        if not trial_ok:
            logger.warning("trial blocked: tenant=%s sender=%s reason=%s",
                           tenant.tenant_id, sender_id[:12], trial_reason)
            return trial_reason

    # ── 前置检查：每用户 6 小时 token 限额 ──
    if tenant.quota_user_tokens_6h:
        q6h_ok, q6h_reason = check_user_token_quota(
            tenant.tenant_id, sender_id, tenant.quota_user_tokens_6h,
        )
        if not q6h_ok:
            logger.warning("6h token quota exceeded: tenant=%s sender=%s", tenant.tenant_id, sender_id[:12])
            return q6h_reason

    # 群聊：用 chat_id 做历史 key（所有人共享上下文）
    # 私聊：用 sender_id 做历史 key（各自独立）
    # 跨平台身份：如果有 identity_id，用 identity_id 做历史 key（跨 channel 共享上下文）
    if chat_type == "group" and chat_id:
        history_key = chat_id
    else:
        sender_ctx = get_current_sender()
        history_key = sender_ctx.identity_id if sender_ctx.identity_id else sender_id

    # ── 上下文重置检测 ──
    # 用户说"重新改/重来/推倒重来"时，清除污染的对话历史和近期记忆，
    # 让 bot 从干净状态开始处理新的请求。
    if _is_fresh_start(user_text):
        chat_history.clear(history_key)
        try:
            from app.services.memory import mark_recent_as_failed
            mark_recent_as_failed(sender_id)
        except Exception:
            logger.debug("mark_recent_as_failed failed", exc_info=True)
        logger.info("fresh start detected for %s: context cleared", sender_id[:12])

    # 获取对话历史
    history = chat_history.get(history_key)

    # 记录用户消息（群聊时带上发送者名字，方便模型区分谁说的）
    if chat_type == "group" and sender_name:
        chat_history.add_user(history_key, f"[{sender_name}]: {user_text}")
    else:
        chat_history.add_user(history_key, user_text)

    logger.info("sender=%s(%s) mode=%s text=%s", sender_name or "?", sender_id, mode, user_text[:80])

    # 设置当前用户，让工具层能用 user_access_token
    set_current_user(sender_id)

    # OAuth reauth 提醒已移除 —— 不再主动打断用户。
    # 当用户实际调用需要 OAuth 的工具（日历/任务/邮件）时，
    # 工具本身会返回 reauth 提示（如 calendar_ops._check_reauth_needed）。

    # ── 计时开始 ──
    t_start = time.monotonic()

    # ── 路由策略 ──
    provider_used = tenant.llm_provider
    model_used = tenant.llm_model

    multimodal, mm_reason = _is_multimodal(user_text, image_urls)

    if tenant.coding_model and not multimodal:
        logger.info("text-only → routing to %s", tenant.coding_model)
        model_used = tenant.coding_model
        provider_used = "openai"
        reply = await kimi_handle_message(
            user_text,
            history=history,
            sender_name=sender_name,
            sender_id=sender_id,
            on_progress=on_progress,
            image_urls=image_urls,
            mode=mode,
            chat_context=chat_context,
            inbox=inbox,
            model_override=tenant.coding_model,
            api_key_override=tenant.coding_api_key,
            base_url_override=tenant.coding_base_url,
            chat_id=chat_id,
            chat_type=chat_type,
        )
        chat_history.add_assistant(history_key, _enrich_reply(reply))
        _record(tenant.tenant_id, sender_id, model_used, provider_used, t_start)
        return reply

    if multimodal and tenant.coding_model:
        logger.info("multimodal(%s) → routing to Gemini", mm_reason)

    if tenant.llm_provider == "gemini":
        from app.services.gemini_provider import handle_message as gemini_handle_message
        handler = gemini_handle_message
    else:
        handler = kimi_handle_message

    reply = await handler(
        user_text,
        history=history,
        sender_name=sender_name,
        sender_id=sender_id,
        on_progress=on_progress,
        image_urls=image_urls,
        mode=mode,
        chat_context=chat_context,
        inbox=inbox,
        chat_id=chat_id,
        chat_type=chat_type,
    )

    chat_history.add_assistant(history_key, _enrich_reply(reply))
    _record(tenant.tenant_id, sender_id, model_used, provider_used, t_start)

    return reply


def _enrich_reply(reply: str) -> str:
    """将工具调用摘要附加到 reply，存入对话历史。"""
    try:
        summary = last_tool_summary.get("")
        if summary:
            last_tool_summary.set("")
            return f"{summary}\n{reply}"
    except Exception:
        pass
    return reply


def _resolve_identity(tenant, channel_platform: str, sender_id: str, sender_name: str) -> None:
    """解析发送者的跨平台统一身份，设置到 SenderContext。"""
    try:
        from app.services.identity import resolve_sender
        from app.tenant.context import set_current_sender

        identity_id, linked = resolve_sender(tenant.tenant_id, channel_platform, sender_id)

        sender_ctx = SenderContext(
            sender_id=sender_id,
            sender_name=sender_name,
            identity_id=identity_id or "",
            channel_platform=channel_platform,
            linked_platforms=linked,
        )
        set_current_sender(sender_ctx)
    except Exception:
        logger.debug("identity resolution failed for %s", sender_id[:12], exc_info=True)
        from app.tenant.context import set_current_sender
        set_current_sender(SenderContext(
            sender_id=sender_id,
            sender_name=sender_name,
            channel_platform=channel_platform,
        ))


def _record(
    tenant_id: str,
    sender_id: str,
    model: str,
    provider: str,
    t_start: float,
) -> None:
    """Fire-and-forget 用量记录 + per-user token 记录"""
    try:
        latency_ms = int((time.monotonic() - t_start) * 1000)
        in_tok, out_tok = last_usage_tokens.get((0, 0))
        last_usage_tokens.set((0, 0))

        record_usage(UsageRecord(
            tenant_id=tenant_id,
            sender_id=sender_id,
            model=model or "",
            provider=provider or "",
            api_calls=1,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_ms=latency_ms,
        ))

        total_tokens = in_tok + out_tok
        if total_tokens > 0:
            record_user_tokens(tenant_id, sender_id, total_tokens)
    except Exception:
        logger.debug("usage recording failed", exc_info=True)


def _apply_agent_profile(
    tenant, channel_platform: str, chat_id: str, chat_type: str, sender_id: str,
) -> None:
    """按 channel/chat/user 匹配 agent profile，覆盖 tenant 运行时配置。"""
    if not tenant.agent_profiles or not tenant.agent_bindings:
        return

    try:
        from app.channels.routing import (
            resolve_agent_profile,
            parse_profiles_from_config,
            parse_bindings_from_config,
        )

        profiles = parse_profiles_from_config(tenant.agent_profiles)
        bindings = parse_bindings_from_config(tenant.agent_bindings)

        profile = resolve_agent_profile(
            profiles, bindings,
            platform=channel_platform,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender_id,
        )

        if not profile:
            return

        if profile.system_prompt:
            tenant.llm_system_prompt = profile.system_prompt
        if profile.tools_enabled:
            tenant.tools_enabled = profile.tools_enabled
        if profile.model:
            tenant.llm_model = profile.model
        if profile.custom_persona:
            tenant.custom_persona = profile.custom_persona

        logger.info(
            "agent_profile: applied profile='%s' (%s) for platform=%s",
            profile.profile_id, profile.name, channel_platform,
        )
    except Exception:
        logger.debug("agent_profile resolution failed", exc_info=True)
