"""飞书 API 异步客户端

负责:
- 获取 tenant_access_token（按租户隔离）
- 回复消息 (reply)
- 获取用户信息
- 下载图片/文件资源
- 获取合并转发子消息
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)


class FileTooLargeError(Exception):
    """飞书消息资源下载 API 的文件大小超限（error code 234037）"""
    pass

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_REPLY_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
_SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_USER_URL = "https://open.feishu.cn/open-apis/contact/v3/users/{user_id}"
_IMAGE_URL = "https://open.feishu.cn/open-apis/im/v1/images/{image_key}"
_MSG_RESOURCE_URL = (
    "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
)
_MSG_READ_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
_MINUTES_META_URL = "https://open.feishu.cn/open-apis/minutes/v1/minutes/{minute_token}"
_MINUTES_MEDIA_URL = (
    "https://open.feishu.cn/open-apis/minutes/v1/minutes/{minute_token}/media"
)


def _learn_bot_open_id(reply_result: dict) -> None:
    """从 reply/send 的 API 响应中提取 bot 的 open_id 并缓存到 handler。

    飞书发送/回复消息的响应里包含 data.sender.id（bot 自身的 open_id），
    利用这个信息自动学习，不需要 bot:info scope。
    """
    try:
        sender_id = reply_result.get("data", {}).get("sender", {}).get("id", "")
        if not sender_id:
            return
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        tid = tenant.tenant_id
        from app.webhook.handler import _bot_open_ids
        if tid not in _bot_open_ids:
            _bot_open_ids[tid] = sender_id
            logger.info("learned bot open_id from reply for tenant=%s: %s", tid, sender_id[:15])
    except Exception:
        pass  # 学习失败不影响正常回复


class FeishuClient:
    """飞书异步客户端，支持多租户。

    Token 按 app_id 缓存，不同租户各自独立。
    凭证从当前请求的 tenant 上下文获取。
    """

    def __init__(self) -> None:
        # 按 app_id 缓存 token：{app_id: (token_str, expire_time)}
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._user_name_cache: dict[str, str] = {}

    def _get_credentials(self) -> tuple[str, str]:
        """从当前 tenant 上下文获取凭证"""
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        return tenant.app_id, tenant.app_secret

    async def _get_token(self) -> str:
        """获取或刷新 tenant_access_token（按 app_id 隔离），网络错误自动重试。"""
        app_id, app_secret = self._get_credentials()

        if not app_id or not app_secret:
            raise RuntimeError("feishu token unavailable: tenant has no feishu app_id/app_secret")

        cached = self._token_cache.get(app_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                    resp = await client.post(
                        _TOKEN_URL,
                        json={"app_id": app_id, "app_secret": app_secret},
                    )
                    data = resp.json()

                if data.get("code") != 0:
                    logger.error("failed to get feishu token for app=%s (attempt %d): %s",
                                 app_id, attempt + 1, data)
                    raise RuntimeError(f"feishu token error: {data}")

                token = data["tenant_access_token"]
                expire = time.time() + data.get("expire", 7200) - 300
                self._token_cache[app_id] = (token, expire)
                return token
            except RuntimeError:
                raise  # API 业务错误不重试
            except Exception as exc:
                last_exc = exc
                logger.warning("feishu token request failed (attempt %d): %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(1 * (attempt + 1))

        raise RuntimeError(f"feishu token request failed after 3 retries: {last_exc}")

    async def reply_text(self, message_id: str, text: str) -> dict:
        """回复一条文本消息"""
        token = await self._get_token()
        url = _REPLY_URL.format(message_id=message_id)
        body = {
            "content": json.dumps({"text": text}),
            "msg_type": "text",
        }
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            result = resp.json()

        if result.get("code") != 0:
            logger.error("reply failed: %s", result)
        else:
            # 从回复响应中学习 bot 的 open_id（无需额外 API scope）
            _learn_bot_open_id(result)
        return result

    async def send_to_chat(self, chat_id: str, text: str) -> dict:
        """往聊天窗口发一条新消息（非回复，独立 bubble）"""
        token = await self._get_token()
        body = {
            "receive_id": chat_id,
            "content": json.dumps({"text": text}),
            "msg_type": "text",
        }
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.post(
                _SEND_URL,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                params={"receive_id_type": "chat_id"},
            )
            result = resp.json()

        if result.get("code") != 0:
            logger.error("send_to_chat failed: %s", result)
        else:
            _learn_bot_open_id(result)
        return result

    async def get_user_name(self, open_id: str) -> str:
        """通过 open_id 获取用户姓名，带缓存"""
        if not open_id:
            return ""
        if open_id in self._user_name_cache:
            return self._user_name_cache[open_id]

        token = await self._get_token()
        url = _USER_URL.format(user_id=open_id)
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.get(
                    url,
                    params={"user_id_type": "open_id"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = resp.json()
            if data.get("code") == 0:
                name = data.get("data", {}).get("user", {}).get("name", "")
                self._user_name_cache[open_id] = name
                return name
            logger.warning("get_user_name failed: %s", data)
        except Exception:
            logger.exception("get_user_name error for %s", open_id)
        return ""

    async def download_image(self, message_id: str, image_key: str) -> str:
        """下载消息中的图片并返回 base64 编码的 data URL

        使用 message resource API（/im/v1/messages/{mid}/resources/{key}?type=image）
        而非 image API（/im/v1/images/{key}），后者仅适用于 bot 自己上传的图片。
        """
        token = await self._get_token()
        url = _MSG_RESOURCE_URL.format(message_id=message_id, file_key=image_key)
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params={"type": "image"},
                )
            if resp.status_code != 200:
                logger.error("download_image failed: status=%d body=%s",
                             resp.status_code, resp.text[:300])
                return ""
            raw_ct = resp.headers.get("content-type", "image/png")
            # 只保留主类型，去掉 charset 等参数
            content_type = raw_ct.split(";")[0].strip()
            # 飞书有时返回 application/octet-stream，强制修正为 image/png
            if not content_type.startswith("image/"):
                logger.warning("download_image: unexpected content-type '%s', using image/png", raw_ct)
                content_type = "image/png"
            b64 = base64.b64encode(resp.content).decode("ascii")
            logger.info("download_image OK: key=%s type=%s size=%dKB",
                        image_key[:20], content_type, len(resp.content) // 1024)
            return f"data:{content_type};base64,{b64}"
        except Exception:
            logger.exception("download_image error for %s", image_key)
            return ""

    async def download_file(self, message_id: str, file_key: str) -> bytes:
        """下载文件附件，返回原始字节。

        Raises:
            FileTooLargeError: 文件超过飞书 API 下载大小限制（~20MB）
        """
        token = await self._get_token()
        url = _MSG_RESOURCE_URL.format(message_id=message_id, file_key=file_key)
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params={"type": "file"},
                )
            if resp.status_code != 200:
                logger.error("download_file failed: status=%d body=%s",
                             resp.status_code, resp.text[:500])
                # 234037 = 文件大小超限，抛专用异常让调用方给出明确提示
                try:
                    if resp.json().get("code") == 234037:
                        raise FileTooLargeError("file exceeds Feishu API download limit")
                except (json.JSONDecodeError, ValueError):
                    pass
                return b""
            return resp.content
        except FileTooLargeError:
            raise
        except Exception:
            logger.exception("download_file error for %s", file_key)
            return b""

    async def get_minutes_meta(self, minute_token: str) -> dict:
        """获取妙记元信息（标题、时长、封面等）"""
        token = await self._get_token()
        url = _MINUTES_META_URL.format(minute_token=minute_token)
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {}).get("minute", {})
            logger.warning("get_minutes_meta failed: %s", data)
        except Exception:
            logger.exception("get_minutes_meta error for %s", minute_token)
        return {}

    async def download_minutes_media(self, minute_token: str) -> bytes:
        """通过妙记 API 下载音视频文件（绕过消息资源 20MB 限制）"""
        token = await self._get_token()
        url = _MINUTES_MEDIA_URL.format(minute_token=minute_token)
        try:
            async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code != 200:
                logger.error("download_minutes_media failed: status=%d body=%s",
                             resp.status_code, resp.text[:500])
                return b""
            logger.info("download_minutes_media OK: token=%s size=%dKB",
                        minute_token[:16], len(resp.content) // 1024)
            return resp.content
        except Exception:
            logger.exception("download_minutes_media error for %s", minute_token)
            return b""

    async def get_message(self, message_id: str) -> dict:
        """获取单条消息详情"""
        token = await self._get_token()
        url = _MSG_READ_URL.format(message_id=message_id)
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {})
            logger.warning("get_message failed: %s", data)
        except Exception:
            logger.exception("get_message error for %s", message_id)
        return {}


# 全局实例，供其他模块导入使用
feishu_client = FeishuClient()
