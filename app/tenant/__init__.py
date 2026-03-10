"""多租户支持模块

每个租户对应一个独立的 IM 平台 app 实例（飞书 app、企微 app 等），
拥有独立的凭证、GitHub 仓库、LLM 配置、管理员列表等。

使用方式:
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    tenant.app_id  # 当前请求所属租户的 app_id
"""

from app.tenant.config import TenantConfig
from app.tenant.registry import TenantRegistry, tenant_registry
from app.tenant.context import get_current_tenant, set_current_tenant

__all__ = [
    "TenantConfig",
    "TenantRegistry",
    "tenant_registry",
    "get_current_tenant",
    "set_current_tenant",
]
