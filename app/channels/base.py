"""Channel 抽象基类

定义所有消息平台必须实现的统一接口。
FeishuChannel / WeComChannel / SlackChannel 等都继承自此类。

借鉴 OpenClaw 的 ChannelCapabilities 和 prompt hint 设计：
- 每个 channel 声明自己支持的能力（文档、日历、reactions 等）
- 每个 channel 可注入平台相关的 system prompt 上下文
- base_agent.py 根据 capabilities 过滤工具，替代硬编码的 _FEISHU_ONLY_TOOLS
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


# ── Channel 能力声明（借鉴 OpenClaw ChannelCapabilities）──

@dataclass
class ChannelCapabilities:
    """声明 channel 支持的能力。

    base_agent.py 根据此声明决定启用哪些工具，
    替代硬编码的 _FEISHU_ONLY_TOOLS / _WECOM_ONLY_TOOLS。

    设计原则：声明 channel 能做什么，而不是不能做什么（opt-in）。
    """

    # 文档/知识库操作（飞书有，企微无）
    documents: bool = False
    # 多维表格 / 数据库操作
    bitable: bool = False
    # 日历事件
    calendar: bool = False
    # 任务管理
    tasks: bool = False
    # 邮件
    mail: bool = False
    # 会议纪要
    minutes: bool = False
    # 用户目录查询
    user_directory: bool = False
    # 消息操作（转发、pin、置顶等）
    message_actions: bool = False
    # Reactions / 表情回复
    reactions: bool = False
    # 线程 / 话题回复
    threads: bool = False
    # 文件上传发送
    file_upload: bool = False
    # 富文本 / Markdown 发送（消息卡片等富文本能力）
    rich_text: bool = False
    # 平台是否在纯文本消息中渲染 Markdown（飞书消息卡片=True，微信=False）
    supports_markdown: bool = False
    # 图片发送
    image_send: bool = False
    # OAuth 用户授权（user_access_token）
    oauth: bool = False


# 预定义的平台能力配置，避免每个 Channel 实例重复声明
FEISHU_CAPABILITIES = ChannelCapabilities(
    documents=True,
    bitable=True,
    calendar=True,
    tasks=True,
    mail=True,
    minutes=True,
    user_directory=True,
    message_actions=True,
    reactions=True,
    threads=True,
    file_upload=True,
    rich_text=True,
    supports_markdown=True,
    image_send=True,
    oauth=True,
)

WECOM_CAPABILITIES = ChannelCapabilities(
    file_upload=True,
    rich_text=True,
    image_send=True,
)

WECOM_KF_CAPABILITIES = ChannelCapabilities(
    rich_text=True,
    image_send=True,
)

QQ_CAPABILITIES = ChannelCapabilities(
    reactions=True,
    rich_text=True,
    image_send=True,
)

# Discord 预留（未来接入）
DISCORD_CAPABILITIES = ChannelCapabilities(
    reactions=True,
    threads=True,
    rich_text=True,
    supports_markdown=True,
    image_send=True,
    file_upload=True,
)


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
    def capabilities(self) -> ChannelCapabilities:
        """平台能力声明。子类应覆盖此属性返回对应的 capabilities。

        base_agent.py 用此声明决定启用/禁用哪些工具。
        """
        return ChannelCapabilities()  # 默认无特殊能力

    @property
    def max_message_length(self) -> int:
        """单条消息最大文本长度（字符数）。子类可覆盖。"""
        return 4000

    @property
    def prompt_hint(self) -> str:
        """平台相关的 system prompt 上下文。

        注入到 LLM system prompt 中，让模型了解当前平台的特性和限制。
        子类可覆盖提供平台特定的提示。空字符串 = 不注入。

        例：飞书 channel 可以提示 "用户在飞书上与你对话，你可以创建飞书文档、日历事件等"
        """
        return ""

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
