"""QQ 机器人 Webhook 事件处理

QQ 开放平台 API v2 webhook:
- 所有事件 POST 到统一回调地址
- op=13: Ed25519 验证 challenge
- op=0, t=C2C_MESSAGE_CREATE: 单聊消息
- op=0, t=GROUP_AT_MESSAGE_CREATE: 群 @消息
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.channels.qq import QQChannel
from app.router.intent import route_message
from app.services import qq as qq_api
from app.services.error_log import record_error
from app.tenant.context import (
    get_current_tenant, set_current_tenant, set_current_channel, set_current_sender,
)
from app.tenant.registry import tenant_registry
from app.webhook.base import (
    MessageDedup, UserStateManager, tuk, split_reply,
    handle_mode_command,
    DEFAULT_PROCESS_TIMEOUT,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ── 共享基础设施 ──
_dedup = MessageDedup(max_cache=2048, ttl=600)
_state = UserStateManager(mode_ttl=7200, lock_idle_ttl=3600)
_channel = QQChannel()

_PROCESS_TIMEOUT = DEFAULT_PROCESS_TIMEOUT


# ── Ed25519 签名验证 ──

def _ed25519_sign(token: str, event_ts: str, plain_token: str) -> str:
    """QQ webhook Ed25519 签名。

    1. token 重复填充到 32 字节作为 seed
    2. 用 seed 生成 Ed25519 私钥
    3. sign(event_ts + plain_token)
    4. 返回 hex 签名
    """
    try:
        from nacl.signing import SigningKey
    except ImportError:
        logger.error("PyNaCl not installed — cannot verify QQ webhook")
        return ""

    # seed: repeat token bytes to fill 32 bytes
    token_bytes = token.encode("utf-8")
    if not token_bytes:
        return ""
    seed = (token_bytes * (32 // len(token_bytes) + 1))[:32]

    signing_key = SigningKey(seed)
    message = (event_ts + plain_token).encode("utf-8")
    signed = signing_key.sign(message)
    # signed.signature 是 64 字节签名
    return signed.signature.hex()


# ── Webhook 路由 ──

@router.post("/webhook/qq/{tenant_id}")
async def qq_callback(tenant_id: str, request: Request) -> Response:
    """QQ 机器人 webhook 回调入口"""
    tenant = tenant_registry.get(tenant_id)
    if not tenant:
        logger.warning("qq: unknown tenant_id: %s", tenant_id)
        return Response("unknown tenant", status_code=404)

    set_current_tenant(tenant)
    ch = tenant.get_channel("qq")
    if ch:
        set_current_channel(ch)

    try:
        body = await request.json()
    except Exception:
        return Response("invalid json", status_code=400)

    op = body.get("op")

    # ── op=13: 验证 challenge ──
    if op == 13:
        return _handle_validation(body, tenant)

    # ── op=0: 事件分发 ──
    if op == 0:
        event_type = body.get("t", "")
        if event_type in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            asyncio.create_task(_dispatch_message(tenant, body))
        else:
            logger.debug("qq: ignoring event type=%s tenant=%s", event_type, tenant_id)
        return JSONResponse({"code": 0})

    return JSONResponse({"code": 0})


def _handle_validation(body: dict, tenant) -> JSONResponse:
    """处理 QQ webhook 验证 challenge (op=13)。"""
    d = body.get("d", {})
    plain_token = d.get("plain_token", "")
    event_ts = d.get("event_ts", "")

    _, _, qq_token = qq_api._get_credentials()
    if not qq_token:
        logger.warning("qq: missing qq_token for tenant=%s, cannot verify", tenant.tenant_id)
        return JSONResponse({"plain_token": plain_token, "signature": ""})

    signature = _ed25519_sign(qq_token, event_ts, plain_token)
    logger.info("qq: challenge verified for tenant=%s", tenant.tenant_id)
    return JSONResponse({"plain_token": plain_token, "signature": signature})


# ── 消息处理 ──

async def _dispatch_message(tenant, payload: dict) -> None:
    """异步分发消息到 LLM 处理。"""
    set_current_tenant(tenant)
    ch = tenant.get_channel("qq")
    if ch:
        set_current_channel(ch)

    msg = _channel.parse_event(payload)
    if not msg:
        return

    # 去重
    if _dedup.is_duplicate(msg.event_id):
        logger.debug("qq: duplicate event_id=%s, skipping", msg.event_id)
        return

    text = _channel.extract_text(msg)

    # 提取附件（图片/视频/语音/文件）
    attachments = _channel.extract_attachments(msg)
    image_urls: list[str] | None = None

    if attachments:
        image_atts = [a for a in attachments if a["type"] == "image"]
        if image_atts:
            image_urls = []
            for att in image_atts[:5]:  # 最多 5 张
                data_url = await qq_api.download_image_url(att["url"])
                if data_url:
                    image_urls.append(data_url)
            if not image_urls:
                image_urls = None

        # 非图片附件：在文本中追加占位符，让 LLM 知道有附件
        for att in attachments:
            if att["type"] == "video":
                text += f"\n[视频: {att.get('filename', '视频文件')}]"
            elif att["type"] == "audio":
                text += f"\n[语音消息]"
            elif att["type"] == "file":
                text += f"\n[文件: {att.get('filename', '文件')}]"

    if not text and not image_urls:
        logger.debug("qq: empty text and no media, skipping")
        return

    sender_id = msg.sender_id
    set_current_sender(sender_id)

    logger.info(
        "qq: message from %s chat=%s text=%s images=%d",
        sender_id[:8], msg.chat_type, text[:100],
        len(image_urls) if image_urls else 0,
    )

    # 模式命令
    async def _reply_fn(t: str) -> None:
        await _channel.reply_text(msg.message_id, t)

    if text and await handle_mode_command(text.strip(), sender_id, _state, _reply_fn):
        return

    # 处理消息
    await _process_and_reply(
        text, msg.message_id, sender_id, msg.chat_id, msg.chat_type,
        image_urls=image_urls,
    )


async def _process_and_reply(
    user_text: str,
    message_id: str,
    sender_id: str,
    chat_id: str,
    chat_type: str,
    *,
    image_urls: list[str] | None = None,
) -> None:
    """获取回复并发送。"""
    is_group = chat_type == "group"

    # 解包 message_id 获取实际 chat_id 和 msg_id
    parts = message_id.split(":", 2)
    qq_chat_id = parts[1] if len(parts) >= 2 else ""
    qq_msg_id = parts[2] if len(parts) >= 3 else ""

    async def _send_progress(text: str) -> None:
        await qq_api.reply_text(qq_chat_id, qq_msg_id, text, is_group=is_group)

    async def _do_work() -> str:
        sender_name = await _channel.get_user_name(sender_id)
        mode = _state.get_mode(sender_id)

        return await route_message(
            user_text,
            sender_id,
            sender_name,
            on_progress=_send_progress,
            mode=mode,
            chat_id=chat_id,
            chat_type=chat_type,
            image_urls=image_urls,
        )

    _state.cleanup_idle()

    async with _state.get_lock(sender_id):
        try:
            reply = await asyncio.wait_for(_do_work(), timeout=_PROCESS_TIMEOUT)
            if not reply:
                return

            # QQ 消息长度限制，分段发送
            chunks = split_reply(reply, max_len=_channel.max_message_length)
            for chunk in chunks:
                await qq_api.reply_text(qq_chat_id, qq_msg_id, chunk, is_group=is_group)
                if len(chunks) > 1:
                    await asyncio.sleep(0.5)

        except asyncio.TimeoutError:
            logger.error("qq: processing timed out for sender=%s", sender_id)
            record_error("timeout", f"QQ 消息处理超时 sender={sender_id} text={user_text[:200]}")
            try:
                await qq_api.reply_text(
                    qq_chat_id, qq_msg_id,
                    f"处理超时（超过 {_PROCESS_TIMEOUT} 秒），请简化消息后重试。",
                    is_group=is_group,
                )
            except Exception:
                logger.exception("qq: timeout reply failed")

        except Exception as exc:
            logger.exception("qq: failed to process message")
            record_error("unhandled", f"QQ 消息处理异常 sender={sender_id}", exc=exc)
            try:
                await qq_api.reply_text(
                    qq_chat_id, qq_msg_id,
                    "处理消息时出错，请稍后再试。",
                    is_group=is_group,
                )
            except Exception:
                logger.exception("qq: error reply failed")
