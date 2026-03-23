"""微信客服 API 异步客户端

面向外部微信用户的客服场景，和内部自建应用 (wecom.py) 完全独立。

核心区别:
- 使用客服专用 secret 获取 access_token
- 消息通过 pull 模式获取 (sync_msg)，不是回调直推
- 发消息用 kf/send_msg，有 48 小时窗口限制
- 用户标识是 external_userid（外部微信用户）

API 文档:
- 读取消息: https://developer.work.weixin.qq.com/document/path/96426
- 发送消息: https://developer.work.weixin.qq.com/document/path/94677
"""

from __future__ import annotations

import base64
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_SYNC_MSG_URL = "https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg"
_SEND_MSG_URL = "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg"
_SEND_MSG_ON_EVENT_URL = "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg_on_event"
_ACCOUNT_LIST_URL = "https://qyapi.weixin.qq.com/cgi-bin/kf/account/list"
_MEDIA_GET_URL = "https://qyapi.weixin.qq.com/cgi-bin/media/get"
_CUSTOMER_BATCHGET_URL = "https://qyapi.weixin.qq.com/cgi-bin/kf/customer/batchget"
_SERVICE_STATE_GET_URL = "https://qyapi.weixin.qq.com/cgi-bin/kf/service_state/get"
_SERVICE_STATE_TRANS_URL = "https://qyapi.weixin.qq.com/cgi-bin/kf/service_state/trans"


class WeComKfClient:
    """微信客服异步客户端，支持多租户。

    Token 按 (corpid, kf_secret) 缓存。
    """

    def __init__(self) -> None:
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._customer_name_cache: dict[str, str] = {}  # external_userid → nickname

    def _get_credentials(self) -> tuple[str, str, str]:
        """从当前 tenant 上下文获取客服凭证

        Returns:
            (corpid, kf_secret, open_kfid)
        """
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        return tenant.wecom_corpid, tenant.wecom_kf_secret, tenant.wecom_kf_open_kfid

    async def _get_token(self) -> str:
        """获取客服专用 access_token（用 kf_secret 而非 corpsecret）"""
        corpid, kf_secret, _ = self._get_credentials()
        cache_key = f"{corpid}:kf"

        cached = self._token_cache.get(cache_key)
        if cached and time.time() < cached[1]:
            return cached[0]

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.get(
                _TOKEN_URL,
                params={"corpid": corpid, "corpsecret": kf_secret},
            )
            data = resp.json()

        if data.get("errcode", -1) != 0:
            logger.error("failed to get wecom kf token: %s", data)
            raise RuntimeError(f"wecom kf token error: {data}")

        token = data["access_token"]
        expire = time.time() + data.get("expires_in", 7200) - 300
        self._token_cache[cache_key] = (token, expire)
        return token

    def _clear_token_cache(self) -> None:
        """清除 token 缓存，强制下次请求重新获取。
        用于 95007/42001 等 token 失效场景。
        """
        self._token_cache.clear()
        logger.info("wecom kf token cache cleared")

    _TOKEN_INVALID_CODES = {95007, 42001, 40014}

    # ── 客服账号管理 API ──

    async def list_accounts(self) -> list[dict]:
        """列出当前 corp 下所有客服账号。

        Returns:
            [{"open_kfid": "wk...", "name": "客服名", "avatar": "url"}, ...]
        """
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.get(
                _ACCOUNT_LIST_URL,
                params={"access_token": token},
            )
            data = resp.json()

        if data.get("errcode", -1) != 0:
            logger.error("wecom kf list_accounts failed: %s", data)
            return []
        return data.get("account_list", [])

    async def add_account(self, name: str, media_id: str = "") -> dict:
        """创建新的客服账号。

        Args:
            name: 客服名称（不超过 16 字符）
            media_id: 头像 media_id（可选）

        Returns:
            {"open_kfid": "wk..."} 或 {"errcode": ..., "errmsg": ...}
        """
        token = await self._get_token()
        body: dict = {"name": name}
        if media_id:
            body["media_id"] = media_id
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/kf/account/add",
                params={"access_token": token},
                json=body,
            )
            data = resp.json()
        if data.get("errcode", -1) != 0:
            logger.error("wecom kf add_account failed: %s", data)
        return data

    async def delete_account(self, open_kfid: str) -> dict:
        """删除客服账号。"""
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/kf/account/del",
                params={"access_token": token},
                json={"open_kfid": open_kfid},
            )
            data = resp.json()
        if data.get("errcode", -1) != 0:
            logger.error("wecom kf delete_account failed: %s", data)
        return data

    async def update_account(
        self, open_kfid: str, name: str = "", media_id: str = ""
    ) -> dict:
        """修改客服账号名称或头像。"""
        token = await self._get_token()
        body: dict = {"open_kfid": open_kfid}
        if name:
            body["name"] = name
        if media_id:
            body["media_id"] = media_id
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/kf/account/update",
                params={"access_token": token},
                json=body,
            )
            data = resp.json()
        if data.get("errcode", -1) != 0:
            logger.error("wecom kf update_account failed: %s", data)
        return data

    async def get_account_link(
        self, open_kfid: str, scene: str = ""
    ) -> dict:
        """获取客服账号的接入链接（可嵌入网页或生成二维码）。

        Args:
            open_kfid: 客服账号 ID
            scene: 场景值（可选，不超过 32 字节）

        Returns:
            {"url": "https://work.weixin.qq.com/kf/..."} 或错误
        """
        token = await self._get_token()
        body: dict = {"open_kfid": open_kfid}
        if scene:
            body["scene"] = scene
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/kf/add_contact_way",
                params={"access_token": token},
                json=body,
            )
            data = resp.json()
        if data.get("errcode", -1) != 0:
            logger.error("wecom kf get_account_link failed: %s", data)
        return data

    async def sync_msg(
        self,
        callback_token: str,
        cursor: str = "",
        open_kfid: str = "",
        limit: int = 200,
    ) -> dict:
        """拉取客服消息（pull 模式）

        Args:
            callback_token: 回调事件中的 Token，用于校验合法性
            cursor: 上次返回的 next_cursor，首次不填
            open_kfid: 指定客服账号（不填则拉全部）
            limit: 拉取条数上限，最大 1000

        Returns:
            {errcode, errmsg, next_cursor, has_more, msg_list}
        """
        token = await self._get_token()
        body: dict = {
            "token": callback_token,
            "limit": limit,
        }
        if cursor:
            body["cursor"] = cursor
        if open_kfid:
            body["open_kfid"] = open_kfid

        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(
                _SYNC_MSG_URL,
                params={"access_token": token},
                json=body,
            )
            data = resp.json()

        errcode = data.get("errcode", 0)
        if errcode in self._TOKEN_INVALID_CODES:
            # token 失效，清除缓存并用新 token 重试一次
            logger.warning("wecom kf sync_msg token invalid (errcode=%d), refreshing token and retrying", errcode)
            self._clear_token_cache()
            token = await self._get_token()
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.post(
                    _SYNC_MSG_URL,
                    params={"access_token": token},
                    json=body,
                )
                data = resp.json()

        if data.get("errcode", -1) != 0:
            logger.error("wecom kf sync_msg failed: %s", data)
        return data

    async def get_service_state(
        self,
        external_userid: str,
        open_kfid: str = "",
    ) -> dict:
        """查询用户当前会话状态

        Returns:
            {errcode, errmsg, service_state, servicer_userid}
            service_state: 0=未处理, 1=智能助手, 2=待接入池, 3=人工接待, 4=已结束
        """
        _, _, default_kfid = self._get_credentials()
        kfid = open_kfid or default_kfid
        token = await self._get_token()

        body = {
            "open_kfid": kfid,
            "external_userid": external_userid,
        }

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                _SERVICE_STATE_GET_URL,
                params={"access_token": token},
                json=body,
            )
            data = resp.json()

        if data.get("errcode", -1) != 0:
            logger.warning("wecom kf get_service_state failed: %s", data)
        else:
            logger.info("wecom kf service_state for user=%s: state=%s servicer=%s",
                        external_userid[:12],
                        data.get("service_state"),
                        data.get("servicer_userid", ""))
        return data

    async def trans_service_state(
        self,
        external_userid: str,
        service_state: int = 1,
        open_kfid: str = "",
        servicer_userid: str = "",
    ) -> dict:
        """转接会话状态

        service_state:
            0 = 结束会话
            1 = 由智能助手接待（需后台开启智能助手）
            2 = 待接入池等待人工接待
            3 = 由人工接待（需传 servicer_userid）
        """
        _, _, default_kfid = self._get_credentials()
        kfid = open_kfid or default_kfid
        token = await self._get_token()

        body: dict = {
            "open_kfid": kfid,
            "external_userid": external_userid,
            "service_state": service_state,
        }
        if servicer_userid:
            body["servicer_userid"] = servicer_userid

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                _SERVICE_STATE_TRANS_URL,
                params={"access_token": token},
                json=body,
            )
            data = resp.json()

        if data.get("errcode", -1) != 0:
            logger.warning("wecom kf trans_service_state to %d failed: %s",
                           service_state, data)
        else:
            logger.info("wecom kf trans_service_state to %d OK for user=%s",
                        service_state, external_userid[:12])
        return data

    async def send_text(
        self,
        external_userid: str,
        text: str,
        open_kfid: str = "",
    ) -> dict:
        """发送文本消息给外部微信用户

        注意 48 小时窗口：用户主动发消息后 48 小时内可回复，最多 5 条。
        用户再次发消息则重新计算。
        """
        _, _, default_kfid = self._get_credentials()
        kfid = open_kfid or default_kfid
        token = await self._get_token()

        body = {
            "touser": external_userid,
            "open_kfid": kfid,
            "msgtype": "text",
            "text": {"content": text},
        }

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                _SEND_MSG_URL,
                params={"access_token": token},
                json=body,
            )
            data = resp.json()

        # 95018: 会话状态不对，查状态诊断，尝试修复
        if data.get("errcode") == 95018:
            state_info = await self.get_service_state(external_userid, open_kfid=kfid)
            cur_state = state_info.get("service_state", "?")
            cur_servicer = state_info.get("servicer_userid", "")
            logger.info("wecom_kf send_text: 95018 for user=%s, "
                        "current state=%s servicer=%s",
                        external_userid[:12], cur_state, cur_servicer)

            accepted = False

            if cur_state == 3:
                # state=3 由后台路由自动分配给人工客服，API 一般无法转出
                # 尝试 3→2→1，但如果后台配置为"人工接待"则会 95016
                to_pool = await self.trans_service_state(
                    external_userid, service_state=2, open_kfid=kfid,
                )
                if to_pool.get("errcode", -1) == 0:
                    accept = await self.trans_service_state(
                        external_userid, service_state=1, open_kfid=kfid,
                    )
                    accepted = accept.get("errcode", -1) == 0
                else:
                    logger.error(
                        "wecom_kf: state=3 无法转出 (servicer=%s, user=%s). "
                        "【管理员操作】请到企业微信后台→微信客服→接待方式，"
                        "改为「智能助手接待」，否则 bot 无法发送消息。",
                        cur_servicer, external_userid[:12])
            else:
                # 其他状态 (0/2/4) → 直接尝试接入 AI (→1)
                accept = await self.trans_service_state(
                    external_userid, service_state=1, open_kfid=kfid,
                )
                accepted = accept.get("errcode", -1) == 0

            if accepted:
                # 接入成功，重试发送
                async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                    resp = await client.post(
                        _SEND_MSG_URL,
                        params={"access_token": token},
                        json=body,
                    )
                    data = resp.json()
                if data.get("errcode", -1) != 0:
                    logger.error("wecom kf send_text retry after accept failed: %s", data)
                else:
                    logger.info("wecom kf send_text OK after session accept for user=%s",
                                external_userid[:12])
                return data

        if data.get("errcode", -1) != 0:
            logger.error("wecom kf send_text failed: %s", data)
        return data

    async def send_msg_on_event(self, code: str, text: str) -> dict:
        """用事件 code 发送响应消息（如 welcome_code 发欢迎语）。

        不受 service_state 限制，但 code 有效期仅 20 秒（enter_session）。
        """
        token = await self._get_token()
        body = {
            "code": code,
            "msgtype": "text",
            "text": {"content": text},
        }

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                _SEND_MSG_ON_EVENT_URL,
                params={"access_token": token},
                json=body,
            )
            data = resp.json()

        if data.get("errcode", -1) != 0:
            logger.warning("wecom_kf send_msg_on_event failed: %s", data)
        else:
            logger.info("wecom_kf send_msg_on_event OK (code=%s...)", code[:8] if code else "")
        return data

    async def reply_text(self, external_userid: str, text: str) -> dict:
        """回复外部微信用户（send_text 的别名，保持接口统一）"""
        return await self.send_text(external_userid, text)

    async def get_customer_name(self, external_userid: str) -> str:
        """获取外部微信用户的昵称（带缓存）。

        调用 kf/customer/batchget 接口，返回微信昵称。
        失败时返回空字符串。
        """
        if not external_userid:
            return ""
        if external_userid in self._customer_name_cache:
            return self._customer_name_cache[external_userid]

        try:
            token = await self._get_token()
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                resp = await client.post(
                    _CUSTOMER_BATCHGET_URL,
                    params={"access_token": token},
                    json={"external_userid_list": [external_userid]},
                )
                data = resp.json()

            if data.get("errcode", -1) != 0:
                logger.warning("get_customer_name failed: %s", data)
                return ""

            customers = data.get("customer_list", [])
            if customers:
                nickname = customers[0].get("nickname", "")
                if nickname:
                    self._customer_name_cache[external_userid] = nickname
                    return nickname
        except Exception:
            logger.warning("get_customer_name error for %s", external_userid[:15], exc_info=True)

        return ""

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
                logger.error("wecom_kf upload_media failed: %s", data)
                return ""
            mid = data.get("media_id", "")
            logger.info("wecom_kf upload_media ok: filename=%s type=%s media_id=%s",
                        filename, media_type, mid[:20])
            return mid
        except Exception:
            logger.exception("wecom_kf upload_media error for %s", filename)
            return ""

    async def send_file(
        self,
        external_userid: str,
        media_id: str,
        open_kfid: str = "",
    ) -> dict:
        """发送文件消息给外部微信用户"""
        _, _, default_kfid = self._get_credentials()
        kfid = open_kfid or default_kfid
        token = await self._get_token()

        body = {
            "touser": external_userid,
            "open_kfid": kfid,
            "msgtype": "file",
            "file": {"media_id": media_id},
        }

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                _SEND_MSG_URL,
                params={"access_token": token},
                json=body,
            )
            data = resp.json()

        if data.get("errcode", -1) != 0:
            logger.error("wecom_kf send_file failed: %s", data)
        return data

    async def send_image(
        self,
        external_userid: str,
        media_id: str,
        open_kfid: str = "",
    ) -> dict:
        """发送图片消息给外部微信用户"""
        _, _, default_kfid = self._get_credentials()
        kfid = open_kfid or default_kfid
        token = await self._get_token()

        body = {
            "touser": external_userid,
            "open_kfid": kfid,
            "msgtype": "image",
            "image": {"media_id": media_id},
        }

        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.post(
                _SEND_MSG_URL,
                params={"access_token": token},
                json=body,
            )
            data = resp.json()

        if data.get("errcode", -1) != 0:
            logger.error("wecom_kf send_image failed: %s", data)
        return data

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """通过 media_id 下载临时素材（图片/语音/视频/文件）

        media/get 接口权限完全公开，kf_secret 的 token 可以调用。
        media_id 有效期 3 天。

        Returns:
            (content_bytes, content_type) 或失败时 (b"", "")
        """
        token = await self._get_token()
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.get(
                    _MEDIA_GET_URL,
                    params={"access_token": token, "media_id": media_id},
                )
            ct = resp.headers.get("content-type", "")
            if "json" in ct or "text/plain" in ct:
                logger.error("download_media failed: %s", resp.text[:300])
                return b"", ""
            logger.info("download_media OK: media_id=%s type=%s size=%dKB",
                        media_id[:20], ct, len(resp.content) // 1024)
            return resp.content, ct.split(";")[0].strip()
        except Exception:
            logger.exception("download_media error for %s", media_id)
            return b"", ""

    async def download_media_as_data_url(self, media_id: str, fallback_mime: str = "image/png") -> str:
        """下载素材并返回 base64 data URL（适合送给视觉模型）

        Returns:
            data URL 字符串，失败返回空字符串
        """
        content, ct = await self.download_media(media_id)
        if not content:
            return ""
        mime = ct if ct else fallback_mime
        b64 = base64.b64encode(content).decode("ascii")
        return f"data:{mime};base64,{b64}"


# 全局实例
wecom_kf_client = WeComKfClient()
