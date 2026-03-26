"""飞书 Webhook 事件处理

支持的消息类型:
- text: 纯文本
- image: 图片 → 下载后用 Kimi 视觉能力分析
- file: 文件附件 → 文本类文件读取内容，其他提示不支持
- post: 富文本 → 提取纯文本
- merge_forward: 合并转发 → 提取子消息文本
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time as _time
from typing import Any

from fastapi import APIRouter, Request
from openai import RateLimitError

from app.config import settings
from app.router.intent import route_message
from app.services.feishu import FeishuClient, FileTooLargeError
from app.services.user_registry import register as register_user, register_p2p_chat
from app.services.oauth_store import build_auth_url, is_authorized, get_token_info, clear_user_token
from app.services.error_log import record_error
from app.tools.message_ops import fetch_chat_history
from app.tenant.context import get_current_tenant, set_current_tenant, set_current_channel
from app.tenant.registry import tenant_registry
from app.webhook.base import (
    MessageDedup, UserStateManager, tuk,
    truncate_text, handle_mode_command,
    DEFAULT_PROCESS_TIMEOUT, DEFAULT_MAX_USER_TEXT_LEN, DEFAULT_STALE_MSG_THRESHOLD,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ── 共享基础设施 ──
_dedup = MessageDedup(max_cache=2048, ttl=600)  # 10 分钟 TTL
_state = UserStateManager(mode_ttl=7200, lock_idle_ttl=3600, persist_mode=True)
_STALE_MSG_THRESHOLD = DEFAULT_STALE_MSG_THRESHOLD

feishu_client = FeishuClient()

# 每个租户的 bot open_id 缓存（用于精确匹配 @mention）
_bot_open_ids: dict[str, str] = {}  # tenant_id → open_id


def _get_bot_open_id(tenant) -> str:
    """获取当前租户 bot 的 open_id（缓存）

    优先级：缓存 → tenants.json 配置 → /bot/v3/info API（需要 bot:info scope）
    """
    tid = tenant.tenant_id
    if tid in _bot_open_ids:
        return _bot_open_ids[tid]
    # 从 tenants.json 中的 bot_open_id 字段读取（不需要额外 API scope）
    if getattr(tenant, "bot_open_id", ""):
        _bot_open_ids[tid] = tenant.bot_open_id
        logger.info("cached bot open_id from config for tenant=%s: %s", tid, tenant.bot_open_id[:15])
        return tenant.bot_open_id
    # fallback: 调 API（需要 application:bot 或 bot:info scope）
    try:
        from app.tools.feishu_api import feishu_get
        data = feishu_get("/bot/v3/info")
        if isinstance(data, dict):
            oid = data.get("data", {}).get("bot", {}).get("open_id", "")
            if oid:
                _bot_open_ids[tid] = oid
                logger.info("cached bot open_id from API for tenant=%s: %s", tid, oid[:15])
                return oid
            else:
                logger.warning("bot open_id empty in response for tenant=%s: %s",
                               tid, str(data)[:200])
        else:
            logger.warning("bot open_id API failed for tenant=%s: %s", tid, str(data)[:200])
    except Exception:
        logger.warning("failed to fetch bot open_id for tenant=%s", tid, exc_info=True)
    return ""


def precache_bot_open_ids() -> int:
    """启动时预缓存所有飞书租户的 bot open_id，避免首次 webhook 时 API 失败导致过滤跳过。"""
    from app.tenant.registry import tenant_registry
    from app.tenant.context import set_current_tenant
    cached = 0
    for tid, tenant in tenant_registry.all_tenants().items():
        if tenant.platform != "feishu":
            continue
        if tid in _bot_open_ids:
            cached += 1
            continue
        set_current_tenant(tenant)
        oid = _get_bot_open_id(tenant)
        if oid:
            cached += 1
        else:
            logger.warning("precache: failed to get bot open_id for tenant=%s", tid)
    return cached


def _is_mention_at_bot(mentions: list, bot_open_id: str) -> bool:
    """检查 mentions 列表中是否有 @当前bot"""
    for m in mentions:
        m_id = m.get("id", {})
        oid = m_id.get("open_id", "") if isinstance(m_id, dict) else str(m_id)
        if oid == bot_open_id:
            return True
    return False


def _is_mention_at_bot_by_name(mentions: list, tenant) -> bool:
    """当 bot_open_id 未知时，通过名字匹配 @mention。

    匹配到后自动学习 open_id，下次就不用走名字匹配了。
    飞书 mention 结构: {"key": "@_user_1", "id": {"open_id": "ou_xxx"}, "name": "高梦"}

    匹配候选：tenant.name + tenant.bot_aliases（飞书应用显示名可能和 tenant.name 不同）
    """
    # 构建所有可能的名字候选
    candidates = set()
    if tenant.name:
        candidates.add(tenant.name)
    for alias in getattr(tenant, "bot_aliases", []):
        if alias:
            candidates.add(alias)
    if not candidates:
        return False

    for m in mentions:
        mention_name = m.get("name", "")
        if not mention_name:
            continue
        # 任一候选名匹配即可（精确或子串）
        for name in candidates:
            if mention_name == name or name in mention_name or mention_name in name:
                # 学习 open_id
                m_id = m.get("id", {})
                oid = m_id.get("open_id", "") if isinstance(m_id, dict) else ""
                if oid:
                    _bot_open_ids[tenant.tenant_id] = oid
                    logger.info(
                        "learned bot open_id from @mention name match: tenant=%s matched=%s mention=%s oid=%s",
                        tenant.tenant_id, name, mention_name, oid[:15],
                    )
                return True
    return False


# ── In-flight 请求追踪（用于部署保护 + 重启恢复）──
# message_id → {sender_id, chat_id, chat_type, tenant_id, start_time, text_preview}
_in_flight: dict[str, dict] = {}


def get_in_flight_count() -> int:
    """当前正在处理的用户请求数（供 self_safe_deploy 检查）"""
    return len(_in_flight)


def get_in_flight_messages() -> dict[str, dict]:
    """返回所有 in-flight 请求的元数据（供 shutdown 保存到 Redis）"""
    return dict(_in_flight)


def load_user_modes_from_redis() -> int:
    """从 Redis 恢复用户模式（启动时调用）。返回恢复数量。"""
    return _state.load_modes_from_redis()


def get_pending_batch_messages() -> dict[str, list[dict]]:
    """返回当前待处理的批量消息（供 shutdown 保存）。"""
    result = {}
    for uk, msgs in _user_pending.items():
        result[uk] = [
            {
                "text": m.get("text", ""),
                "message_id": m.get("message_id", ""),
                "chat_id": m.get("chat_id", ""),
                "chat_type": m.get("chat_type", ""),
            }
            for m in msgs
        ]
    return result

# 文本类文件后缀
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".xml", ".csv", ".log",
    ".py", ".js", ".ts", ".cs", ".java", ".cpp", ".c", ".h",
    ".html", ".css", ".sh", ".bat", ".ini", ".toml", ".cfg",
    ".sql", ".r", ".lua", ".rb", ".go", ".rs", ".swift",
}

# 视频/图片/音频后缀（文件附件形式发送时也能识别并处理）
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tiff"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".opus"}

# Gemini 多模态可直接理解的富文档格式——下载后转 data URL 传给 LLM
_RICH_FILE_EXTS = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# 用户消息最大长度（字符数）：超过此长度截断，防止 context 溢出
_MAX_USER_TEXT_LEN = 8000

# 飞书单条文本消息长度限制（保守值，实际约 30,000）
_MAX_REPLY_LEN = 4000

# 整体处理超时（秒）：超过此时间自动回复超时提示
# 自我迭代任务（读写代码+LLM多轮）需要较长时间，180s 不够
_PROCESS_TIMEOUT = 600

# ── 消息合并：用户连发多条时攒在一起处理 ──
_BATCH_WAIT = 1.5  # 秒，等新消息的窗口期
_user_pending: dict[str, list[dict]] = {}   # sender_id → 待处理消息列表
_batch_timers: dict[str, asyncio.Task] = {}  # sender_id → 定时器任务


@router.post("/webhook/event")
async def handle_event(request: Request) -> dict[str, Any]:
    """处理飞书事件回调（默认租户，兼容现有部署）"""
    # 设置默认租户上下文
    tenant = tenant_registry.get_default()
    set_current_tenant(tenant)
    ch = tenant.get_channel("feishu")
    if ch:
        set_current_channel(ch)
    return await _handle_event_inner(request)


@router.post("/webhook/feishu/{tenant_id}/event")
async def handle_feishu_tenant_event(tenant_id: str, request: Request) -> dict[str, Any]:
    """处理飞书事件回调（带平台前缀，匹配 nginx 路由 /webhook/feishu/{tenant_id}/event）"""
    tenant = tenant_registry.get(tenant_id)
    if not tenant:
        logger.warning("unknown tenant_id: %s", tenant_id)
        return {"code": -1, "msg": "unknown tenant"}
    set_current_tenant(tenant)
    ch = tenant.get_channel("feishu")
    if ch:
        set_current_channel(ch)
    return await _handle_event_inner(request)


@router.post("/webhook/channel/{channel_id}/event")
async def handle_channel_event(channel_id: str, request: Request) -> dict[str, Any]:
    """通用 channel 路由：按 channel_id 查找 (tenant, channel)"""
    tenant, ch = tenant_registry.find_by_channel_id(channel_id)
    if not tenant or not ch:
        logger.warning("unknown channel_id: %s", channel_id)
        return {"code": -1, "msg": "unknown channel"}
    set_current_tenant(tenant)
    set_current_channel(ch)
    if ch.platform == "feishu":
        return await _handle_event_inner(request)
    # 非飞书 channel 不应走这个 handler（应走 wecom/wecom_kf handler）
    logger.warning("channel %s platform=%s routed to feishu handler", channel_id, ch.platform)
    return {"code": -1, "msg": f"platform {ch.platform} not supported on this handler"}


@router.post("/webhook/{tenant_id}/event")
async def handle_tenant_event(tenant_id: str, request: Request) -> dict[str, Any]:
    """处理飞书事件回调（兼容旧路径 /webhook/{tenant_id}/event）"""
    tenant = tenant_registry.get(tenant_id)
    if not tenant:
        logger.warning("unknown tenant_id: %s", tenant_id)
        return {"code": -1, "msg": "unknown tenant"}
    set_current_tenant(tenant)
    ch = tenant.get_channel("feishu")
    if ch:
        set_current_channel(ch)
    return await _handle_event_inner(request)


async def _handle_event_inner(request: Request) -> dict[str, Any]:
    """事件处理核心逻辑（租户上下文已设置）"""
    tenant = get_current_tenant()
    body: dict = await request.json()
    logger.info("webhook incoming: tenant=%s event_type=%s body_keys=%s",
                tenant.tenant_id, body.get("header", {}).get("event_type", "unknown"),
                list(body.keys()))

    # ---------- 验证 token（在 challenge 之前，防止未授权的 webhook URL 注册）----------
    header = body.get("header", {})
    token = header.get("token", "")
    if not tenant.verification_token:
        logger.warning("verification_token not configured for tenant=%s, rejecting event", tenant.tenant_id)
        return {"code": -1, "msg": "verification_token not configured"}
    if token != tenant.verification_token:
        logger.warning("invalid verification token for tenant=%s", tenant.tenant_id)
        return {"code": -1, "msg": "invalid token"}

    # ---------- challenge 验证（token 已验证）----------
    if "challenge" in body:
        logger.info("challenge verification: returning challenge for tenant=%s", tenant.tenant_id)
        return {"challenge": body["challenge"]}

    # ---------- 加密事件解密 ----------
    if "encrypt" in body:
        logger.warning("received encrypted event but encrypt_key is not configured for tenant=%s", tenant.tenant_id)
        return {"code": 0, "msg": "encrypt_key not configured"}

    # ---------- 事件处理 ----------
    event = body.get("event", {})
    event_id = header.get("event_id", "")

    # 去重（基于 TTL 的 OrderedDict，避免内存泄漏）
    if _dedup.is_duplicate(event_id):
        logger.debug("duplicate event %s, skipping", event_id)
        return {"code": 0}

    # ---------- 只处理 im.message.receive_v1 ----------
    event_type = header.get("event_type", "")
    if event_type != "im.message.receive_v1":
        logger.debug("unhandled event type: %s", event_type)
        return {"code": 0}

    message = event.get("message", {})
    message_id = message.get("message_id", "")
    chat_id = message.get("chat_id", "")
    chat_type = message.get("chat_type", "")  # "p2p" or "group"
    sender = event.get("sender", {})
    sender_id = sender.get("sender_id", {}).get("open_id", "")
    sender_type = sender.get("sender_type", "")
    msg_type = message.get("message_type", "")

    # 跳过 bot 自己发的消息，防止自我回复循环
    if sender_type == "app":
        logger.debug("skip bot-sent message from %s", sender_id[:12])
        return {"code": 0}

    logger.info("received msg_type=%s from=%s chat=%s(%s) tenant=%s",
                msg_type, sender_id, chat_id[:15], chat_type, tenant.tenant_id)

    # ── 跳过过旧的消息（重启后被飞书重新投递的历史消息）──
    create_time_str = message.get("create_time", "")
    if create_time_str:
        try:
            # 飞书 create_time 是毫秒级 epoch 字符串
            msg_epoch = int(create_time_str) / 1000.0
            age = _time.time() - msg_epoch
            if age > _STALE_MSG_THRESHOLD:
                logger.info("skip stale feishu msg (%.0fs old) msg=%s from=%s type=%s",
                            age, message_id[:15], sender_id[:12], msg_type)
                return {"code": 0}
        except (ValueError, TypeError):
            pass  # create_time 格式异常，不阻塞正常流程

    # 记录私聊 chat_id 映射，供 fetch_chat_history 按 open_id 查私聊用
    if chat_type == "p2p" and sender_id and chat_id:
        register_p2p_chat(sender_id, chat_id)

    # 群聊过滤：只处理 @本bot 的消息
    # 开启 im:message.group_msg 权限后 bot 会收到所有群消息，
    # 精确匹配 @mention 中的 open_id，避免多 bot 同群时重复回复
    if chat_type == "group":
        mentions = message.get("mentions", [])
        if not mentions:
            logger.debug("group msg without @mention from %s, skipping", sender_id[:12])
            return {"code": 0}
        bot_oid = _get_bot_open_id(tenant)
        if bot_oid:
            if not _is_mention_at_bot(mentions, bot_oid):
                logger.debug("group msg @mentions others (not this bot) from %s, skipping", sender_id[:12])
                return {"code": 0}
        else:
            # bot open_id 未知 → 用名字匹配（同时自动学习 open_id）
            if _is_mention_at_bot_by_name(mentions, tenant):
                logger.info("group msg: matched @mention by name for tenant=%s", tenant.tenant_id)
            else:
                logger.debug("group msg @mentions others (name mismatch) from %s, skipping", sender_id[:12])
                return {"code": 0}

    # ── 写入 Redis inbox：先持久化再返回 200，防止重启丢消息 ──
    # 飞书收到 200 后不会重试，所以必须在返回前确保消息已持久化。
    # 处理完成后在 _dispatch_message 的 finally 中 HDEL 清除（覆盖所有路径：
    # 命令、批处理、session 注入、直接处理）。_process_and_reply 也有冗余 HDEL。
    # 如果容器在处理过程中被杀，重启后 _recover_missed_messages 会
    # HGETALL 读取残留的 inbox 条目并重新投递。
    try:
        from app.services import redis_client as _redis
        if _redis.available():
            _inbox_payload = json.dumps({
                "msg_type": msg_type,
                "message": message,
                "message_id": message_id,
                "sender_id": sender_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "tenant_id": tenant.tenant_id,
                "received_at": _time.time(),
            }, ensure_ascii=False)
            _redis.execute("HSET", f"msg:inbox:{tenant.tenant_id}", message_id, _inbox_payload)
            _redis.execute("EXPIRE", f"msg:inbox:{tenant.tenant_id}", "600")
    except Exception:
        logger.debug("inbox: failed to write msg %s to Redis (fail-open)", message_id[:15])

    # 异步处理，立刻返回 200 给飞书
    # 注意：通过闭包捕获当前 tenant，确保异步任务中上下文正确
    _tenant = tenant
    async def _dispatch_with_tenant():
        set_current_tenant(_tenant)
        try:
            await _dispatch_message(msg_type, message, message_id, sender_id, chat_id, chat_type)
        except Exception:
            logger.exception("_dispatch_with_tenant crashed (msg_type=%s, tenant=%s)", msg_type, _tenant.tenant_id)

    asyncio.create_task(_dispatch_with_tenant())
    return {"code": 0}


async def _dispatch_message(
    msg_type: str, message: dict, message_id: str, sender_id: str,
    chat_id: str = "", chat_type: str = "",
) -> None:
    """根据消息类型分发处理"""
    try:
        content_str = message.get("content", "{}")
        content = json.loads(content_str) if content_str else {}
    except json.JSONDecodeError:
        content = {}

    try:
        if msg_type == "text":
            user_text = _extract_text(content)
            if not user_text:
                return

            # ---------- 斜杠命令拦截 ----------
            cmd = user_text.strip().lower()
            logger.info("command check: text='%s' cmd='%s' sender=%s", user_text[:50], cmd, sender_id[:15])

            async def _reply_cmd(msg: str) -> None:
                await feishu_client.reply_text(message_id, msg)

            if await handle_mode_command(user_text.strip(), sender_id, _state, _reply_cmd):
                return
            if cmd == "/auth":
                logger.info("command /auth: sender=%s", sender_id[:15])
                tenant = get_current_tenant()
                redirect_uri = tenant.oauth_redirect_uri or settings.feishu.oauth_redirect_uri
                if not redirect_uri:
                    logger.warning("command /auth: oauth_redirect_uri not configured")
                    await feishu_client.reply_text(
                        message_id, "OAuth 未配置（缺少 oauth_redirect_uri）。"
                    )
                else:
                    # 总是允许重新授权（scope 可能有变更）
                    url = build_auth_url(sender_id)
                    status = "重新授权" if is_authorized(sender_id) else "授权"
                    logger.info("command /auth: returning %s url for sender=%s", status, sender_id[:15])
                    await feishu_client.reply_text(
                        message_id,
                        f"请点击链接{status}（日历+任务+消息权限）：\n\n{url}",
                    )
                return
            if cmd == "/authstatus":
                info = get_token_info(sender_id)
                if info:
                    import time as _t
                    expires_in = int(info["expires_at"] - _t.time())
                    scope = info.get("scope", "")
                    # 检查是否包含关键邮件权限
                    has_mail = any(s in scope for s in ["mail:user_mailbox", "mail:user_mailbox.message"])
                    has_folder = "mail:user_mailbox.folder" in scope
                    
                    scope_short = scope.replace(" ", "\n") if scope else "(未记录)"
                    
                    msg_lines = [
                        f"授权状态: 已授权",
                        f"用户: {info['name']}",
                        f"Token: {'有' if info['has_token'] else '无'}",
                        f"Refresh: {'有' if info['has_refresh'] else '无'}",
                        f"过期: {'已过期' if expires_in < 0 else f'{expires_in}秒后'}",
                        f"",
                        f"邮件权限: {'✓' if has_mail else '✗'}",
                        f"文件夹权限: {'✓' if has_folder else '✗'}",
                        f"",
                        f"授权 scope:",
                        scope_short,
                    ]
                    if not has_folder:
                        msg_lines.extend([
                            f"",
                            f"⚠️ 缺少邮件文件夹权限！",
                            f"请重新发送 /auth 并勾选所有邮箱权限",
                        ])
                    await feishu_client.reply_text(message_id, "\n".join(msg_lines))
                else:
                    await feishu_client.reply_text(message_id, "授权状态: 未授权\n请发 /auth 授权。")
                return
            if cmd == "/authclear":
                logger.info("command /authclear: sender=%s", sender_id[:15])
                if clear_user_token(sender_id):
                    await feishu_client.reply_text(
                        message_id,
                        "已清除授权信息。\n\n"
                        "请重新发送 /auth 进行授权。",
                    )
                else:
                    await feishu_client.reply_text(
                        message_id,
                        "没有找到授权信息（可能已过期或未授权）。\n\n"
                        "请直接发送 /auth 进行授权。",
                    )
                return
            if cmd == "/contacts":
                from app.services.user_registry import (
                    sync_org_contacts, sync_from_bot_groups,
                    all_users, last_sync_errors,
                )
                lines = []

                # 1) 通讯录同步
                try:
                    added1 = sync_org_contacts()
                    lines.append(f"通讯录同步: 新增 {added1} 人")
                except Exception as e:
                    lines.append(f"通讯录同步异常: {e}")

                # 2) 群成员 fallback
                try:
                    added2 = sync_from_bot_groups()
                    lines.append(f"群成员同步: 新增 {added2} 人")
                except Exception as e:
                    lines.append(f"群成员同步异常: {e}")

                # 3) 显示错误
                errors = last_sync_errors()
                if errors:
                    lines.append(f"\n⚠️ API 错误 ({len(errors)} 个):")
                    for err in errors[:5]:
                        lines.append(f"  {err[:150]}")
                    if len(errors) > 5:
                        lines.append(f"  ...还有 {len(errors) - 5} 个错误")

                # 4) 显示用户列表
                users = all_users()
                lines.append(f"\n共 {len(users)} 人：")
                for _, name in list(users.items())[:50]:
                    lines.append(f"  - {name}")
                if len(users) > 50:
                    lines.append("  ...")

                await feishu_client.reply_text(message_id, "\n".join(lines))
                return
            if cmd == "/status":
                await _handle_status_command(message_id, sender_id)
                return
            if cmd == "/selffix":
                # 手动触发自我诊断修复（任何人都可以触发）
                selffix_prompt = (
                    "请执行自我诊断和修复流程：\n"
                    "1. 用 get_bot_errors 查看最近的运行时错误\n"
                    "2. 用 get_deploy_logs 查看完整运行日志（比 get_bot_errors 更全面）\n"
                    "3. 如果涉及外部 API 问题，用 web_search 搜索官方文档确认行为\n"
                    "4. 分析错误原因，定位出问题的代码\n"
                    "5. 如果能修复，用 self_edit_file 做精确修改，然后 self_safe_deploy 部署\n"
                    "6. 如果不能修复，详细说明问题和建议"
                )
                _state.set_mode(sender_id, "full_access")  # 自动进入 full access
                await _enqueue_message(selffix_prompt, None, message_id, sender_id, chat_id, chat_type)
                return

            await _enqueue_message(user_text, None, message_id, sender_id, chat_id, chat_type)

        elif msg_type == "image":
            image_key = content.get("image_key", "")
            if image_key:
                data_url = await feishu_client.download_image(message_id, image_key)
                if data_url:
                    await _enqueue_message("请查看这张图片", [data_url], message_id, sender_id, chat_id, chat_type)
                else:
                    await feishu_client.reply_text(message_id, "图片下载失败，请重试。")

        elif msg_type == "file":
            await _handle_file(content, message, message_id, sender_id, chat_id, chat_type)

        elif msg_type == "post":
            user_text, image_keys, video_keys = _extract_post_content(content)
            logger.info("post extracted: text=%d chars, images=%d, videos=%d",
                        len(user_text), len(image_keys), len(video_keys))
            # 下载 post 中嵌入的图片
            image_urls: list[str] | None = None
            if image_keys:
                image_urls = []
                for key in image_keys[:5]:  # 最多 5 张，防止过多
                    data_url = await feishu_client.download_image(message_id, key)
                    if data_url:
                        image_urls.append(data_url)
                if not image_urls:
                    image_urls = None
            # 处理 post 中嵌入的视频
            if video_keys:
                logger.info("post contains %d video(s): keys=%s", len(video_keys), video_keys[:2])
                tenant = get_current_tenant()
                use_native = tenant.llm_provider == "gemini"

                for vk in video_keys[:2]:  # 最多 2 个视频
                    try:
                        file_bytes = await feishu_client.download_file(message_id, vk)
                        if not file_bytes:
                            logger.warning("post video download returned empty for key=%s msg=%s", vk, message_id)
                            continue
                    except FileTooLargeError:
                        logger.warning("post video too large for Feishu API, key=%s", vk)
                        # 不中断整个 post 处理，跳过这个视频
                        continue
                    except Exception:
                        logger.warning("post embedded video download failed for key=%s", vk, exc_info=True)
                        continue
                    try:
                        logger.info("post video downloaded: key=%s size=%dKB", vk, len(file_bytes) // 1024)

                        if use_native and len(file_bytes) <= 15 * 1024 * 1024:
                            # Gemini：直接传原始视频
                            from app.services.media_processor import media_to_data_url, detect_media_mime
                            mime = detect_media_mime(file_bytes, "video/mp4")
                            data_url = media_to_data_url(file_bytes, mime)
                        else:
                            # OpenAI 兼容 / 大视频：提取首帧
                            from app.services.media_processor import extract_video_frame, frame_to_data_url
                            frame_bytes = await extract_video_frame(file_bytes)
                            if not frame_bytes:
                                logger.warning("post video frame extraction returned empty for key=%s", vk)
                                continue
                            data_url = frame_to_data_url(frame_bytes)

                        if image_urls is None:
                            image_urls = []
                        image_urls.append(data_url)
                    except Exception:
                        logger.warning("post embedded video processing failed for key=%s", vk, exc_info=True)

                if not user_text:
                    if use_native:
                        user_text = "用户发送了一段视频，请观看并描述你看到和听到的内容"
                    else:
                        user_text = "用户发送了一段视频，这是视频的首帧画面，请描述你看到的内容"
                elif video_keys:
                    if use_native:
                        user_text += "\n\n（消息中包含视频，请一并观看）"
                    else:
                        user_text += "\n\n（消息中包含视频，已提取首帧画面供参考）"
            if user_text or image_urls:
                await _enqueue_message(
                    user_text or "请查看这些图片",
                    image_urls,
                    message_id, sender_id, chat_id, chat_type,
                )

        elif msg_type == "merge_forward":
            await _handle_merge_forward(message_id, sender_id, chat_id, chat_type)

        elif msg_type == "sticker":
            # 表情包和贴纸本质上是图片，下载后送视觉模型
            sticker_key = content.get("file_key", "")
            if sticker_key:
                data_url = await feishu_client.download_image(message_id, sticker_key)
                if data_url:
                    await _enqueue_message("用户发送了一个表情包/贴纸，请描述你看到的内容", [data_url], message_id, sender_id, chat_id, chat_type)
                else:
                    await feishu_client.reply_text(message_id, "表情包下载失败，请重试。")

        elif msg_type == "audio":
            file_key = content.get("file_key", "")
            if file_key:
                await _handle_audio(file_key, message_id, sender_id, chat_id, chat_type)
            else:
                await feishu_client.reply_text(message_id, "语音下载失败，请重试。")

        elif msg_type in ("media", "video"):
            file_key = content.get("file_key", "")
            if file_key:
                await _handle_video(file_key, message_id, sender_id, chat_id, chat_type)
            else:
                await feishu_client.reply_text(message_id, "视频下载失败，请重试。")

        else:
            await feishu_client.reply_text(
                message_id,
                f"暂不支持 {msg_type} 类型的消息，支持：文本、图片、表情包、文件、富文本、合并转发。",
            )
    except Exception as exc:
        logger.exception("_dispatch_message unhandled error (msg_type=%s)", msg_type)
        record_error("unhandled", f"_dispatch_message 异常 msg_type={msg_type}", exc=exc)
        try:
            await feishu_client.reply_text(message_id, "不好意思出了点小状况~ 你再发一遍试试？")
        except Exception:
            logger.exception("fallback reply also failed")
    finally:
        # 清除 inbox 条目：无论走命令路径还是 _process_and_reply，确保 HDEL 执行。
        # _process_and_reply 的 finally 也做了 HDEL，重复 HDEL 无害（返回 0）。
        # 这里兜底处理 /auth 等命令路径，它们 return 前不经过 _process_and_reply。
        try:
            from app.services import redis_client as _redis
            if _redis.available():
                tid = get_current_tenant().tenant_id
                _redis.execute("HDEL", f"msg:inbox:{tid}", message_id)
        except Exception:
            pass


def _extract_text(content: dict) -> str:
    """从 text 类型消息中提取文本"""
    user_text: str = content.get("text", "").strip()
    user_text = re.sub(r"@_user_\d+\s*", "", user_text).strip()
    return user_text


def _extract_post_content(content: dict) -> tuple[str, list[str], list[str]]:
    """从 post（富文本）中提取纯文本、图片 image_key 列表、视频 file_key 列表"""
    texts = []
    image_keys = []
    video_keys = []

    # 飞书 post 结构有两种变体:
    # 变体 A: {"zh_cn": {"title": "...", "content": [[...]]}}
    # 变体 B: {"post": {"zh_cn": {"title": "...", "content": [[...]]}}}  (有 post 包装层)
    root = content
    if "post" in content and isinstance(content["post"], dict):
        root = content["post"]
        logger.debug("post content: unwrapped 'post' key")

    logger.debug("post content keys: %s", list(root.keys()))

    def _walk_lang(lang_body: dict) -> None:
        title = lang_body.get("title", "")
        if title:
            texts.append(title)
        for paragraph in lang_body.get("content", []):
            if not isinstance(paragraph, list):
                continue
            for elem in paragraph:
                if not isinstance(elem, dict):
                    continue
                tag = elem.get("tag", "")
                if tag == "text":
                    texts.append(elem.get("text", ""))
                elif tag == "code_block":
                    code_text = elem.get("text", "")
                    if code_text:
                        lang = elem.get("language", "").lower()
                        if lang in ("plain_text", "plaintext", ""):
                            lang = ""
                        texts.append(f"```{lang}\n{code_text}\n```")
                elif tag == "a":
                    texts.append(elem.get("text", "") + " " + elem.get("href", ""))
                elif tag == "at":
                    # @mention - 忽略 mention 但保留可读文本
                    pass
                elif tag == "img":
                    key = elem.get("image_key", "")
                    if key:
                        image_keys.append(key)
                elif tag in ("media", "video", "file"):
                    key = elem.get("file_key", "")
                    if key:
                        video_keys.append(key)
                        logger.debug("post: found %s element, file_key=%s", tag, key[:20])

    for lang_key, lang_content in root.items():
        if isinstance(lang_content, dict) and "content" in lang_content:
            # 正常语言层: {"zh_cn": {"title": ..., "content": [...]}}
            _walk_lang(lang_content)
        elif isinstance(lang_content, dict):
            # 可能还有一层嵌套，兼容处理
            for sub_val in lang_content.values():
                if isinstance(sub_val, dict) and "content" in sub_val:
                    _walk_lang(sub_val)

    # 如果以上都没解析到，尝试把 root 本身当作语言层解析
    if not texts and not image_keys and not video_keys and "content" in root:
        logger.debug("post: fallback - treating root as lang body")
        _walk_lang(root)

    result = "\n".join(texts).strip()
    result = re.sub(r"@_user_\d+\s*", "", result).strip()

    if not result and not image_keys and not video_keys:
        # 仍然没解析到任何内容，dump 原始结构帮助排查
        logger.warning("post content empty after parsing, raw keys: %s, sample: %s",
                        list(content.keys()),
                        str(content)[:500])

    return result, image_keys, video_keys


def _truncate_user_text(text: str) -> str:
    """截断过长的用户消息，防止 context 溢出"""
    if len(text) <= _MAX_USER_TEXT_LEN:
        return text
    logger.warning("user text truncated: %d -> %d chars", len(text), _MAX_USER_TEXT_LEN)
    return text[:_MAX_USER_TEXT_LEN] + f"\n\n... (消息过长，已截断，原文共 {len(text)} 字符)"


def _truncate_reply(text: str) -> str:
    """截断过长的回复，避免飞书 API 拒绝"""
    if len(text) <= _MAX_REPLY_LEN:
        return text
    return text[:_MAX_REPLY_LEN] + f"\n\n... (回复过长已截断，共 {len(text)} 字符)"



async def _handle_file(
    content: dict, message: dict, message_id: str, sender_id: str,
    chat_id: str = "", chat_type: str = "",
) -> None:
    """处理文件附件：文本类文件读取内容，视频/图片/音频转交专用 handler"""
    file_key = content.get("file_key", "")
    file_name = content.get("file_name", "unknown")

    # 判断文件类型
    ext = ""
    if "." in file_name:
        ext = "." + file_name.rsplit(".", 1)[-1].lower()

    # 视频文件 → 转交视频 handler
    if ext in _VIDEO_EXTENSIONS:
        logger.info("file attachment is video (%s), routing to _handle_video", file_name)
        await _handle_video(file_key, message_id, sender_id, chat_id, chat_type)
        return

    # 图片文件 → 下载后作为图片处理
    if ext in _IMAGE_EXTENSIONS:
        logger.info("file attachment is image (%s), downloading as image", file_name)
        try:
            file_bytes = await feishu_client.download_file(message_id, file_key)
        except FileTooLargeError:
            await feishu_client.reply_text(message_id, f"图片文件 {file_name} 太大了，超过飞书下载限制。请压缩后重新发送。")
            return
        if file_bytes:
            from app.services.media_processor import media_to_data_url, detect_media_mime
            mime = detect_media_mime(file_bytes, "image/jpeg")
            data_url = media_to_data_url(file_bytes, mime)
            await _enqueue_message(
                f"用户发送了图片文件 {file_name}，请查看",
                [data_url], message_id, sender_id, chat_id, chat_type,
            )
        else:
            await feishu_client.reply_text(message_id, f"图片文件 {file_name} 下载失败，请重试。")
        return

    # 音频文件 → 转交音频 handler
    if ext in _AUDIO_EXTENSIONS:
        logger.info("file attachment is audio (%s), routing to _handle_audio", file_name)
        await _handle_audio(file_key, message_id, sender_id, chat_id, chat_type)
        return

    # 富文档（PDF/Word/Excel/PPT）→ 转 data URL 传给 Gemini 多模态理解
    rich_mime = _RICH_FILE_EXTS.get(ext)
    if rich_mime:
        logger.info("file attachment is rich document (%s), routing to multimodal", file_name)
        try:
            file_bytes = await feishu_client.download_file(message_id, file_key)
        except FileTooLargeError:
            await feishu_client.reply_text(message_id, f"文件 {file_name} 太大了，超过飞书下载限制。请压缩后重新发送。")
            return
        if file_bytes:
            from app.services.media_processor import media_to_data_url, detect_media_mime
            actual_mime = detect_media_mime(file_bytes, rich_mime)
            data_url = media_to_data_url(file_bytes, actual_mime)
            size_kb = len(file_bytes) // 1024
            logger.info("feishu file→multimodal: %s (%dKB, mime=%s)", file_name, size_kb, actual_mime)
            await _enqueue_message(
                f"用户发送了文件「{file_name}」({size_kb}KB)，请阅读并理解文件内容，然后回复用户。",
                [data_url], message_id, sender_id, chat_id, chat_type,
            )
        else:
            await feishu_client.reply_text(message_id, f"文件 {file_name} 下载失败，请重试。")
        return

    if ext not in _TEXT_EXTENSIONS:
        await feishu_client.reply_text(
            message_id,
            f"收到文件 {file_name}，但暂不支持此格式。支持：文本/代码、PDF、Word、Excel、PPT、图片、视频和音频。",
        )
        return

    try:
        file_bytes = await feishu_client.download_file(message_id, file_key)
    except FileTooLargeError:
        await feishu_client.reply_text(message_id, f"文件 {file_name} 太大了，超过飞书下载限制（约20MB）。")
        return
    if not file_bytes:
        await feishu_client.reply_text(message_id, f"文件 {file_name} 下载失败，请重试。")
        return

    try:
        file_text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            file_text = file_bytes.decode("gbk")
        except Exception:
            await feishu_client.reply_text(message_id, f"文件 {file_name} 编码无法识别。")
            return

    # 截断过长文件
    if len(file_text) > 30000:
        file_text = file_text[:30000] + f"\n\n... (文件被截断，总共 {len(file_bytes)} 字节)"

    user_text = f"用户发送了文件 {file_name}，内容如下：\n\n```\n{file_text}\n```"
    await _process_and_reply(user_text, None, message_id, sender_id, chat_id, chat_type)


async def _handle_audio(
    file_key: str, message_id: str, sender_id: str,
    chat_id: str = "", chat_type: str = "",
) -> None:
    """处理语音消息

    Gemini 租户：直接传原始音频给模型（原生理解语音）
    其他租户：下载 → ffmpeg 转 WAV → Whisper 转写 → 送 LLM
    """
    from app.tenant.context import get_current_tenant

    try:
        file_bytes = await feishu_client.download_file(message_id, file_key)
    except FileTooLargeError:
        logger.warning("audio file too large for Feishu download API, msg=%s", message_id)
        await feishu_client.reply_text(
            message_id,
            "语音文件太大了，超过飞书 API 的下载限制。请发送较短的语音或用文字描述。",
        )
        return
    except Exception as exc:
        logger.exception("audio download_file failed for msg=%s", message_id)
        record_error("audio", f"语音下载异常 msg={message_id}", exc=exc)
        await feishu_client.reply_text(message_id, "语音下载失败，请重试。")
        return
    if not file_bytes:
        await feishu_client.reply_text(message_id, "语音下载失败，请重试。")
        return

    tenant = get_current_tenant()

    # ── Gemini：原生音频理解，跳过 ffmpeg + Whisper ──
    if tenant.llm_provider == "gemini":
        from app.services.media_processor import media_to_data_url, detect_media_mime
        mime = detect_media_mime(file_bytes, "audio/ogg")
        # m4a 容器和 mp4 共享 ftyp 魔数，detect_media_mime 会误判为 video/mp4
        # 这里明确是音频（_handle_audio 入口），强制纠正为 audio/mp4
        if mime.startswith("video/"):
            mime = mime.replace("video/", "audio/", 1)
        data_url = media_to_data_url(file_bytes, mime)
        logger.info("audio native passthrough (gemini): %dKB mime=%s", len(file_bytes) // 1024, mime)
        await _enqueue_message(
            "[语音消息] 请听取并理解这段语音，然后回复用户",
            [data_url], message_id, sender_id, chat_id, chat_type,
        )
        return

    # ── OpenAI 兼容：ffmpeg 转 WAV → Whisper STT ──
    from app.services.media_processor import convert_voice_to_wav, transcribe_audio
    from app.config import settings

    wav_bytes = await convert_voice_to_wav(file_bytes)
    if not wav_bytes:
        await feishu_client.reply_text(message_id, "语音格式转换失败，请用文字发送你的需求。")
        return

    api_key = tenant.stt_api_key or settings.stt.api_key
    base_url = tenant.stt_base_url or settings.stt.base_url
    model = tenant.stt_model or settings.stt.model
    try:
        transcript = await transcribe_audio(wav_bytes, api_key, base_url, model)
    except Exception as exc:
        logger.exception("transcribe_audio failed for msg=%s", message_id)
        record_error("audio", f"语音转写异常 msg={message_id}", exc=exc)
        transcript = None

    if transcript:
        await _enqueue_message(f"[语音转文字] {transcript}", None, message_id, sender_id, chat_id, chat_type)
    else:
        await feishu_client.reply_text(
            message_id,
            "收到你的语音消息~\n当前语音转写服务不可用，请用文字发送你的需求！",
        )


async def _handle_video(
    file_key: str, message_id: str, sender_id: str,
    chat_id: str = "", chat_type: str = "",
) -> None:
    """处理视频消息

    Gemini 租户：直接传原始视频给模型（原生理解画面+声音）
    其他租户：下载 → ffmpeg 提取首帧 → 视觉模型分析
    """
    from app.tenant.context import get_current_tenant

    try:
        file_bytes = await feishu_client.download_file(message_id, file_key)
    except FileTooLargeError:
        logger.warning("video file too large for Feishu download API, msg=%s", message_id)
        await feishu_client.reply_text(
            message_id,
            "视频文件太大了，超过飞书 API 的下载限制（约20MB）。\n\n"
            "建议：\n"
            "1. 压缩视频后重新发送（降低分辨率或码率）\n"
            "2. 裁剪出关键片段发送\n"
            "3. 截图发给我也行，我能看图",
        )
        return
    except Exception as exc:
        logger.exception("video download_file failed for msg=%s", message_id)
        record_error("video", f"视频下载异常 msg={message_id}", exc=exc)
        await feishu_client.reply_text(message_id, "视频下载失败，请重试。")
        return
    if not file_bytes:
        await feishu_client.reply_text(message_id, "视频下载失败，请重试。")
        return

    tenant = get_current_tenant()

    # ── Gemini：原生视频理解（画面+声音），跳过 ffmpeg ──
    if tenant.llm_provider == "gemini":
        from app.services.gemini_provider import MAX_INLINE_VIDEO_SIZE
        if len(file_bytes) <= MAX_INLINE_VIDEO_SIZE:
            from app.services.media_processor import media_to_data_url, detect_media_mime
            mime = detect_media_mime(file_bytes, "video/mp4")
            data_url = media_to_data_url(file_bytes, mime)
            logger.info("video native passthrough (gemini): %dKB mime=%s", len(file_bytes) // 1024, mime)
            await _enqueue_message(
                "用户发送了一段视频，请观看并描述你看到和听到的内容",
                [data_url], message_id, sender_id, chat_id, chat_type,
            )
            return
        else:
            logger.info("video too large for inline (%dMB), falling back to frame extraction",
                        len(file_bytes) // (1024 * 1024))
            # 大视频回退到抽帧

    # ── OpenAI 兼容 / 大视频回退：ffmpeg 提取首帧 ──
    from app.services.media_processor import extract_video_frame, frame_to_data_url

    try:
        frame_bytes = await extract_video_frame(file_bytes)
    except Exception as exc:
        logger.exception("extract_video_frame failed for msg=%s", message_id)
        record_error("video", f"视频帧提取异常 msg={message_id}", exc=exc)
        frame_bytes = None
    if not frame_bytes:
        await feishu_client.reply_text(
            message_id,
            "视频帧提取失败，请用文字描述视频内容或截图发给我。",
        )
        return

    data_url = frame_to_data_url(frame_bytes)
    await _enqueue_message(
        "用户发送了一段视频，这是视频的第一帧画面，请描述你看到的内容",
        [data_url], message_id, sender_id, chat_id, chat_type,
    )


async def _handle_merge_forward(
    message_id: str, sender_id: str,
    chat_id: str = "", chat_type: str = "",
) -> None:
    """处理合并转发：尝试提取子消息文本"""
    # 合并转发的 content 是空的，需要通过 API 获取子消息
    # 飞书目前不直接暴露子消息内容，提示用户分开发送
    await feishu_client.reply_text(
        message_id,
        "收到合并转发消息。由于飞书 API 限制，我无法直接读取合并转发的内容。\n\n"
        "建议：请把关键内容复制成文字发给我，或者把聊天记录截图发给我（我能看图片）。",
    )


async def _handle_status_command(message_id: str, sender_id: str) -> None:
    """处理 /status 命令：显示 bot 运行状态"""
    import time as _t
    lines: list[str] = []

    # 1. 基本信息
    lines.append("=== Bot Status ===")

    # 2. 用户授权状态
    if is_authorized(sender_id):
        info = get_token_info(sender_id)
        expires_in = int(info["expires_at"] - _t.time()) if info else 0
        if expires_in > 0:
            lines.append(f"授权: 已授权 (token {expires_in // 60}分钟后刷新)")
        else:
            lines.append("授权: token 刷新中...")
    else:
        lines.append("授权: 未授权 (发 /auth 授权)")

    # 3. 当前模式
    mode = _state.get_mode(sender_id)
    lines.append(f"模式: {'Full Access' if mode == 'full_access' else 'Safe'}")

    # 4. 已知用户
    from app.services.user_registry import all_users
    users = all_users()
    lines.append(f"已知用户: {len(users)} 人")

    # 5. 最近错误
    try:
        from app.services.error_log import get_recent_errors
        errors = get_recent_errors(10)
        if errors:
            lines.append(f"最近错误: {len(errors)} 条")
            latest = errors[0]
            lines.append(f"  最新: [{latest.category}] {latest.summary[:80]}")
        else:
            lines.append("最近错误: 无")
    except Exception:
        lines.append("最近错误: 查询失败")

    # 6. 活跃计划
    try:
        from app.services import planner as bot_planner
        plans_ctx = bot_planner.get_active_plans_context(sender_id)
        if plans_ctx:
            plan_count = plans_ctx.count("计划:")
            lines.append(f"活跃计划: {max(plan_count, 1)} 个")
        else:
            lines.append("活跃计划: 无")
    except Exception:
        lines.append("活跃计划: 查询失败")

    # 7. 调度器
    try:
        from app.services.scheduler import _exec_lock, _in_work_hours
        in_hours = _in_work_hours()
        locked = _exec_lock.locked()
        lines.append(f"调度器: {'工作时间内' if in_hours else '非工作时间'}"
                     f"{' (执行中)' if locked else ''}")
    except Exception:
        lines.append("调度器: 状态未知")

    # 8. LLM 配置
    tenant = get_current_tenant()
    model = tenant.llm_model or settings.kimi.model
    lines.append(f"LLM: {model}")

    await feishu_client.reply_text(message_id, "\n".join(lines))


# ── 消息合并逻辑 ──

async def _enqueue_message(
    user_text: str, image_urls: list[str] | None,
    message_id: str, sender_id: str, chat_id: str, chat_type: str,
) -> None:
    """把消息加入等待队列。等一小段时间，如果没有新消息就合并处理。

    关键机制：如果 bot 正在为该用户处理消息，新消息直接进信箱（inbox），
    agent 循环会在工具调用之间检查信箱并注入上下文，而不是排队等新一轮。
    """
    # ── 如果 bot 正在处理该用户的消息，直接注入信箱 ──
    if _state.is_active(sender_id):
        inbox = _state.get_inbox(sender_id)
        if inbox is not None:
            await inbox.put({"text": user_text, "images": image_urls})
            logger.info("injected message into active session for %s: %s (images=%d)",
                        sender_id[:12], user_text[:50], len(image_urls) if image_urls else 0)
            return

    # ── 否则走正常的合并等待流程 ──
    uk = tuk(sender_id)
    if uk not in _user_pending:
        _user_pending[uk] = []

    _user_pending[uk].append({
        "text": user_text,
        "images": image_urls,
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
    })

    # 取消之前的定时器，重新计时
    old = _batch_timers.pop(uk, None)
    if old:
        old.cancel()

    _batch_timers[uk] = asyncio.create_task(
        _flush_after_wait(sender_id)
    )


async def _flush_after_wait(sender_id: str) -> None:
    """等待窗口期结束，合并文本但逐个处理媒体。

    多媒体场景（用户连发多个视频/音频）：
    - 文本全部合并（上下文完整）
    - 第一个媒体随文本一起发给 agent（初始调用）
    - 剩余媒体通过 inbox 逐个注入，agent loop 每轮处理一个
    - 避免一次性发 20MB inline_data 导致 Gemini 500
    """
    await asyncio.sleep(_BATCH_WAIT)

    uk = tuk(sender_id)
    batch = _user_pending.pop(uk, [])
    _batch_timers.pop(uk, None)
    if not batch:
        return

    # 合并文本
    texts = [m["text"] for m in batch if m["text"]]
    combined_text = "\n".join(texts)
    last = batch[-1]

    # 收集所有媒体 data URL
    all_media: list[str] = []
    for m in batch:
        if m["images"]:
            all_media.extend(m["images"])

    if len(batch) > 1:
        logger.info("batched %d messages from %s (text merged, %d media items)",
                     len(batch), sender_id, len(all_media))

    # 第一个媒体随文本一起发（初始调用）
    first_media = all_media[:1] if all_media else None
    remaining_media = all_media[1:]

    if remaining_media:
        # 告知模型后续还有更多媒体
        if combined_text:
            combined_text += f"\n[系统提示] 用户共发送了{len(all_media)}个媒体文件，正在逐个发送，请依次分析。"
        else:
            combined_text = f"[系统提示] 用户共发送了{len(all_media)}个媒体文件，正在逐个发送，请依次分析。"

    if remaining_media:
        # 用并发任务注入剩余媒体：等 session 激活后逐个放入 inbox
        async def _inject_remaining():
            # 等 session 激活（_process_and_reply 里会调 _state.activate）
            for _ in range(40):
                await asyncio.sleep(0.25)
                if _state.is_active(sender_id):
                    break
            else:
                logger.warning("timeout waiting for session to activate for %s", sender_id[:12])
                return

            inbox = _state.get_inbox(sender_id)
            if inbox is None:
                logger.warning("inbox not available for sequential media delivery to %s", sender_id[:12])
                return

            for i, media_url in enumerate(remaining_media):
                await inbox.put({
                    "text": f"[第{i+2}/{len(all_media)}个媒体文件]",
                    "images": [media_url],
                })
                logger.info("queued media %d/%d into inbox for %s",
                            i + 2, len(all_media), sender_id[:12])

        asyncio.create_task(_inject_remaining())

    await _process_and_reply(
        combined_text,
        first_media,
        last["message_id"],
        sender_id,
        last["chat_id"],
        last["chat_type"],
    )


# ── 回复拆分成多条 bubble ──

# 过短的段落合并到上一条，避免出现只有一个"好"的 bubble
_MIN_BUBBLE_LEN = 6
# bubble 最多拆几条，避免刷屏
_MAX_BUBBLES = 5


def _split_into_bubbles(text: str) -> list[str]:
    """把回复文本拆成多条 bubble。

    拆分策略：
    - 按连续空行（\\n\\n）分段
    - 过短的段落合并到前一条
    - 单段不拆（普通短回复不需要多条）
    - 最多 _MAX_BUBBLES 条，超出的合并到最后一条
    """
    raw_parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(raw_parts) <= 1:
        return [text.strip()] if text.strip() else []

    bubbles: list[str] = []
    for part in raw_parts:
        if bubbles and len(part) < _MIN_BUBBLE_LEN:
            bubbles[-1] += "\n" + part
        elif len(bubbles) >= _MAX_BUBBLES:
            # 超出上限，合并到最后一条
            bubbles[-1] += "\n\n" + part
        else:
            bubbles.append(part)
    return bubbles


async def _send_as_bubbles(reply: str, message_id: str, chat_id: str) -> None:
    """把回复拆成多条消息发送，模拟真人聊天节奏。

    第一条用 reply（引用原消息），后续条用 send_to_chat（独立 bubble）。
    如果没有 chat_id 或只有一条，就用传统的 reply 方式。
    """
    bubbles = _split_into_bubbles(reply)
    if not bubbles:
        return

    logger.info("feishu: reply to %s: %s", chat_id[:15] if chat_id else message_id[:15], reply[:200])

    # 只有一条 or 没有 chat_id → 传统方式
    if len(bubbles) == 1 or not chat_id:
        await feishu_client.reply_text(message_id, reply.strip())
        return

    # 多条 bubble：第一条 reply，后续 send_to_chat
    await feishu_client.reply_text(message_id, bubbles[0])
    for bubble in bubbles[1:]:
        # 随机间隔，模拟真人打字（0.3~0.8 秒）
        delay = 0.3 + random.random() * 0.5
        await asyncio.sleep(delay)
        await feishu_client.send_to_chat(chat_id, bubble)


async def _process_and_reply(
    user_text: str,
    image_urls: list[str] | None,
    message_id: str,
    sender_id: str,
    chat_id: str = "",
    chat_type: str = "",
) -> None:
    """获取用户名 → agent 处理 → 发送回复（同一用户串行执行）

    保护措施:
    - 截断过长的用户输入
    - 整体超时控制（_PROCESS_TIMEOUT 秒）
    - 截断过长的回复
    - 双重异常捕获，确保用户至少收到错误提示
    - in-flight 追踪：记录正在处理的请求，供部署保护和重启恢复使用
    """

    # 记录 in-flight 状态（部署保护 + shutdown 恢复）
    _in_flight[message_id] = {
        "sender_id": sender_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "tenant_id": get_current_tenant().tenant_id,
        "start_time": _time.time(),
        "text_preview": user_text[:100],
    }

    try:
        await _process_and_reply_inner(
            user_text, image_urls, message_id, sender_id, chat_id, chat_type,
        )
    finally:
        _in_flight.pop(message_id, None)
        # 清除 inbox 条目：处理完成（无论成功失败），不再需要重启恢复
        try:
            from app.services import redis_client as _redis
            if _redis.available():
                tid = get_current_tenant().tenant_id
                _redis.execute("HDEL", f"msg:inbox:{tid}", message_id)
        except Exception:
            pass


async def _process_and_reply_inner(
    user_text: str,
    image_urls: list[str] | None,
    message_id: str,
    sender_id: str,
    chat_id: str = "",
    chat_type: str = "",
) -> None:
    """实际的消息处理逻辑（被 _process_and_reply 包装以追踪 in-flight 状态）"""

    # 截断过长输入
    user_text = _truncate_user_text(user_text)

    # 拉取聊天记录作为上下文
    # - 群聊：bot 可能漏掉 @mention 间隙的消息，需要从飞书 API 补上下文
    # - 私聊：不注入 chat_context，避免和 in-memory history 重复 / 被 LLM 误归属给其他人
    #   agent 如需查看其他人的聊天记录，应主动调用 fetch_chat_history 工具
    chat_context = ""
    context_image_refs: list[dict] = []
    if chat_id and chat_type == "group":
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, fetch_chat_history, chat_id,
            )
            chat_context = result.get("text", "")
            context_image_refs = result.get("image_refs", [])
            # 权限不足等错误时 fetch_chat_history 返回 [ERROR] 字符串
            if chat_context.startswith("[ERROR]"):
                logger.warning(
                    "chat context unavailable for %s(%s): %s "
                    "— 群聊需要 im:message.group_msg 权限，请在飞书开发者后台添加",
                    chat_id[:15], chat_type, chat_context,
                )
                chat_context = ""
                context_image_refs = []
        except Exception:
            logger.warning("fetch_chat_history failed for chat_id=%s", chat_id, exc_info=True)

    # 下载聊天记录中的图片（最多 5 张），让 LLM 能真正看到历史图片
    if context_image_refs:
        history_images: list[str] = []
        for ref in context_image_refs[:5]:
            try:
                data_url = await feishu_client.download_image(
                    ref["message_id"], ref["image_key"],
                )
                if data_url:
                    history_images.append(data_url)
            except Exception:
                logger.debug("failed to download history image %s", ref.get("image_key", ""))
        if history_images:
            # 历史图片放在用户图片之前，与聊天记录中的 [图片N] 编号对应
            image_urls = history_images + (image_urls or [])
            logger.info("injected %d history images into context", len(history_images))

    async def _send_progress(text: str) -> None:
        await feishu_client.reply_text(message_id, text)

    async def _do_work() -> str:
        sender_name = await feishu_client.get_user_name(sender_id)
        if sender_name:
            register_user(sender_id, sender_name)
        elif sender_id:
            sender_name = f"用户({sender_id[:10]}...)"
        mode = _state.get_mode(sender_id)

        # 创建信箱，让 agent 循环能收到实时插入的消息
        inbox = _state.activate(sender_id)

        try:
            return await route_message(
                user_text,
                sender_id,
                sender_name,
                on_progress=_send_progress,
                image_urls=image_urls,
                mode=mode,
                chat_context=chat_context,
                chat_id=chat_id,
                chat_type=chat_type,
                inbox=inbox,
            )
        finally:
            _state.deactivate(sender_id)

    # 趁机清理不活跃的用户状态
    _state.cleanup_idle()

    async with _state.get_lock(sender_id):
        try:
            reply = await asyncio.wait_for(_do_work(), timeout=_PROCESS_TIMEOUT)
            reply = _truncate_reply(reply)
            await _send_as_bubbles(reply, message_id, chat_id)
        except RateLimitError:
            logger.warning("Kimi API rate limit hit for user %s", sender_id)
            record_error("api_error", "Kimi API 每日 token 上限触发 RateLimitError")
            await feishu_client.reply_text(
                message_id,
                "今日 AI 额度已用完（Kimi API 每日 token 上限），请明天再试。\n"
                "如果急需使用，可以联系管理员升级 API 额度。",
            )
        except asyncio.TimeoutError:
            logger.error("processing timed out after %ds for sender=%s", _PROCESS_TIMEOUT, sender_id)
            record_error(
                "timeout",
                f"消息处理超时 ({_PROCESS_TIMEOUT}s) sender={sender_id} text={user_text[:200]}",
            )
            try:
                from app.services.base_agent import build_timeout_message
                await feishu_client.reply_text(message_id, build_timeout_message())
            except Exception:
                logger.exception("timeout reply failed")
        except Exception as exc:
            logger.exception("failed to process message")
            record_error(
                "unhandled",
                f"消息处理异常 sender={sender_id} text={user_text[:200]}",
                exc=exc,
            )
            try:
                await feishu_client.reply_text(
                    message_id, "不好意思出了点小状况~ 你再发一遍试试？"
                )
            except Exception:
                logger.exception("error reply failed")
