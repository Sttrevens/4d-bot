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
    platform: str = "feishu"  # "feishu" | "wecom" | "wecom_kf" | "qq"

    # ── 多 Channel 支持（新架构）──
    channels: list[ChannelConfig] = field(default_factory=list)

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
    qq_app_id: str = ""
    qq_app_secret: str = ""
    qq_token: str = ""

    # ── GitHub 配置 ──
    github_token: str = ""
    github_repo_owner: str = ""
    github_repo_name: str = ""

    # ── LLM 配置 ──
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.moonshot.cn/v1"
    llm_model: str = "kimi-k2.5"
    llm_model_strong: str = ""
    llm_system_prompt: str = ""

    # ── 语音转写 (STT) 配置 ──
    stt_api_key: str = ""
    stt_base_url: str = ""
    stt_model: str = ""

    # ── 管理员 ──
    admin_open_ids: list[str] = field(default_factory=list)
    admin_names: list[str] = field(default_factory=list)

    # ── 工具配置 ──
    tools_enabled: list[str] = field(default_factory=list)

    # ── 调度器配置 ──
    scheduler_timezone: str = "Asia/Shanghai"

    # ── 人设模式 ──
    custom_persona: bool = False

    # ── 纯文本路由 ──
    coding_model: str = ""
    coding_api_key: str = ""
    coding_base_url: str = ""

    # ── 自我迭代 ──
    self_iteration_enabled: bool = False

    # ── Auto-fix 策略 ──
    autofix_allowed_paths: list[str] = field(default_factory=list)

    # ── 实例管理 ──
    instance_management_enabled: bool = False

    # ── 能力模块 ──
    capability_modules: list[str] = field(default_factory=list)

    # ── 配额与限流 ──
    quota_monthly_api_calls: int = 0
    quota_monthly_tokens: int = 0
    rate_limit_rpm: int = 0
    rate_limit_user_rpm: int = 0

    # ── 社媒数据 API ──
    social_media_api_provider: str = ""
    social_media_api_key: str = ""
    social_media_api_secret: str = ""

    # ── 记忆系统 ──
    memory_diary_enabled: bool = True
    memory_journal_max: int = 800
    memory_chat_rounds: int = 5
    memory_chat_ttl: int = 3600
    memory_context_enabled: bool = True
    memory_org_recall_enabled: bool = False

    # ── AGENT.md 项目上下文 ──
    # 类似 Claude Code 的 CLAUDE.md：从 GitHub 仓库加载项目级上下文，
    # 让 bot 理解项目结构、编码规范和架构约定。
    agentmd_enabled: bool = True        # 是否从 GitHub 仓库加载 AGENT.md
    agentmd_path: str = "AGENT.md"      # AGENT.md 在仓库中的路径

    # ── 试用期 ──
    trial_enabled: bool = False
    trial_duration_hours: int = 48
    approval_duration_days: int = 30
    quota_user_tokens_6h: int = 0

    # ── 部署配额 ──
    deploy_free_quota: int = 1

    # ── Coworker 模式 ──
    coworker_mode_enabled: bool = False
    coworker_scan_interval_hours: int = 6
    coworker_scan_groups: list[str] = field(default_factory=list)
    coworker_msg_count: int = 30
    coworker_quiet_hours_start: int = 22
    coworker_quiet_hours_end: int = 8

    # ── 访问控制 ──
    allowed_users: list[dict] = field(default_factory=list)
    owner: str = ""
    access_deny_msg: str = "抱歉，您没有权限使用此助手。如需开通，请联系管理员。"

    # ── Agent 路由绑定 ──
    agent_profiles: list[dict] = field(default_factory=list)
    agent_bindings: list[dict] = field(default_factory=list)

    # ── 插件系统（NanoClaw 启发）──
    plugin_groups_enabled: list[str] = field(default_factory=list)  # 启用的工具组 ["core","feishu_collab","code_dev"]，空=全部
    plugin_lazy_loading: bool = True   # 是否按需加载工具（减少 context）

    # ── 容器沙箱（NanoClaw 启发）──
    container_sandbox_enabled: bool = False  # 是否使用 Docker 容器级沙箱（需服务器有 Docker）

    # ── Per-Channel 记忆隔离（NanoClaw 启发）──
    memory_channel_isolation: bool = False   # 是否启用频道级记忆隔离（每个群聊独立记忆空间）

    # ── Cron Agent 定时任务（NanoClaw 启发）──
    cron_agent_enabled: bool = False  # 是否启用定时 Agent 任务

    # ── Channel 辅助方法 ──

    def get_channels(self) -> list[ChannelConfig]:
        if self.channels:
            return [ch for ch in self.channels if ch.enabled]
        return [self._build_primary_channel()]

    def get_channel(self, platform: str) -> Optional[ChannelConfig]:
        for ch in self.get_channels():
            if ch.platform == platform:
                return ch
        return None

    def find_channel_by_id(self, channel_id: str) -> Optional[ChannelConfig]:
        for ch in self.get_channels():
            if ch.channel_id == channel_id:
                return ch
        return None

    def get_channel_platforms(self) -> list[str]:
        return list(dict.fromkeys(ch.platform for ch in self.get_channels()))

    def has_platform(self, platform: str) -> bool:
        return any(ch.platform == platform for ch in self.get_channels())

    def _build_primary_channel(self) -> ChannelConfig:
        return ChannelConfig(
            channel_id=f"{self.tenant_id}-{self.platform}",
            platform=self.platform,
            enabled=True,
            app_id=self.app_id,
            app_secret=self.app_secret,
            verification_token=self.verification_token,
            encrypt_key=self.encrypt_key,
            oauth_redirect_uri=self.oauth_redirect_uri,
            bot_open_id=self.bot_open_id,
            bot_aliases=self.bot_aliases,
            wecom_corpid=self.wecom_corpid,
            wecom_corpsecret=self.wecom_corpsecret,
            wecom_agent_id=self.wecom_agent_id,
            wecom_token=self.wecom_token,
            wecom_encoding_aes_key=self.wecom_encoding_aes_key,
            wecom_kf_secret=self.wecom_kf_secret,
            wecom_kf_token=self.wecom_kf_token,
            wecom_kf_encoding_aes_key=self.wecom_kf_encoding_aes_key,
            wecom_kf_open_kfid=self.wecom_kf_open_kfid,
            qq_app_id=self.qq_app_id,
            qq_app_secret=self.qq_app_secret,
            qq_token=self.qq_token,
        )
