"""企微 API 异步客户端

负责:
- 获取 access_token（按 corpid 隔离）
- 发送应用消息（文本）
- 获取用户信息
"""

from __future__ import annotations

import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
_USER_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/get"


class WeComClient:
    """企微异步客户端，支持多租户。

    Token 按 corpid 缓存，不同租户各自独立。
    凭证从当前请求的 tenant 上下文获取。
    """

    def __init__(self) -> None:
        # 按 corpid 缓存 token：{corpid: (token_str, expire_time)}
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._user_name_cache: dict[str, str] = {}

    def _get_credentials(self) -> tuple[str, str, int]:
        """从当前 tenant 上下文获取企微凭证"""
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        return tenant.wecom_corpid, tenant.wecom_corpsecret, tenant.wecom_agent_id

    async def _get_token(self) -> str:
        """获取或刷新 access_token（按 corpid 隔离）"""
        corpid, corpsecret, _ = self._get_credentials()

        cached = self._token_cache.get(corpid)
        if cached and time.time() < cached[1]:
            return cached[0]

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.get(
                _TOKEN_URL,
                params={"corpid": corpid, "corpsecret": corpsecret},
            )
            data = resp.json()

        if data.get("errcode", -1) != 0:
            logger.error("failed to get wecom token for corpid=%s: %s", corpid[:10], data)
            raise RuntimeError(f"wecom token error: {data}")

        token = data["access_token"]
        expire = time.time() + data.get("expires_in", 7200) - 300
        self._token_cache[corpid] = (token, expire)
        return token

    async def send_text(self, userid: str, text: str) -> dict:
        """发送文本消息给指定用户"""
        _, _, agent_id = self._get_credentials()
        token = await self._get_token()

        body = {
            "touser": userid,
            "msgtype": "text",
            "agentid": agent_id,
            "text": {"content": text},
        }

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                _SEND_URL,
                params={"access_token": token},
                json=body,
            )
            result = resp.json()

        if result.get("errcode", -1) != 0:
            logger.error("wecom send_text failed: %s", result)
        return result

    async def send_text_to_chat(self, chatid: str, text: str) -> dict:
        """发送文本到群聊 (使用群聊会话推送)"""
        # 企微群聊用 appchat/send 接口
        token = await self._get_token()

        body = {
            "chatid": chatid,
            "msgtype": "text",
            "text": {"content": text},
        }

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/appchat/send",
                params={"access_token": token},
                json=body,
            )
            result = resp.json()

        if result.get("errcode", -1) != 0:
            logger.error("wecom send_text_to_chat failed: %s", result)
        return result

    async def get_user_name(self, userid: str) -> str:
        """通过 userid 获取用户姓名，带缓存"""
        if not userid:
            return ""
        if userid in self._user_name_cache:
            return self._user_name_cache[userid]

        token = await self._get_token()
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.get(
                    _USER_URL,
                    params={"access_token": token, "userid": userid},
                )
                data = resp.json()
            if data.get("errcode", -1) == 0:
                name = data.get("name", "")
                self._user_name_cache[userid] = name
                return name
            logger.warning("wecom get_user_name failed: %s", data)
        except Exception:
            logger.exception("wecom get_user_name error for %s", userid)
        return ""

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """下载临时素材（语音、图片等）

        Returns:
            (文件字节, content_type)，失败返回 (b"", "")
        """
        token = await self._get_token()
        url = "https://qyapi.weixin.qq.com/cgi-bin/media/get"
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.get(url, params={"access_token": token, "media_id": media_id})
                content_type = resp.headers.get("content-type", "")
                # 如果返回 JSON 说明出错了
                if "json" in content_type or resp.status_code != 200:
                    logger.error("wecom download_media failed: status=%d body=%s",
                                 resp.status_code, resp.text[:200])
                    return b"", ""
                logger.info("wecom download_media ok: %dKB, type=%s",
                            len(resp.content) // 1024, content_type)
                return resp.content, content_type
        except Exception:
            logger.exception("wecom download_media error for media_id=%s", media_id)
            return b"", ""

    async def upload_media(self, file_bytes: bytes, filename: str, media_type: str = "file") -> str:
        """上传临时素材，返回 media_id（有效期 3 天）

        Args:
            file_bytes: 文件字节
            filename: 文件名（含扩展名）
            media_type: "image" | "voice" | "video" | "file"

        Returns:
            media_id 字符串，失败返回空字符串
        """
        token = await self._get_token()
        url = "https://qyapi.weixin.qq.com/cgi-bin/media/upload"
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.post(
                    url,
                    params={"access_token": token, "type": media_type},
                    files={"media": (filename, file_bytes)},
                )
                data = resp.json()
            if data.get("errcode", 0) != 0:
                logger.error("wecom upload_media failed: %s", data)
                return ""
            mid = data.get("media_id", "")
            logger.info("wecom upload_media ok: filename=%s type=%s media_id=%s",
                        filename, media_type, mid[:20])
            return mid
        except Exception:
            logger.exception("wecom upload_media error for %s", filename)
            return ""

    async def send_file(self, userid: str, media_id: str) -> dict:
        """发送文件消息给指定用户"""
        _, _, agent_id = self._get_credentials()
        token = await self._get_token()

        body = {
            "touser": userid,
            "msgtype": "file",
            "agentid": agent_id,
            "file": {"media_id": media_id},
        }

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                _SEND_URL,
                params={"access_token": token},
                json=body,
            )
            result = resp.json()

        if result.get("errcode", -1) != 0:
            logger.error("wecom send_file failed: %s", result)
        return result

    async def send_image(self, userid: str, media_id: str) -> dict:
        """发送图片消息给指定用户"""
        _, _, agent_id = self._get_credentials()
        token = await self._get_token()

        body = {
            "touser": userid,
            "msgtype": "image",
            "agentid": agent_id,
            "image": {"media_id": media_id},
        }

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                _SEND_URL,
                params={"access_token": token},
                json=body,
            )
            result = resp.json()

        if result.get("errcode", -1) != 0:
            logger.error("wecom send_image failed: %s", result)
        return result

    async def reply_text(self, userid: str, text: str) -> dict:
        """回复用户消息（企微没有「回复」概念，直接发送应用消息）"""
        return await self.send_text(userid, text)


# 全局实例
wecom_client = WeComClient()
