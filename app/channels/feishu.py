"""飞书 Channel 实现

将现有 FeishuClient 封装为 Channel 接口。
webhook handler 通过此类与飞书交互。
"""

from __future__ import annotations

import json
import logging
import re

from app.channels.base import Channel, ChannelCapabilities, IncomingMessage, FEISHU_CAPABILITIES
from app.services.feishu import FeishuClient

logger = logging.getLogger(__name__)

# 飞书 @mention 正则：匹配 @_user_N 格式（群消息中的 at 标记）
_AT_PATTERN = re.compile(r"@_user_\d+\s*")


class FeishuChannel(Channel):
    """飞书平台 Channel 实现"""

    def __init__(self, client: FeishuClient | None = None) -> None:
        self._client = client or FeishuClient()

    @property
    def platform(self) -> str:
        return "feishu"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return FEISHU_CAPABILITIES

    @property
    def max_message_length(self) -> int:
        return 4000

    @property
    def prompt_hint(self) -> str:
        return (
            "用户在飞书上与你对话。你可以创建飞书文档、多维表格、日历事件、"
            "发送邮件、管理任务等。回复支持 Markdown 格式。"
        )

    # ── 消息发送 ──

    async def reply_text(self, message_id: str, text: str) -> dict:
        return await self._client.reply_text(message_id, text)

    async def send_to_chat(self, chat_id: str, text: str) -> dict:
        return await self._client.send_to_chat(chat_id, text)

    # ── 资源下载 ──

    async def download_image(self, message_id: str, image_key: str) -> str:
        return await self._client.download_image(message_id, image_key)

    async def download_file(self, message_id: str, file_key: str) -> bytes:
        return await self._client.download_file(message_id, file_key)

    # ── 用户信息 ──

    async def get_user_name(self, user_id: str) -> str:
        return await self._client.get_user_name(user_id)

    # ── 消息信息 ──

    async def get_message(self, message_id: str) -> dict:
        return await self._client.get_message(message_id)

    # ── Webhook 事件解析 ──

    def verify_event(self, headers: dict, body: bytes) -> bool:
        """飞书用 verification_token 验证。

        实际验证在 handler 层通过对比 header.token 完成。
        此方法预留给需要签名验证的场景（如加密模式）。
        """
        return True

    def parse_event(self, payload: dict) -> IncomingMessage | None:
        """解析飞书 webhook 事件为 IncomingMessage"""
        header = payload.get("header", {})
        event_type = header.get("event_type", "")

        # 只处理消息接收事件
        if event_type != "im.message.receive_v1":
            return None

        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})

        # 跳过 bot 自己的消息
        if sender.get("sender_type") == "app":
            return None

        sender_id = sender.get("sender_id", {}).get("open_id", "")
        if not sender_id:
            return None

        return IncomingMessage(
            event_id=header.get("event_id", ""),
            message_id=message.get("message_id", ""),
            chat_id=message.get("chat_id", ""),
            chat_type=message.get("chat_type", "p2p"),
            sender_id=sender_id,
            msg_type=message.get("message_type", "text"),
            content_raw=message.get("content", "{}"),
            extra={"mentions": message.get("mentions", [])},
        )

    def extract_text(self, msg: IncomingMessage) -> str:
        """从飞书消息中提取纯文本"""
        try:
            content = json.loads(msg.content_raw)
        except (json.JSONDecodeError, TypeError):
            return ""

        if msg.msg_type == "text":
            text = content.get("text", "")
            # 去除 @mention 标记
            text = _AT_PATTERN.sub("", text).strip()
            return text

        if msg.msg_type == "post":
            return self._extract_post_text(content)

        return ""

    @staticmethod
    def _extract_post_text(content: dict) -> str:
        """从 post（富文本）消息中提取纯文本"""
        texts: list[str] = []
        lang_blocks: list[dict] = []
        for val in content.values():
            if isinstance(val, dict) and "content" in val:
                lang_blocks.append(val)
        if not lang_blocks and "content" in content:
            lang_blocks.append(content)

        for lang_content in lang_blocks:
            if title := lang_content.get("title"):
                texts.append(title)
            for block in lang_content.get("content", []):
                if not isinstance(block, list):
                    continue
                for item in block:
                    tag = item.get("tag", "")
                    if tag == "text":
                        texts.append(item.get("text", ""))
                    elif tag == "at":
                        pass  # 跳过 at 标记
                    elif tag == "a":
                        texts.append(item.get("text", ""))
        return " ".join(t for t in texts if t)
