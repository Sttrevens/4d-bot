"""租户配置模型

每个租户（bot）是一个独立的 AI 实体，拥有独立的:
- 人格、记忆、工具集、LLM 配置
- 一个或多个 Channel（平台接入点：飞书/企微/微信客服）
- GitHub 仓库（不同团队操作不同代码仓库）
- 管理员列表

架构:
  TenantConfig = Bot 身份层（人格 + 记忆 + 工具 + LLM）
  ChannelConfig = 平台接入层（凭证 + webhook）
  一个 Bot 可以有多个 Channel，共享记忆和人格。

向后兼容:
  - 旧 tenants.json 无 channels 字段 → 自动从 platform + 顶层凭证构造 primary channel
  - 所有读取 tenant.platform / tenant.app_id 等的旧代码继续正常工作
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Channel（平台接入点）──

@dataclass
class ChannelConfig:
    """单个平台接入 channel 的配置。

    一个 bot 可以有多个 channel，每个 channel 对应一个平台账号。
    channel_id 全局唯一，用于 webhook 路由。
    """
    channel_id: str = ""              # 唯一标识（如 "code-bot-feishu"）
    platform: str = "feishu"          # "feishu" | "wecom" | "wecom_kf" | "qq"
    enabled: bool = True              # 是否启用

    # ── 飞书凭证 ──
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    oauth_redirect_uri: str = ""
    bot_open_id: str = ""
    bot_aliases: list[str] = field(default_factory=list)

    # ── 企微凭证 ──
    wecom_corpid: str = ""
    wecom_corpsecret: str = ""
    wecom_agent_id: int = 0
    wecom_token: str = ""
    wecom_encoding_aes_key: str = ""

    # ── 微信客服凭证 ──
    wecom_kf_secret: str = ""
    wecom_kf_token: str = ""
    wecom_kf_encoding_aes_key: str = ""
    wecom_kf_open_kfid: str = ""

    # ── QQ 机器人凭证 ──
    qq_app_id: str = ""                   # QQ 开放平台 AppID
    qq_app_secret: str = ""               # QQ 开放平台 AppSecret
    qq_token: str = ""                    # Webhook 回调验证 token（Ed25519 seed）


# ── Tenant / Bot 身份层 ──

@dataclass
class TenantConfig:
    # ── 租户标识 ──
    tenant_id: str = ""
    name: str = ""

    # ── 平台类型（primary channel，向后兼容）──
    # 新架构下请用 channels 列表；此字段保留为 primary channel 的 platform
    platform: str = "feishu"  # "feishu" | "wecom" | "wecom_kf" | "qq"

    # ── 多 Channel 支持（新架构）──
    # 空列表 = 从 platform + 顶层凭证自动构造 primary channel（向后兼容）
    channels: list[ChannelConfig] = field(default_factory=list)

    # ── 飞书凭证 ──
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    oauth_redirect_uri: str = ""
    bot_open_id: str = ""  # 机器人 open_id，用于群聊精确 @mention 判断
    bot_aliases: list[str] = field(default_factory=list)  # bot 在飞书的显示名别名列表，用于 @mention 名字匹配

    # ── 企微凭证 ──
    wecom_corpid: str = ""
    wecom_corpsecret: str = ""
    wecom_agent_id: int = 0
    wecom_token: str = ""              # 回调验证 Token
    wecom_encoding_aes_key: str = ""   # 回调加密 EncodingAESKey

    # ── 微信客服凭证（面向外部微信用户）──
    # 使用企微 corpid，但用专门的客服 secret（在管理后台「微信客服」中获取）
    wecom_kf_secret: str = ""              # 微信客服专用 secret
    wecom_kf_token: str = ""               # 客服回调验证 Token
    wecom_kf_encoding_aes_key: str = ""    # 客服回调加密 EncodingAESKey
    wecom_kf_open_kfid: str = ""           # 客服账号 ID（wkXXXXXX）

    # ── QQ 机器人凭证 ──
    qq_app_id: str = ""                    # QQ 开放平台 AppID
    qq_app_secret: str = ""                # QQ 开放平台 AppSecret
    qq_token: str = ""                     # Webhook 回调验证 token（Ed25519 seed）

    # ── GitHub 配置 ──
    github_token: str = ""
    github_repo_owner: str = ""
    github_repo_name: str = ""

    # ── LLM 配置 ──
    llm_provider: str = "openai"  # "openai" | "gemini"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.moonshot.cn/v1"
    llm_model: str = "kimi-k2.5"
    llm_model_strong: str = ""  # 复杂任务自动升级使用的强模型（如 gemini-2.5-pro）
    llm_system_prompt: str = ""

    # ── 语音转写 (STT) 配置 ──
    # 空值 = 回退到全局 STT_API_KEY / STT_BASE_URL
    stt_api_key: str = ""
    stt_base_url: str = ""
    stt_model: str = ""

    # ── 管理员 ──
    admin_open_ids: list[str] = field(default_factory=list)
    admin_names: list[str] = field(default_factory=list)

    # ── 工具配置 ──
    # 空列表 = 全部启用；非空 = 仅启用列出的工具
    tools_enabled: list[str] = field(default_factory=list)

    # ── 调度器配置 ──
    scheduler_timezone: str = "Asia/Shanghai"  # 调度器工作时间使用的时区

    # ── 人设模式 ──
    # True = 租户有完整独立人设（如 AI 分身），跳过通用行为准则注入
    custom_persona: bool = False

    # ── 纯文本路由 ──
    # 非多模态消息走此模型，多模态（图片/视频）仍走 Gemini
    # 设为空字符串 "" 可禁用，全走主 provider
    coding_model: str = ""  # 默认全走主 provider（Gemini）；设为 "kimi-k2.5" 可启用纯文本路由
    coding_api_key: str = ""       # 为空 = fallback 到 settings.kimi.api_key
    coding_base_url: str = ""      # 为空 = fallback 到 settings.kimi.base_url

    # ── 自我迭代 ──
    # 仅平台管理员租户开启；客户租户禁用，防止修改共享代码
    self_iteration_enabled: bool = False

    # ── Auto-fix 策略（per-tenant 权限边界）──
    # 配置 auto-fix 可修改的文件路径前缀列表（GTC OpenShell 借鉴）
    # 空列表 = 使用全局默认（app/tools/, app/knowledge/）
    autofix_allowed_paths: list[str] = field(default_factory=list)

    # ── 实例管理 ──
    # 开启后可通过 bot tool 创建/管理其他租户实例（Phase 2 Control Plane）
    # 适用于销售/售后 bot，为客户自助开通独立 bot 实例
    instance_management_enabled: bool = False

    # ── 能力模块 ──
    # 预加载的能力模块名（对应 app/knowledge/modules/{name}.md）
    # 例：["social_media_research"] → 自动注入社媒调研工作流到 system prompt
    # 空列表 = 通用 bot，不预装任何领域知识（仍可通过工具动态加载）
    capability_modules: list[str] = field(default_factory=list)

    # ── 配额与限流 ──
    # 0 = 无限制（适用于自有/管理员租户）
    quota_monthly_api_calls: int = 0    # 每月最大 LLM API 调用次数
    quota_monthly_tokens: int = 0       # 每月最大 token 用量（input + output）
    rate_limit_rpm: int = 0             # 每分钟最大请求数（租户级，0=默认60）
    rate_limit_user_rpm: int = 0        # 每分钟最大请求数（用户级，0=默认10）

    # ── 社媒数据 API（可选）──
    # 配置后 search_social_media 工具优先使用第三方 API 获取精确数据
    # 空 = 回退到 web_search（DuckDuckGo）
    # "tikhub" — TikHub API ($0.001/次，中国直连 api.tikhub.dev)
    #            覆盖抖音+小红书+B站+快手+微博，700+ 端点
    # "newrank" — 新榜 API（需企业认证）
    social_media_api_provider: str = ""  # "tikhub" | "newrank" | ""
    social_media_api_key: str = ""       # TikHub: Bearer token; 新榜: API key
    social_media_api_secret: str = ""    # 新榜需要; TikHub 不需要

    # ── 记忆系统 ──
    # per-tenant 记忆行为配置，不同类型的 bot 需要不同深度的记忆
    memory_diary_enabled: bool = True   # 是否写日记（每次交互后 LLM 提炼摘要+标签）
                                        # False = 跳过 write_diary()，节省 LLM 调用
    memory_journal_max: int = 800       # 日志压缩阈值（0=不压缩，默认800条触发压缩）
    memory_chat_rounds: int = 5         # 对话历史保留轮数（1轮=用户+助手各1条）
    memory_chat_ttl: int = 3600         # 对话历史 Redis TTL（秒），0=不过期
    memory_context_enabled: bool = True # 是否在 system prompt 注入记忆上下文
                                        # False = 不调 build_memory_context()，纯无状态
    memory_org_recall_enabled: bool = False  # 是否启用组织级记忆共享
                                              # True = build_memory_context 时也搜索其他用户的解决方案
                                              # 同一 tenant 下的用户共享「解决方案」类记忆

    # ── 试用期 ──
    trial_enabled: bool = False         # 是否启用试用期（新用户首次对话开始计时）
    trial_duration_hours: int = 48      # 试用时长（小时），默认 2 天
    approval_duration_days: int = 30    # 审批后有效期（天），到期需重新审批（0=永久）
    quota_user_tokens_6h: int = 0       # 每用户每 6 小时最大 token 用量（0=不限）

    # ── 部署配额 ──
    # 每个用户可免费部署 bot 的次数，仅部署成功才消耗
    # 0 = 无限制（适用于管理员租户）
    deploy_free_quota: int = 1          # 每用户免费部署次数（默认 1 次）

    # ── Coworker 模式（主动巡群）──
    # 开启后 bot 定期扫描所在群聊的新消息，LLM 自主决定是否参与讨论
    coworker_mode_enabled: bool = False          # 是否启用 coworker 模式
    coworker_scan_interval_hours: int = 6        # 扫描间隔（小时），默认 6 小时
    coworker_scan_groups: list[str] = field(default_factory=list)  # 限定扫描的群 chat_id 列表，空=扫描所有群
    coworker_msg_count: int = 30                 # 每次扫描拉取的最近消息数
    coworker_quiet_hours_start: int = 22         # 安静时段开始（不主动发言）
    coworker_quiet_hours_end: int = 8            # 安静时段结束

    # ── 访问控制（白名单）──
    # 付费客户专属 bot：只有白名单中的用户才能使用
    # 空列表 = 不限制（试用 bot / 开放 bot）
    # 列表格式：[{"external_userid": "wm_xxx", "nickname": "张三"}, ...]
    allowed_users: list[dict] = field(default_factory=list)
    # bot 所有者（付费客户的 external_userid），用于后台管理/续费联系
    owner: str = ""
    # 白名单拦截时的提示语
    access_deny_msg: str = "抱歉，您没有权限使用此助手。如需开通，请联系管理员。"

    # ── Agent 路由绑定（借鉴 OpenClaw binding 系统）──
    # 同一个 bot 可以有多个人格，按 channel/chat/user 路由
    # 空列表 = 不启用（用 tenant 级别的统一配置）
    # 配置示例见 app/channels/routing.py 顶部注释
    agent_profiles: list[dict] = field(default_factory=list)   # list of AgentProfile dicts
    agent_bindings: list[dict] = field(default_factory=list)   # list of AgentBinding dicts

    # ── Channel 辅助方法 ──

    def get_channels(self) -> list[ChannelConfig]:
        """获取所有 channel。如果没有显式配置 channels，自动从顶层凭证构造 primary channel。"""
        if self.channels:
            return [ch for ch in self.channels if ch.enabled]
        # 向后兼容：从 platform + 顶层凭证构造 primary channel
        return [self._build_primary_channel()]

    def get_channel(self, platform: str) -> Optional[ChannelConfig]:
        """按平台类型获取第一个匹配的 channel。"""
        for ch in self.get_channels():
            if ch.platform == platform:
                return ch
        return None

    def find_channel_by_id(self, channel_id: str) -> Optional[ChannelConfig]:
        """按 channel_id 查找。"""
        for ch in self.get_channels():
            if ch.channel_id == channel_id:
                return ch
        return None

    def get_channel_platforms(self) -> list[str]:
        """获取所有已启用 channel 的平台列表（去重）。"""
        return list(dict.fromkeys(ch.platform for ch in self.get_channels()))

    def has_platform(self, platform: str) -> bool:
        """检查此 bot 是否有指定平台的 channel。"""
        return any(ch.platform == platform for ch in self.get_channels())

    def _build_primary_channel(self) -> ChannelConfig:
        """从顶层凭证构造 primary channel（向后兼容）。"""
        return ChannelConfig(
            channel_id=f"{self.tenant_id}-{self.platform}",
            platform=self.platform,
            enabled=True,
            # 飞书
            app_id=self.app_id,
            app_secret=self.app_secret,
            verification_token=self.verification_token,
            encrypt_key=self.encrypt_key,
            oauth_redirect_uri=self.oauth_redirect_uri,
            bot_open_id=self.bot_open_id,
            bot_aliases=self.bot_aliases,
            # 企微
            wecom_corpid=self.wecom_corpid,
            wecom_corpsecret=self.wecom_corpsecret,
            wecom_agent_id=self.wecom_agent_id,
            wecom_token=self.wecom_token,
            wecom_encoding_aes_key=self.wecom_encoding_aes_key,
            # 微信客服
            wecom_kf_secret=self.wecom_kf_secret,
            wecom_kf_token=self.wecom_kf_token,
            wecom_kf_encoding_aes_key=self.wecom_kf_encoding_aes_key,
            wecom_kf_open_kfid=self.wecom_kf_open_kfid,
            # QQ
            qq_app_id=self.qq_app_id,
            qq_app_secret=self.qq_app_secret,
            qq_token=self.qq_token,
        )
