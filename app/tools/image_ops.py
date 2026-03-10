"""AI 画图工具 —— Gemini native image generation

使用 Gemini 的 response_modalities=["IMAGE"] 能力生成图片，
生成后通过平台 API 直接发送给用户（企微客服/企微内部）。
飞书平台通过上传 image_key 后发送图片消息。
"""

from __future__ import annotations

import io
import logging
import os
import time

import httpx

from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── 企微 API ──
_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_UPLOAD_URL = "https://qyapi.weixin.qq.com/cgi-bin/media/upload"
_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
_KF_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg"

# 飞书 API
_FEISHU_UPLOAD_IMAGE_URL = "https://open.feishu.cn/open-apis/im/v1/images"
_FEISHU_SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

# token 缓存
_token_cache: dict[str, tuple[str, float]] = {}


def _get_wecom_token(corpid: str, secret: str) -> str:
    ck = f"{corpid}:{secret[:8]}"
    cached = _token_cache.get(ck)
    if cached and time.time() < cached[1]:
        return cached[0]
    with httpx.Client(timeout=10, trust_env=False) as client:
        resp = client.get(_TOKEN_URL, params={"corpid": corpid, "corpsecret": secret})
        data = resp.json()
    if data.get("errcode", -1) != 0:
        raise RuntimeError(f"wecom token error: {data}")
    token = data["access_token"]
    _token_cache[ck] = (token, time.time() + data.get("expires_in", 7200) - 300)
    return token


async def _generate_image_bytes(prompt: str) -> tuple[bytes, str] | None:
    """调用 Gemini 生图，返回 (图片字节, mime_type) 或 None"""
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        logger.warning("image_ops: google-genai not installed")
        return None

    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    if not tenant.llm_api_key:
        logger.warning("image_ops: no Gemini API key")
        return None

    http_options: dict = {"timeout": 120_000}
    _custom_base = tenant.llm_base_url
    if _custom_base and "moonshot" not in _custom_base and "openai.com" not in _custom_base:
        http_options["base_url"] = _custom_base
    env_base = os.getenv("GOOGLE_GEMINI_BASE_URL", "")
    if not http_options.get("base_url") and env_base:
        http_options["base_url"] = env_base
    proxy_url = os.getenv("GEMINI_PROXY", "")
    if proxy_url:
        http_options["async_client_args"] = {"proxy": proxy_url}

    client = genai.Client(api_key=tenant.llm_api_key, http_options=http_options)

    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                temperature=1.0,
            ),
        )

        if not response.candidates:
            logger.warning("image_ops: Gemini returned no candidates")
            return None

        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type and part.inline_data.mime_type.startswith("image/"):
                logger.info("image_ops: generated image (%d bytes, %s)",
                            len(part.inline_data.data), part.inline_data.mime_type)
                return bytes(part.inline_data.data), part.inline_data.mime_type

        logger.warning("image_ops: Gemini response had no image parts")
        return None
    except Exception:
        logger.exception("image_ops: Gemini image generation failed")
        return None


def _upload_wecom_image(token: str, image_bytes: bytes, filename: str) -> str:
    """上传图片到企微临时素材，返回 media_id"""
    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.post(
            _UPLOAD_URL,
            params={"access_token": token, "type": "image"},
            files={"media": (filename, io.BytesIO(image_bytes), "image/png")},
        )
        data = resp.json()
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"wecom image upload failed: {data}")
    return data.get("media_id", "")


def _send_image_wecom(token: str, userid: str, media_id: str, agent_id: int) -> dict:
    """企微内部应用发送图片"""
    body = {
        "touser": userid,
        "msgtype": "image",
        "agentid": agent_id,
        "image": {"media_id": media_id},
    }
    with httpx.Client(timeout=10, trust_env=False) as client:
        resp = client.post(_SEND_URL, params={"access_token": token}, json=body)
        return resp.json()


def _send_image_wecom_kf(token: str, external_userid: str, media_id: str, open_kfid: str) -> dict:
    """微信客服发送图片"""
    body = {
        "touser": external_userid,
        "open_kfid": open_kfid,
        "msgtype": "image",
        "image": {"media_id": media_id},
    }
    with httpx.Client(timeout=10, trust_env=False) as client:
        resp = client.post(_KF_SEND_URL, params={"access_token": token}, json=body)
        return resp.json()


def _upload_feishu_image(tenant_token: str, image_bytes: bytes, filename: str) -> str:
    """上传图片到飞书，返回 image_key"""
    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.post(
            _FEISHU_UPLOAD_IMAGE_URL,
            headers={"Authorization": f"Bearer {tenant_token}"},
            data={"image_type": "message"},
            files={"image": (filename, io.BytesIO(image_bytes), "image/png")},
        )
        data = resp.json()
    if data.get("code", -1) != 0:
        raise RuntimeError(f"feishu image upload failed: {data}")
    return data.get("data", {}).get("image_key", "")


def _send_image_feishu(tenant_token: str, chat_id: str, image_key: str) -> dict:
    """飞书发送图片消息"""
    import json
    with httpx.Client(timeout=10, trust_env=False) as client:
        resp = client.post(
            _FEISHU_SEND_URL,
            headers={"Authorization": f"Bearer {tenant_token}"},
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}),
            },
        )
        return resp.json()


async def generate_image(prompt: str = "", **kwargs) -> str | ToolResult:
    """生成图片并发送给用户。

    使用 Gemini AI 根据文字描述生成图片，生成后直接发送给用户。
    支持企微客服、企微内部应用和飞书平台。
    """
    if not prompt:
        return ToolResult.invalid_param("请提供图片描述（prompt 参数）")

    from app.tenant.context import get_current_tenant
    from app.tools.feishu_api import _current_user_open_id

    tenant = get_current_tenant()
    platform = tenant.platform
    sender_id = _current_user_open_id.get("")

    if not sender_id:
        return ToolResult.error("无法确定当前用户", code="internal")

    # 生成图片
    result = await _generate_image_bytes(prompt)
    if not result:
        return ToolResult.error(
            "图片生成失败。可能原因：Gemini API 不可用、提示词被安全策略拒绝、或网络问题。请换个描述试试。",
            code="api_error",
        )

    image_bytes, mime_type = result
    ext = "png" if "png" in mime_type else "jpg"
    filename = f"generated_image.{ext}"

    try:
        if platform == "wecom_kf":
            token = _get_wecom_token(tenant.wecom_corpid, tenant.wecom_kf_secret)
            media_id = _upload_wecom_image(token, image_bytes, filename)
            send_result = _send_image_wecom_kf(token, sender_id, media_id, tenant.wecom_kf_open_kfid)
        elif platform == "wecom":
            token = _get_wecom_token(tenant.wecom_corpid, tenant.wecom_corpsecret)
            media_id = _upload_wecom_image(token, image_bytes, filename)
            send_result = _send_image_wecom(token, sender_id, media_id, tenant.wecom_agent_id)
        elif platform == "feishu":
            from app.services.feishu import FeishuClient
            fs = FeishuClient(tenant.feishu_app_id, tenant.feishu_app_secret)
            tenant_token = await fs._get_token()
            image_key = _upload_feishu_image(tenant_token, image_bytes, filename)
            # 需要 chat_id，从 kwargs 或 context 获取
            chat_id = kwargs.get("chat_id", "")
            if not chat_id:
                # 尝试从 feishu_api 模块获取 p2p chat_id
                from app.services.user_registry import get_p2p_chat_id
                chat_id = get_p2p_chat_id(sender_id)
            if not chat_id:
                return ToolResult.error(
                    "飞书平台发送图片需要 chat_id，但当前未找到对应的聊天窗口。",
                    code="internal",
                )
            send_result = _send_image_feishu(tenant_token, chat_id, image_key)
            if send_result.get("code", -1) != 0:
                return ToolResult.error(f"图片发送失败: {send_result}", code="api_error")
            size_kb = len(image_bytes) / 1024
            return ToolResult.success(
                f"✅ 图片已生成并发送（{size_kb:.0f}KB）。\n描述: {prompt[:100]}"
            )
        else:
            return ToolResult.error(f"平台 {platform} 暂不支持图片发送", code="invalid_param")

        # wecom / wecom_kf 结果检查
        if send_result.get("errcode", -1) != 0:
            return ToolResult.error(f"图片发送失败: {send_result}", code="api_error")

        size_kb = len(image_bytes) / 1024
        return ToolResult.success(
            f"✅ 图片已生成并发送（{size_kb:.0f}KB）。\n描述: {prompt[:100]}"
        )

    except Exception as e:
        logger.exception("image_ops: send image failed")
        return ToolResult.error(f"图片发送失败: {e}", code="api_error")


# ── 工具定义 ──

TOOL_DEFINITIONS = [
    {
        "name": "generate_image",
        "description": (
            "使用 AI 生成图片并发送给用户。"
            "根据文字描述（prompt）生成对应的图片。"
            "适用于：画图、生成插画、创建配图、设计素材等场景。"
            "提示词越详细，生成效果越好（建议用英文描述以获得更好效果）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "图片描述/提示词。描述你想生成的图片内容，越详细越好。"
                        "建议包含：主题、风格、色调、构图等。"
                        "例如：'A cute orange cat sitting on a windowsill, watercolor style, warm afternoon light'"
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
]

TOOL_MAP = {
    "generate_image": generate_image,
}
