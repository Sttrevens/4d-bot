"""企微 Webhook 事件处理

企微回调协议:
- GET: URL 验证 (echostr 解密返回)
- POST: 消息/事件推送 (加密 XML)

消息格式与飞书不同：
- 企微用 XML + AES 加密 (飞书用 JSON)
- 用户标识是 userid (飞书用 open_id)
- 没有「回复」概念，只能主动发应用消息 (飞书有 reply API)
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Request, Response

from app.services.wecom import wecom_client
from app.services.wecom_crypto import decrypt_callback, verify_signature, decrypt
from app.tenant.context import get_current_tenant, set_current_tenant, set_current_channel
from app.tenant.registry import tenant_registry
from app.services.error_log import record_error
from app.webhook.base import (
    MessageDedup, UserStateManager,
    split_reply, strip_markdown, handle_mode_command, handle_status_command,
    DEFAULT_PROCESS_TIMEOUT, DEFAULT_MAX_USER_TEXT_LEN,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ── 共享基础设施 ──
_dedup = MessageDedup(max_cache=1024)
_state = UserStateManager()

_PROCESS_TIMEOUT = DEFAULT_PROCESS_TIMEOUT
_MAX_USER_TEXT_LEN = DEFAULT_MAX_USER_TEXT_LEN
_MAX_REPLY_LEN = 2000
_MAX_REPLY_BYTES = 3800  # 企微 API text 字段字节限制（~4096 减去 JSON 包装开销）


# ── 路由 ──

@router.get("/webhook/wecom/{tenant_id}")
async def wecom_verify(tenant_id: str, request: Request) -> Response:
    """企微回调 URL 验证 (GET)"""
    tenant = tenant_registry.get(tenant_id)
    if not tenant or not tenant.has_platform("wecom"):
        return Response("unknown tenant", status_code=404)

    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    echostr = request.query_params.get("echostr", "")

    if not all([msg_signature, timestamp, nonce, echostr]):
        return Response("missing params", status_code=400)

    # 验证签名 + 解密 echostr
    if not verify_signature(tenant.wecom_token, timestamp, nonce, echostr, msg_signature):
        logger.warning("wecom verify failed: signature mismatch for tenant=%s", tenant_id)
        return Response("signature error", status_code=403)

    try:
        plaintext, _ = decrypt(tenant.wecom_encoding_aes_key, echostr)
        logger.info("wecom URL verification OK for tenant=%s", tenant_id)
        return Response(plaintext, media_type="text/plain")
    except Exception:
        logger.exception("wecom echostr decrypt failed for tenant=%s", tenant_id)
        return Response("decrypt error", status_code=500)


@router.post("/webhook/wecom/{tenant_id}")
async def wecom_callback(tenant_id: str, request: Request) -> Response:
    """企微消息/事件回调 (POST)"""
    tenant = tenant_registry.get(tenant_id)
    if not tenant or not tenant.has_platform("wecom"):
        return Response("unknown tenant", status_code=404)

    set_current_tenant(tenant)
    ch = tenant.get_channel("wecom")
    if ch:
        set_current_channel(ch)

    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")

    raw_body = await request.body()
    body_str = raw_body.decode("utf-8")

    try:
        decrypted_xml = decrypt_callback(
            token=tenant.wecom_token,
            encoding_aes_key=tenant.wecom_encoding_aes_key,
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            post_data=body_str,
        )
    except Exception:
        logger.exception("wecom callback decrypt failed for tenant=%s", tenant_id)
        return Response("decrypt error", status_code=403)

    # 解析 XML
    try:
        root = ET.fromstring(decrypted_xml)
    except ET.ParseError:
        logger.error("wecom callback: invalid XML after decrypt")
        return Response("ok")

    msg_type = _xml_text(root, "MsgType")
    logger.info("wecom callback: tenant=%s msg_type=%s", tenant_id, msg_type)

    # 异步处理，立刻返回空串给企微（企微要求 200 + 空 body 或 "success"）
    _tenant = tenant
    asyncio.create_task(_dispatch_with_tenant(_tenant, root, msg_type))
    return Response("success", media_type="text/plain")


# ── 消息处理 ──

async def _dispatch_with_tenant(tenant, root: ET.Element, msg_type: str) -> None:
    """设置租户上下文后分发消息"""
    set_current_tenant(tenant)
    ch = tenant.get_channel("wecom")
    if ch:
        set_current_channel(ch)
    try:
        await _dispatch_message(root, msg_type)
    except Exception as exc:
        logger.exception("wecom _dispatch_message error")
        record_error("unhandled", f"wecom dispatch error: {exc}", exc=exc)


async def _dispatch_message(root: ET.Element, msg_type: str) -> None:
    """根据消息类型分发处理"""
    from_user = _xml_text(root, "FromUserName")  # userid
    msg_id = _xml_text(root, "MsgId")

    # 去重
    if _dedup.is_duplicate(msg_id):
        return

    if msg_type == "text":
        content = _xml_text(root, "Content")
        if not content:
            return
        await _handle_text(from_user, content)

    elif msg_type == "image":
        # 企微图片消息有 PicUrl 和 MediaId
        pic_url = _xml_text(root, "PicUrl")
        if pic_url:
            await _handle_text(from_user, f"[用户发送了一张图片: {pic_url}]")

    elif msg_type == "voice":
        media_id = _xml_text(root, "MediaId")
        if media_id:
            await _handle_voice(from_user, media_id)
        else:
            await wecom_client.reply_text(from_user, "语音接收失败，请重新发送~")

    elif msg_type == "event":
        event_type = _xml_text(root, "Event")
        await _handle_event(from_user, event_type, root)

    else:
        await wecom_client.reply_text(
            from_user,
            f"暂不支持 {msg_type} 类型消息，目前支持文本、图片和语音。"
        )


async def _handle_text(userid: str, text: str) -> None:
    """处理文本消息"""
    text = text.strip()
    if not text:
        return

    async def _reply(msg: str) -> None:
        await wecom_client.reply_text(userid, msg)

    # 斜杠命令
    if await handle_mode_command(text, userid, _state, _reply):
        return

    if text.strip().lower() == "/status":
        await handle_status_command(userid, _state, "企微", _reply)
        return

    # 正常消息 → agent 处理
    await _process_and_reply(userid, text)


async def _handle_voice(userid: str, media_id: str) -> None:
    """下载语音 → ffmpeg 转 WAV → Whisper 转写 → 送 LLM"""
    from app.services.media_processor import convert_voice_to_wav, transcribe_audio
    from app.config import settings
    from app.tenant.context import get_current_tenant

    content_bytes, _ = await wecom_client.download_media(media_id)
    if not content_bytes:
        await wecom_client.reply_text(userid, "语音下载失败，请重新发送~")
        return

    wav_bytes = await convert_voice_to_wav(content_bytes)
    if not wav_bytes:
        await wecom_client.reply_text(userid, "语音格式转换失败，请用文字发送你的需求~")
        return

    tenant = get_current_tenant()
    api_key = tenant.stt_api_key or settings.stt.api_key
    base_url = tenant.stt_base_url or settings.stt.base_url
    model = tenant.stt_model or settings.stt.model
    transcript = await transcribe_audio(wav_bytes, api_key, base_url, model)

    if transcript:
        await _handle_text(userid, f"[语音转文字] {transcript}")
    else:
        await wecom_client.reply_text(
            userid,
            "收到你的语音消息~\n当前语音转写服务不可用，请用文字发送你的需求！"
        )


async def _handle_event(userid: str, event_type: str, root: ET.Element) -> None:
    """处理事件（关注/进入应用等）"""
    if event_type == "subscribe":
        await wecom_client.reply_text(
            userid,
            "你好！我是你的智能工作助手。\n"
            "发消息给我，我可以帮你处理工作任务。\n"
            "发 /mode 查看当前模式。",
        )
    elif event_type == "enter_agent":
        # 用户进入应用主页，可以不回复
        logger.info("wecom: user %s entered agent", userid)
    else:
        logger.debug("wecom: unhandled event type=%s", event_type)


async def _process_and_reply(userid: str, text: str) -> None:
    """agent 处理 + 发送回复"""
    if len(text) > _MAX_USER_TEXT_LEN:
        text = text[:_MAX_USER_TEXT_LEN] + "\n(消息过长已截断)"

    async with _state.get_lock(userid):
        try:
            reply = await asyncio.wait_for(
                _do_agent_work(userid, text),
                timeout=_PROCESS_TIMEOUT,
            )

            display_reply = strip_markdown(reply) if reply else reply
            for chunk in split_reply(display_reply, _MAX_REPLY_LEN, max_bytes=_MAX_REPLY_BYTES):
                await wecom_client.reply_text(userid, chunk)

        except asyncio.TimeoutError:
            logger.error("wecom: processing timeout for user=%s", userid)
            record_error("timeout", f"wecom message timeout user={userid}")
            await wecom_client.reply_text(userid, "处理超时，请简化消息后重试。")
        except Exception as exc:
            logger.exception("wecom: process error for user=%s", userid)
            record_error("unhandled", f"wecom process error user={userid}", exc=exc)
            await wecom_client.reply_text(userid, "处理消息时出错，请稍后重试。")


async def _do_agent_work(userid: str, text: str) -> str:
    """调用 LLM agent 处理消息"""
    sender_name = await wecom_client.get_user_name(userid)
    if sender_name:
        from app.services.user_registry import register as register_user
        register_user(userid, sender_name)
    elif userid:
        sender_name = f"用户({userid[:10]})"

    # 设置 _current_user_open_id，让工具层能获取当前用户 ID
    from app.tools.feishu_api import _current_user_open_id
    _current_user_open_id.set(userid)

    mode = _state.get_mode(userid)

    async def _send_progress(msg: str) -> None:
        msg = strip_markdown(msg)
        for chunk in split_reply(msg, _MAX_REPLY_LEN, max_bytes=_MAX_REPLY_BYTES):
            await wecom_client.reply_text(userid, chunk)

    from app.router.intent import route_message
    return await route_message(
        user_text=text,
        sender_id=userid,
        sender_name=sender_name,
        on_progress=_send_progress,
        mode=mode,
    )


def _xml_text(root: ET.Element, tag: str) -> str:
    """从 XML element 中安全提取文本"""
    elem = root.find(tag)
    return elem.text.strip() if elem is not None and elem.text else ""
