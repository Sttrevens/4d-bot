"""Channel 抽象基类

定义所有消息平台必须实现的统一接口。
FeishuChannel / WeComChannel / SlackChannel 等都继承自此类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class IncomingMessage:
    """平台无关的收到消息结构"""

    event_id: str  # 事件去重 ID
    message_id: str  # 消息 ID（用于回复）
    chat_id: str  # 会话 ID
    chat_type: str  # "p2p" | "group"
    sender_id: str  # 发送者 ID（open_id / userid）
    msg_type: str  # "text" | "image" | "file" | "post" 等
    content_raw: str  # 原始 content JSON 字符串
    # 扩展字段（平台特有数据放这里，避免改基类）
    extra: dict | None = None


class Channel(ABC):
    """消息平台统一接口

    每个平台实现此基类。webhook handler 和 agent loop
    只通过 Channel 交互，不直接调用平台 SDK。
    """

    # ── 平台元数据 ──

    @property
    @abstractmethod
    def platform(self) -> str:
        """平台标识: "feishu" / "wecom" / "slack" 等"""

    @property
    def max_message_length(self) -> int:
        """单条消息最大文本长度（字符数）。子类可覆盖。"""
        return 4000

    # ── 消息发送 ──

    @abstractmethod
    async def reply_text(self, message_id: str, text: str) -> dict:
        """回复一条文本消息（以回复形式）"""

    @abstractmethod
    async def send_to_chat(self, chat_id: str, text: str) -> dict:
        """往聊天窗口发一条独立消息（非回复）"""

    # ── 文件发送（可选能力，子类按需实现）──

    async def upload_and_send_file(
        self, user_id: str, file_bytes: bytes, filename: str,
    ) -> dict:
        """上传文件并发送给用户。

        默认不支持，子类（如 WeComChannel）覆盖此方法。
        Returns:
            API 返回或 {"errcode": -1, "errmsg": "not supported"} 表示不支持
        """
        return {"errcode": -1, "errmsg": "file sending not supported on this platform"}

    # ── 资源下载 ──

    @abstractmethod
    async def download_image(self, message_id: str, image_key: str) -> str:
        """下载图片，返回 base64 data URL 或空字符串"""

    @abstractmethod
    async def download_file(self, message_id: str, file_key: str) -> bytes:
        """下载文件附件，返回原始字节"""

    # ── 用户信息 ──

    @abstractmethod
    async def get_user_name(self, user_id: str) -> str:
        """根据用户 ID 获取显示名"""

    # ── 消息信息 ──

    @abstractmethod
    async def get_message(self, message_id: str) -> dict:
        """获取单条消息详情"""

    # ── Webhook 事件解析 ──

    @abstractmethod
    def verify_event(self, headers: dict, body: bytes) -> bool:
        """验证 webhook 事件签名/token。返回 True 表示合法。"""

    @abstractmethod
    def parse_event(self, payload: dict) -> IncomingMessage | None:
        """从 webhook payload 解析出 IncomingMessage。

        返回 None 表示该事件不需要处理（如非消息事件）。
        """

    @abstractmethod
    def extract_text(self, msg: IncomingMessage) -> str:
        """从 IncomingMessage 提取纯文本内容"""
