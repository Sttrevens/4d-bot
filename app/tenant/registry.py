"""租户注册表

管理所有租户的配置。支持两种加载方式:
1. 从环境变量构建默认租户（兼容现有部署）
2. 从 JSON 配置文件加载多租户（产品化部署）
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from app.tenant.config import TenantConfig, ChannelConfig

logger = logging.getLogger(__name__)

_DEFAULT_TENANT_ID = "default"

_ENV_VAR_RE = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")


def _resolve_env(value):
    """递归解析值中的 ${VAR} 或 ${VAR:default} 占位符。

    支持字符串、列表、字典的递归解析。
    """
    if isinstance(value, str):
        def _replacer(m):
            var_name, default = m.group(1), m.group(2)
            return os.environ.get(var_name, default if default is not None else "")
        return _ENV_VAR_RE.sub(_replacer, value)
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    return value


def _dict_to_tenant(d: dict) -> TenantConfig:
    """将 dict 转为 TenantConfig，正确处理 channels 子对象。

    如果 dict 中有 "channels" 列表，每个元素转为 ChannelConfig。
    未知字段（不在 dataclass 中的 key）会被安全忽略。
    """
    # 提取 channels 列表，单独处理
    channels_raw = d.pop("channels", None) or []
    channels = []
    for ch_dict in channels_raw:
        if isinstance(ch_dict, dict):
            # 只传 ChannelConfig 能接受的字段
            ch_fields = {f.name for f in ChannelConfig.__dataclass_fields__.values()}
            filtered = {k: v for k, v in ch_dict.items() if k in ch_fields}
            channels.append(ChannelConfig(**filtered))
        elif isinstance(ch_dict, ChannelConfig):
            channels.append(ch_dict)

    # 只传 TenantConfig 能接受的字段（忽略未知字段）
    tc_fields = {f.name for f in TenantConfig.__dataclass_fields__.values()}
    filtered = {k: v for k, v in d.items() if k in tc_fields}
    filtered["channels"] = channels
    return TenantConfig(**filtered)


class TenantRegistry:
    def __init__(self) -> None:
        self._tenants: dict[str, TenantConfig] = {}
        self._default_tenant_id: str = _DEFAULT_TENANT_ID

    # ── 注册 ──

    def register(self, tenant: TenantConfig) -> None:
        """注册一个租户"""
        self._tenants[tenant.tenant_id] = tenant
        logger.info("tenant registered: id=%s name=%s platform=%s",
                     tenant.tenant_id, tenant.name, tenant.platform)

    def register_from_dict(self, config: dict) -> TenantConfig:
        """从 dict 创建并注册租户（热加载用，支持 ${VAR} 解析）。"""
        resolved = _resolve_env(config)
        tenant = _dict_to_tenant(resolved)
        self.register(tenant)
        return tenant

    def unregister(self, tenant_id: str) -> bool:
        """移除租户。返回 True 如果确实移除了。"""
        if tenant_id in self._tenants:
            del self._tenants[tenant_id]
            logger.info("tenant unregistered: id=%s", tenant_id)
            return True
        return False

    def reload_from_file(self, path: str | Path | None = None) -> int:
        """重新加载 tenants.json，更新/新增租户（不删除已有）。"""
        if path is None:
            # Auto-detect: 容器内 /app/tenants.json 或项目根目录
            for candidate in ("/app/tenants.json",):
                if Path(candidate).exists():
                    path = candidate
                    break
        if not path or not Path(path).exists():
            logger.warning("reload_from_file: no tenants file found")
            return 0
        return self.load_from_file(path)

    # ── 查询 ──

    def get(self, tenant_id: str) -> TenantConfig | None:
        return self._tenants.get(tenant_id)

    def get_default(self) -> TenantConfig:
        """获取默认租户。如果没有注册任何租户，从环境变量创建一个。"""
        tenant = self._tenants.get(self._default_tenant_id)
        if tenant:
            return tenant
        # 首次调用：从环境变量创建默认租户
        tenant = self._create_default_from_env()
        self._tenants[self._default_tenant_id] = tenant
        return tenant

    def all_tenants(self) -> dict[str, TenantConfig]:
        return dict(self._tenants)

    def find_by_app_id(self, app_id: str) -> TenantConfig | None:
        """通过飞书 app_id 查找租户（webhook 回调时可用于自动识别）"""
        for tenant in self._tenants.values():
            if tenant.app_id == app_id:
                return tenant
            # 也搜索 channels
            for ch in tenant.channels:
                if ch.app_id == app_id:
                    return tenant
        return None

    def find_by_channel_id(self, channel_id: str) -> tuple[TenantConfig | None, ChannelConfig | None]:
        """通过 channel_id 查找 (tenant, channel) 对。"""
        for tenant in self._tenants.values():
            ch = tenant.find_channel_by_id(channel_id)
            if ch:
                return tenant, ch
        return None, None

    def find_by_kf_open_kfid(self, open_kfid: str) -> tuple[TenantConfig | None, ChannelConfig | None]:
        """通过微信客服 open_kfid 查找 (tenant, channel)。"""
        for tenant in self._tenants.values():
            for ch in tenant.get_channels():
                if ch.platform == "wecom_kf" and ch.wecom_kf_open_kfid == open_kfid:
                    return tenant, ch
        return None, None

    # ── 加载 ──

    def load_from_file(self, path: str | Path) -> int:
        """从 JSON 文件加载租户配置。返回加载的租户数。

        JSON 格式:
        {
            "tenants": [
                {
                    "tenant_id": "team-alpha",
                    "name": "Alpha 团队",
                    "platform": "feishu",
                    "app_id": "cli_xxxx",
                    "app_secret": "xxxx",
                    ...
                }
            ],
            "default_tenant_id": "team-alpha"
        }
        """
        path = Path(path)
        if not path.exists():
            logger.warning("tenants config file not found: %s", path)
            return 0

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        tenants = data.get("tenants", [])
        for item in tenants:
            item = _resolve_env(item)
            tenant = _dict_to_tenant(item)
            if not tenant.tenant_id:
                logger.warning("skipping tenant without tenant_id: %s", item.get("name"))
                continue
            self.register(tenant)

        if data.get("default_tenant_id"):
            self._default_tenant_id = data["default_tenant_id"]

        logger.info("loaded %d tenants from %s (default=%s)",
                     len(tenants), path, self._default_tenant_id)
        return len(tenants)

    def load_default_from_env(self) -> None:
        """从环境变量创建默认租户并注册（兼容现有单租户部署）"""
        tenant = self._create_default_from_env()
        self._tenants[tenant.tenant_id] = tenant

    # ── 内部 ──

    @staticmethod
    def _create_default_from_env() -> TenantConfig:
        """从当前环境变量构建默认租户配置"""
        from app.config import settings
        return TenantConfig(
            tenant_id=_DEFAULT_TENANT_ID,
            name="Default",
            platform="feishu",
            app_id=settings.feishu.app_id,
            app_secret=settings.feishu.app_secret,
            verification_token=settings.feishu.verification_token,
            encrypt_key=settings.feishu.encrypt_key,
            oauth_redirect_uri=settings.feishu.oauth_redirect_uri,
            github_token=settings.github.token,
            github_repo_owner=settings.github.repo_owner,
            github_repo_name=settings.github.repo_name,
            llm_api_key=settings.kimi.api_key,
            llm_base_url=settings.kimi.base_url,
            llm_model=settings.kimi.model,
            llm_system_prompt=settings.kimi.chat_system_prompt,
            stt_api_key=settings.stt.api_key,
            stt_base_url=settings.stt.base_url,
            stt_model=settings.stt.model,
            admin_open_ids=settings.admin_open_ids,
            admin_names=settings.admin_names,
            self_iteration_enabled=True,  # 默认租户（平台管理员）开启自我迭代
        )


# 全局单例
tenant_registry = TenantRegistry()
