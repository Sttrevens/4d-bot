"""QQ 机器人 API 客户端

QQ 开放平台 API v2:
- 鉴权: POST https://bots.qq.com/app/getAppAccessToken → access_token (7200s)
- API: https://api.sgroup.qq.com, Authorization: QQBot {token}
- 发消息: POST /v2/users/{openid}/messages (C2C) / /v2/groups/{group_openid}/messages (群)
- 富媒体: POST /v2/users/{openid}/files (C2C) / /v2/groups/{group_openid}/files (群)

⚠️ QQ API 服务器在国内，直连即可，httpx 必须 trust_env=False。
"""

from __future__ import annotations

import base64
import io
import logging
import time
from typing import Any

import httpx

from app.tenant.context import get_current_tenant, get_current_channel

logger = logging.getLogger(__name__)

_API_BASE = "https://api.sgroup.qq.com"
_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# access_token 缓存: {app_id: (token, expire_ts)}
_token_cache: dict[str, tuple[str, float]] = {}


def _get_credentials() -> tuple[str, str, str]:
    """从当前 tenant/channel 上下文获取 QQ 凭证。返回 (app_id, app_secret, token)。"""
    ch = get_current_channel()
    if ch and ch.qq_app_id:
        return ch.qq_app_id, ch.qq_app_secret, ch.qq_token
    t = get_current_tenant()
    return t.qq_app_id, t.qq_app_secret, t.qq_token


async def _get_access_token() -> str:
    """获取 QQ API access_token（带缓存，自动刷新）。"""
    app_id, app_secret, _ = _get_credentials()
    if not app_id or not app_secret:
        logger.warning("qq: missing app_id or app_secret")
        return ""

    cached = _token_cache.get(app_id)
    if cached and cached[1] > time.time() + 60:  # 60s 提前刷新
        return cached[0]

    try:
        async with httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(connect=5.0, read=10.0),
        ) as client:
            resp = await client.post(_TOKEN_URL, json={
                "appId": app_id,
                "clientSecret": app_secret,
            })
            resp.raise_for_status()
            data = resp.json()
            token = data.get("access_token", "")
            expires_in = int(data.get("expires_in", 7200))
            if token:
                _token_cache[app_id] = (token, time.time() + expires_in)
                logger.info("qq: refreshed access_token for app_id=%s, expires_in=%d", app_id, expires_in)
            return token
    except Exception:
        logger.warning("qq: failed to get access_token", exc_info=True)
        return ""


async def _request(method: str, path: str, **kwargs: Any) -> dict:
    """通用 QQ API 请求。"""
    token = await _get_access_token()
    if not token:
        return {"error": "no access_token"}

    url = f"{_API_BASE}{path}"
    headers = {"Authorization": f"QQBot {token}"}

    try:
        async with httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(connect=5.0, read=30.0),
        ) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
            if resp.status_code >= 400:
                logger.warning("qq API error: %s %s → %d %s", method, path, resp.status_code, resp.text[:500])
                return {"error": resp.text, "status": resp.status_code}
            return resp.json() if resp.content else {}
    except Exception as e:
        logger.warning("qq API request failed: %s %s → %s", method, path, e)
        return {"error": str(e)}


# ── 发送消息 ──

async def send_group_message(
    group_openid: str, content: str, msg_id: str = "",
    *, msg_type: int = 0, media: dict | None = None,
    markdown: dict | None = None,
) -> dict:
    """发送群消息。msg_id 非空则为被动回复（5 分钟内有效，最多 5 条）。

    msg_type: 0=文本, 2=Markdown, 3=Ark, 7=富媒体(图片/视频/语音/文件)
    """
    body: dict[str, Any] = {"msg_type": msg_type, "content": content}
    if msg_id:
        body["msg_id"] = msg_id
    if media:
        body["media"] = media
    if markdown:
        body["markdown"] = markdown
        body["msg_type"] = 2
    return await _request("POST", f"/v2/groups/{group_openid}/messages", json=body)


async def send_c2c_message(
    openid: str, content: str, msg_id: str = "",
    *, msg_type: int = 0, media: dict | None = None,
    markdown: dict | None = None,
) -> dict:
    """发送单聊消息。msg_id 非空则为被动回复（60 分钟内有效，最多 5 条）。

    msg_type: 0=文本, 2=Markdown, 3=Ark, 7=富媒体(图片/视频/语音/文件)
    """
    body: dict[str, Any] = {"msg_type": msg_type, "content": content}
    if msg_id:
        body["msg_id"] = msg_id
    if media:
        body["media"] = media
    if markdown:
        body["markdown"] = markdown
        body["msg_type"] = 2
    return await _request("POST", f"/v2/users/{openid}/messages", json=body)


async def reply_text(
    chat_id: str,
    msg_id: str,
    text: str,
    *,
    is_group: bool = False,
) -> dict:
    """统一回复接口。chat_id 是 openid (C2C) 或 group_openid (群)。"""
    if is_group:
        return await send_group_message(chat_id, text, msg_id=msg_id)
    return await send_c2c_message(chat_id, text, msg_id=msg_id)


async def send_to_chat(
    chat_id: str,
    text: str,
    *,
    is_group: bool = False,
) -> dict:
    """主动发消息（无 msg_id）。注意 QQ 限制：主动推送能力已于 2025-04 停用。"""
    if is_group:
        return await send_group_message(chat_id, text)
    return await send_c2c_message(chat_id, text)


# ── 富媒体上传 ──

async def upload_media(
    chat_id: str,
    *,
    is_group: bool = False,
    file_type: int = 1,
    url: str = "",
    file_data: bytes | None = None,
    srv_send_msg: bool = False,
) -> dict:
    """上传富媒体资源，获取 file_info 用于发送。

    file_type: 1=图片, 2=视频, 3=语音, 4=文件
    url: 外部资源 URL（优先）
    file_data: 本地文件字节（url 为空时使用 base64 编码上传）
    srv_send_msg: True=上传并直接发送, False=仅上传获取 file_info

    返回: {"file_uuid": "...", "file_info": "...", "ttl": 259200}
    file_info 有效期 3 天（259200 秒），过期需重新上传。
    """
    if is_group:
        path = f"/v2/groups/{chat_id}/files"
    else:
        path = f"/v2/users/{chat_id}/files"

    body: dict[str, Any] = {
        "file_type": file_type,
        "srv_send_msg": srv_send_msg,
    }
    if url:
        body["url"] = url
    elif file_data:
        body["file_data"] = base64.b64encode(file_data).decode("ascii")

    return await _request("POST", path, json=body)


async def reply_media(
    chat_id: str,
    msg_id: str,
    *,
    is_group: bool = False,
    file_type: int = 1,
    url: str = "",
    file_data: bytes | None = None,
) -> dict:
    """上传富媒体 + 发送消息（两步合一）。

    先上传获取 file_info，再用 msg_type=7 发送。
    """
    # Step 1: 上传
    upload_res = await upload_media(
        chat_id, is_group=is_group, file_type=file_type,
        url=url, file_data=file_data, srv_send_msg=False,
    )
    file_info = upload_res.get("file_info")
    if not file_info:
        logger.warning("qq: upload_media failed: %s", upload_res)
        return upload_res

    # Step 2: 发送 msg_type=7 富媒体消息
    media = {"file_info": file_info}
    if is_group:
        return await send_group_message(
            chat_id, " ", msg_id=msg_id, msg_type=7, media=media,
        )
    return await send_c2c_message(
        chat_id, " ", msg_id=msg_id, msg_type=7, media=media,
    )


async def reply_markdown(
    chat_id: str,
    msg_id: str,
    content: str,
    *,
    is_group: bool = False,
) -> dict:
    """发送 Markdown 消息（msg_type=2，自定义 Markdown）。

    QQ 的 Markdown 消息需要在 QQ 开放平台申请 Markdown 模板或自定义 Markdown 权限。
    自定义 Markdown 仅限部分白名单 bot。
    """
    markdown = {"content": content}
    if is_group:
        return await send_group_message(
            chat_id, " ", msg_id=msg_id, markdown=markdown,
        )
    return await send_c2c_message(
        chat_id, " ", msg_id=msg_id, markdown=markdown,
    )


# ── 图片下载 ──

async def download_image_url(url: str) -> str:
    """下载图片 URL 并返回 base64 data URL。

    QQ 消息中的图片 URL 需要带 Authorization header 才能访问。
    """
    if not url:
        return ""

    token = await _get_access_token()
    headers = {}
    if token:
        headers["Authorization"] = f"QQBot {token}"

    try:
        async with httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(connect=5.0, read=30.0),
        ) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning("qq: download image failed: %d %s", resp.status_code, url[:100])
                return ""
            ct = resp.headers.get("content-type", "image/png")
            b64 = base64.b64encode(resp.content).decode("ascii")
            return f"data:{ct};base64,{b64}"
    except Exception as e:
        logger.warning("qq: download image error: %s", e)
        return ""
