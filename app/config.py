from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str = field(default_factory=lambda: _env("FEISHU_APP_ID"))
    app_secret: str = field(default_factory=lambda: _env("FEISHU_APP_SECRET"))
    verification_token: str = field(
        default_factory=lambda: _env("FEISHU_VERIFICATION_TOKEN")
    )
    encrypt_key: str = field(default_factory=lambda: _env("FEISHU_ENCRYPT_KEY"))
    # OAuth: 部署地址 + /oauth/callback，如 https://your-domain.com/oauth/callback
    oauth_redirect_uri: str = field(
        default_factory=lambda: _env("FEISHU_OAUTH_REDIRECT_URI")
    )


@dataclass(frozen=True)
class KimiConfig:
    api_key: str = field(default_factory=lambda: _env("KIMI_API_KEY"))
    base_url: str = field(
        default_factory=lambda: _env(
            "KIMI_BASE_URL", "https://api.moonshot.cn/v1"
        )
    )
    model: str = field(
        default_factory=lambda: _env("KIMI_MODEL", "kimi-k2.5")
    )
    chat_system_prompt: str = field(
        default_factory=lambda: _env(
            "KIMI_CHAT_SYSTEM_PROMPT",
            "你是飞书群里的智能助手，友好、简洁地回答用户问题。如果问题涉及编程但不需要实际操作代码仓库，也可以直接给出建议。",
        )
    )


@dataclass(frozen=True)
class SttConfig:
    """语音转写 (Speech-to-Text) 配置，独立于 LLM 配置。

    支持 OpenAI-compatible Whisper API（如 OpenAI、Groq、本地部署等）。
    """
    api_key: str = field(default_factory=lambda: _env("STT_API_KEY"))
    base_url: str = field(
        default_factory=lambda: _env("STT_BASE_URL", "https://api.openai.com/v1")
    )
    model: str = field(
        default_factory=lambda: _env("STT_MODEL", "whisper-1")
    )


@dataclass(frozen=True)
class AnthropicConfig:
    api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    model: str = field(
        default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    )


@dataclass(frozen=True)
class GitHubConfig:
    token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    repo_owner: str = field(default_factory=lambda: _env("GITHUB_REPO_OWNER"))
    repo_name: str = field(default_factory=lambda: _env("GITHUB_REPO_NAME"))
    local_repo_path: str = field(
        default_factory=lambda: _env("GITHUB_LOCAL_REPO_PATH", "/tmp/workspace")
    )


@dataclass(frozen=True)
class RailwayConfig:
    """Railway 部署平台配置（已迁移到阿里云，保留向后兼容）"""
    api_token: str = field(default_factory=lambda: _env("RAILWAY_API_TOKEN"))
    project_id: str = field(default_factory=lambda: _env("RAILWAY_PROJECT_ID"))
    service_id: str = field(default_factory=lambda: _env("RAILWAY_SERVICE_ID"))
    environment_id: str = field(default_factory=lambda: _env("RAILWAY_ENVIRONMENT_ID"))


@dataclass(frozen=True)
class SelffixConfig:
    """自我修复专用 LLM 配置（独立于正常对话，可用更强的模型）

    留空则回退到 Kimi 配置（settings.kimi.*）。
    设置 provider="gemini" + api_key + model 即可用 Gemini 强模型做 selffix。
    """
    provider: str = field(default_factory=lambda: _env("SELFFIX_PROVIDER", ""))
    api_key: str = field(default_factory=lambda: _env("SELFFIX_API_KEY"))
    base_url: str = field(default_factory=lambda: _env("SELFFIX_BASE_URL"))
    model: str = field(default_factory=lambda: _env("SELFFIX_MODEL"))


@dataclass(frozen=True)
class Settings:
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    kimi: KimiConfig = field(default_factory=KimiConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    railway: RailwayConfig = field(default_factory=RailwayConfig)
    selffix: SelffixConfig = field(default_factory=SelffixConfig)
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(
        default_factory=lambda: int(_env("PORT", "8000"))
    )
    debug: bool = field(
        default_factory=lambda: _env("DEBUG", "false").lower() == "true"
    )
    # 管理员配置：逗号分隔的 open_id 或名字，匹配到的用户享受无条件服从
    admin_open_ids: list[str] = field(
        default_factory=lambda: [x.strip() for x in _env("ADMIN_OPEN_IDS", "").split(",") if x.strip()]
    )
    admin_names: list[str] = field(
        default_factory=lambda: [x.strip() for x in _env("ADMIN_NAMES", "Admin").split(",") if x.strip()]
    )
    # 自我迭代：bot 自己的仓库（默认与 GITHUB_REPO 相同）
    self_repo_owner: str = field(
        default_factory=lambda: _env("SELF_REPO_OWNER") or _env("GITHUB_REPO_OWNER")
    )
    self_repo_name: str = field(
        default_factory=lambda: _env("SELF_REPO_NAME") or _env("GITHUB_REPO_NAME")
    )
    # 多租户配置文件路径（可选，不配则使用环境变量作为默认单租户）
    tenants_config_path: str = field(
        default_factory=lambda: _env("TENANTS_CONFIG_PATH", "")
    )


settings = Settings()
