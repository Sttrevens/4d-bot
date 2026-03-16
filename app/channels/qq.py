"""QQ 机器人 Channel 实现

将 QQ Bot API 封装为 Channel 接口。
QQ 官方 API v2 使用 webhook + Ed25519 签名验证。

多媒体支持:
- 接收: attachments 字段包含图片/视频/语音/文件的 URL + content_type
- 发送: msg_type=7 富媒体（先上传获取 file_info 再发送）
- Markdown: msg_type=2（需要 QQ 开放平台白名单权限）
"""

from __future__ import annotations

import logging

from app.channels.base import Channel, ChannelCapabilities, IncomingMessage, QQ_CAPABILITIES
from app.services import qq as qq_api

logger = logging.getLogger(__name__)


class QQChannel(Channel):
    """QQ 官方机器人平台 Channel 实现"""

    @property
    def platform(self) -> str:
        return "qq"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return QQ_CAPABILITIES

    @property
    def max_message_length(self) -> int:
        # QQ 群消息长度限制较短
        return 2000

    @property
    def prompt_hint(self) -> str:
        return (
            "用户在 QQ 上与你对话。QQ 消息长度限制较短（2000字符），"
            "请尽量简洁回复。不支持 Markdown 渲染。"
        )

    # ── 消息发送 ──

    async def reply_text(self, message_id: str, text: str) -> dict:
        """回复消息。message_id 格式: "{chat_type}:{chat_id}:{msg_id}"

        webhook handler 在 parse_event 时将三者编码到 message_id 中。
        """
        chat_type, chat_id, msg_id = _unpack_message_id(message_id)
        return await qq_api.reply_text(
            chat_id, msg_id, text, is_group=(chat_type == "group"),
        )

    async def send_to_chat(self, chat_id: str, text: str) -> dict:
        """主动发送消息到聊天。chat_id 格式: "{chat_type}:{id}"。"""
        if ":" in chat_id:
            chat_type, real_id = chat_id.split(":", 1)
        else:
            chat_type, real_id = "p2p", chat_id
        return await qq_api.send_to_chat(
            real_id, text, is_group=(chat_type == "group"),
        )

    async def upload_and_send_file(
        self, user_id: str, file_bytes: bytes, filename: str,
    ) -> dict:
        """上传文件并发送给用户。"""
        # 推断 file_type: 1=图片, 2=视频, 3=语音, 4=文件
        lower = filename.lower()
        if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
            file_type = 1
        elif lower.endswith((".mp4", ".avi", ".mov", ".mkv", ".webm")):
            file_type = 2
        elif lower.endswith((".mp3", ".wav", ".ogg", ".amr", ".silk")):
            file_type = 3
        else:
            file_type = 4

        return await qq_api.upload_media(
            user_id, is_group=False, file_type=file_type,
            file_data=file_bytes, srv_send_msg=True,
        )

    # ── 资源下载 ──

    async def download_image(self, message_id: str, image_key: str) -> str:
        """下载图片，返回 base64 data URL。

        QQ 消息中 attachments 的 url 需要带 Authorization 才能访问。
        """
        if not image_key:
            return ""
        # 如果已经是 data URL，直接返回
        if image_key.startswith("data:"):
            return image_key
        # 通过 API 下载
        return await qq_api.download_image_url(image_key)

    async def download_file(self, message_id: str, file_key: str) -> bytes:
        """下载文件附件。QQ 文件 URL 需要认证访问。"""
        if not file_key or not file_key.startswith("http"):
            return b""
        import httpx
        token = await qq_api._get_access_token()
        headers = {"Authorization": f"QQBot {token}"} if token else {}
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(connect=5.0, read=60.0),
            ) as client:
                resp = await client.get(file_key, headers=headers)
                if resp.status_code == 200:
                    return resp.content
                logger.warning("qq: download file failed: %d", resp.status_code)
        except Exception as e:
            logger.warning("qq: download file error: %s", e)
        return b""

    # ── 用户信息 ──

    async def get_user_name(self, user_id: str) -> str:
        # QQ API 对用户信息获取有限制，返回 openid 前 8 位作为标识
        return f"QQ用户({user_id[:8]})" if user_id else ""

    # ── 消息信息 ──

    async def get_message(self, message_id: str) -> dict:
        return {}

    # ── Webhook 事件解析 ──

    def verify_event(self, headers: dict, body: bytes) -> bool:
        """QQ webhook 验证在 handler 层通过 Ed25519 完成，此处预留。"""
        return True

    def parse_event(self, payload: dict) -> IncomingMessage | None:
        """解析 QQ webhook 事件为 IncomingMessage。"""
        op = payload.get("op")
        event_type = payload.get("t", "")
        d = payload.get("d", {})

        # 只处理消息事件
        if op != 0:
            return None

        if event_type == "C2C_MESSAGE_CREATE":
            return self._parse_c2c(d, payload)
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            return self._parse_group(d, payload)

        return None

    def extract_text(self, msg: IncomingMessage) -> str:
        """提取纯文本内容。"""
        text = msg.content_raw
        # 群 @消息 content 前面可能有空格
        return text.strip()

    def extract_attachments(self, msg: IncomingMessage) -> list[dict]:
        """从 IncomingMessage 中提取附件列表。

        QQ 消息 attachments 结构:
        [{"content_type": "image/png", "filename": "xxx.png",
          "height": 720, "width": 1280, "size": "12345", "url": "https://..."}]

        返回标准化的附件列表:
        [{"type": "image", "url": "https://...", "content_type": "image/png",
          "filename": "xxx.png", "size": 12345}]
        """
        if not msg.extra:
            return []
        raw_attachments = msg.extra.get("attachments", [])
        if not raw_attachments:
            return []

        result = []
        for att in raw_attachments:
            ct = att.get("content_type", "")
            url = att.get("url", "")
            if not url:
                continue

            # 推断类型
            if ct.startswith("image/"):
                att_type = "image"
            elif ct.startswith("video/"):
                att_type = "video"
            elif ct.startswith("audio/"):
                att_type = "audio"
            else:
                att_type = "file"

            result.append({
                "type": att_type,
                "url": url,
                "content_type": ct,
                "filename": att.get("filename", ""),
                "size": int(att.get("size", 0) or 0),
                "width": att.get("width"),
                "height": att.get("height"),
            })

        return result

    # ── 内部解析 ──

    @staticmethod
    def _parse_c2c(d: dict, payload: dict) -> IncomingMessage:
        """解析 C2C（单聊）消息。"""
        author = d.get("author", {})
        sender_id = author.get("user_openid", "")
        msg_id = d.get("id", "")
        content = d.get("content", "")
        attachments = d.get("attachments", []) or []

        # 根据附件推断 msg_type
        msg_type = "text"
        if attachments:
            ct = attachments[0].get("content_type", "")
            if ct.startswith("image/"):
                msg_type = "image"
            elif ct.startswith("video/"):
                msg_type = "video"
            elif ct.startswith("audio/"):
                msg_type = "audio"
            else:
                msg_type = "file"

        return IncomingMessage(
            event_id=payload.get("id", msg_id),
            message_id=f"p2p:{sender_id}:{msg_id}",
            chat_id=f"p2p:{sender_id}",
            chat_type="p2p",
            sender_id=sender_id,
            msg_type=msg_type,
            content_raw=content,
            extra={"qq_msg_id": msg_id, "attachments": attachments},
        )

    @staticmethod
    def _parse_group(d: dict, payload: dict) -> IncomingMessage:
        """解析群 @消息。"""
        author = d.get("author", {})
        sender_id = author.get("member_openid", "")
        group_openid = d.get("group_openid", "")
        msg_id = d.get("id", "")
        content = d.get("content", "")
        attachments = d.get("attachments", []) or []

        msg_type = "text"
        if attachments:
            ct = attachments[0].get("content_type", "")
            if ct.startswith("image/"):
                msg_type = "image"
            elif ct.startswith("video/"):
                msg_type = "video"
            elif ct.startswith("audio/"):
                msg_type = "audio"
            else:
                msg_type = "file"

        return IncomingMessage(
            event_id=payload.get("id", msg_id),
            message_id=f"group:{group_openid}:{msg_id}",
            chat_id=f"group:{group_openid}",
            chat_type="group",
            sender_id=sender_id,
            msg_type=msg_type,
            content_raw=content,
            extra={
                "qq_msg_id": msg_id,
                "group_openid": group_openid,
                "attachments": attachments,
            },
        )


def _unpack_message_id(packed: str) -> tuple[str, str, str]:
    """解包 message_id: "chat_type:chat_id:msg_id" → (chat_type, chat_id, msg_id)"""
    parts = packed.split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    # fallback: 当作 C2C
    return "p2p", "", packed
