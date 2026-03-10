"""用户注册表

全局 open_id ↔ 用户名映射。
来源有两个：
1. 和 bot 对话时自动注册
2. 启动时从飞书通讯录同步组织内所有成员
3. 从 bot 所在群里拉群成员（作为 fallback）

P2P chat_id 映射持久化到 Upstash Redis（HASH p2p_chats:{tenant_id}），
重启后自动恢复，不再丢失。
"""

from __future__ import annotations

import logging
from typing import Optional

from app.tenant.context import get_current_tenant

logger = logging.getLogger(__name__)

# tenant_id -> {open_id: name}
_registries: dict[str, dict[str, str]] = {}

# tenant_id -> {open_id: p2p chat_id}
_p2p_chat_ids_map: dict[str, dict[str, str]] = {}

# tenant_id -> list[str]
_last_sync_errors_map: dict[str, list[str]] = {}


def _get_registry() -> dict[str, str]:
    tid = get_current_tenant().tenant_id
    if tid not in _registries:
        _registries[tid] = {}
    return _registries[tid]


def _get_p2p_map() -> dict[str, str]:
    tid = get_current_tenant().tenant_id
    if tid not in _p2p_chat_ids_map:
        _p2p_chat_ids_map[tid] = {}
    return _p2p_chat_ids_map[tid]


def _get_redis_key() -> str:
    return f"p2p_chats:{get_current_tenant().tenant_id}"


# ── Redis 辅助（延迟 import 避免循环引用） ──

def _redis_hset(field: str, value: str) -> None:
    try:
        from app.services.redis_client import execute, available
        if available():
            execute("HSET", _get_redis_key(), field, value)
    except Exception:
        logger.debug("redis HSET p2p_chats failed", exc_info=True)


def _redis_hget(field: str) -> str:
    try:
        from app.services.redis_client import execute, available
        if available():
            return execute("HGET", _get_redis_key(), field) or ""
    except Exception:
        logger.debug("redis HGET p2p_chats failed", exc_info=True)
    return ""


def load_p2p_chats_from_redis() -> int:
    """启动时从 Redis 恢复 P2P chat_id 映射。返回加载数量。"""
    from app.tenant.context import _current_tenant
    if _current_tenant.get() is None:
        # 启动时未设置租户上下文 -> 遍历所有租户加载
        from app.tenant.registry import tenant_registry
        from app.tenant.context import set_current_tenant
        total = 0
        for tid, tenant in tenant_registry.all_tenants().items():
            set_current_tenant(tenant)
            total += load_p2p_chats_from_redis()
        # 恢复为无租户状态，避免污染后续代码
        _current_tenant.set(None)
        return total

    try:
        from app.services.redis_client import execute, available
        if not available():
            return 0
        key = _get_redis_key()
        data = execute("HGETALL", key)
        if not data:
            return 0
        
        p2p_map = _get_p2p_map()
        # Upstash HGETALL 返回 [field, value, field, value, ...]
        if isinstance(data, list):
            for i in range(0, len(data) - 1, 2):
                p2p_map[data[i]] = data[i + 1]
            loaded = len(data) // 2
        elif isinstance(data, dict):
            p2p_map.update(data)
            loaded = len(data)
        else:
            return 0
        logger.info("loaded %d p2p_chat mappings from Redis for key %s", loaded, key)
        return loaded
    except Exception:
        logger.warning("load_p2p_chats_from_redis failed", exc_info=True)
        return 0


def register(open_id: str, name: str) -> None:
    """记录一个用户"""
    if open_id and name:
        _get_registry()[open_id] = name


def register_p2p_chat(open_id: str, chat_id: str) -> None:
    """记录用户的私聊 chat_id（oc_xxx 格式），供 fetch_chat_history 查询。
    同时写入 Redis 持久化。
    """
    if open_id and chat_id and chat_id.startswith("oc_"):
        _get_p2p_map()[open_id] = chat_id
        _redis_hset(open_id, chat_id)


def get_p2p_chat_id(open_id: str) -> str:
    """通过 open_id 查私聊 chat_id。内存优先，miss 时回退 Redis。"""
    p2p_map = _get_p2p_map()
    cached = p2p_map.get(open_id)
    if cached:
        return cached
    # 内存没有 → 查 Redis（覆盖重启后内存还没恢复完的场景）
    from_redis = _redis_hget(open_id)
    if from_redis:
        p2p_map[open_id] = from_redis
    return from_redis


def get_name(open_id: str) -> str:
    """通过 open_id 查名字"""
    return _get_registry().get(open_id, "")


def find_by_name(name: str) -> Optional[str]:
    """通过名字查 open_id（模糊匹配）"""
    name = name.strip()
    if not name:
        return None

    registry = _get_registry()
    # 精确匹配
    for oid, n in registry.items():
        if n == name:
            return oid

    # 包含匹配
    for oid, n in registry.items():
        if name in n or n in name:
            return oid

    return None


def all_users() -> dict[str, str]:
    """返回所有已知用户 {open_id: name}"""
    return dict(_get_registry())


def summary() -> str:
    """返回用户列表的文字摘要，供 system prompt 注入"""
    registry = _get_registry()
    if not registry:
        return ""
    lines = ["已知团队成员（名字 → open_id）："]
    for oid, name in registry.items():
        lines.append(f"  - {name}: {oid}")
    return "\n".join(lines)


def last_sync_errors() -> list[str]:
    """返回最近一次同步的错误信息"""
    tid = get_current_tenant().tenant_id
    return list(_last_sync_errors_map.get(tid, []))


# ── 通讯录同步 ──

def sync_org_contacts() -> int:
    """从飞书通讯录拉取组织内所有成员，注册到 _registry。

    使用 tenant_access_token（bot 身份），需要应用开通:
    - contact:user.base:readonly（用户基本信息）
    - contact:department.base:readonly（部门基本信息）
    - 通讯录可见范围设为「全部员工」

    递归遍历所有部门。返回新增用户数。
    """
    from app.tenant.context import _current_tenant
    if _current_tenant.get() is None:
        # 启动时未设置租户上下文 -> 遍历所有飞书租户同步
        from app.tenant.registry import tenant_registry
        from app.tenant.context import set_current_tenant
        total = 0
        for tid, tenant in tenant_registry.all_tenants().items():
            if tenant.platform != "feishu":
                continue
            set_current_tenant(tenant)
            total += sync_org_contacts()
        _current_tenant.set(None)
        return total

    from app.tools.feishu_api import feishu_get

    tid = get_current_tenant().tenant_id
    if tid not in _last_sync_errors_map:
        _last_sync_errors_map[tid] = []
    errors = _last_sync_errors_map[tid]
    errors.clear()
    
    registry = _get_registry()
    added = 0
    # 队列存 (dept_id, dept_id_type)
    # 根部门 "0" 用默认 department_id 类型，子部门统一用 open_department_id
    departments: list[tuple[str, str]] = [("0", "department_id")]
    visited_depts: set[str] = set()

    while departments:
        dept_id, dept_id_type = departments.pop()
        if dept_id in visited_depts:
            continue
        visited_depts.add(dept_id)

        # ── 拉取该部门下的用户 ──
        page_token = ""
        while True:
            params: dict = {
                "department_id": dept_id,
                "department_id_type": dept_id_type,
                "page_size": 50,
                "user_id_type": "open_id",
            }
            if page_token:
                params["page_token"] = page_token

            data = feishu_get("/contact/v3/users/find_by_department", params=params)
            if isinstance(data, str):
                errors.append(f"部门 {dept_id} 用户列表: {data}")
                break

            items = data.get("data", {}).get("items", [])
            for user in items:
                oid = user.get("open_id", "")
                name = user.get("name", "")
                if oid and name and oid not in registry:
                    registry[oid] = name
                    added += 1

            if not data.get("data", {}).get("has_more", False):
                break
            page_token = data.get("data", {}).get("page_token", "")
            if not page_token:
                break

        # ── 拉取子部门 ──
        child_page_token = ""
        while True:
            child_params: dict = {
                "department_id_type": dept_id_type,
                "page_size": 50,
            }
            if child_page_token:
                child_params["page_token"] = child_page_token

            child_data = feishu_get(
                f"/contact/v3/departments/{dept_id}/children",
                params=child_params,
            )
            if isinstance(child_data, str):
                errors.append(f"部门 {dept_id} 子部门: {child_data}")
                break

            child_items = child_data.get("data", {}).get("items", [])
            for dept in child_items:
                # 子部门统一用 open_department_id，避免 400 错误
                child_dept_id = dept.get("open_department_id", "")
                if child_dept_id and child_dept_id not in visited_depts:
                    departments.append((child_dept_id, "open_department_id"))

            if not child_data.get("data", {}).get("has_more", False):
                break
            child_page_token = child_data.get("data", {}).get("page_token", "")
            if not child_page_token:
                break

    logger.info("sync_org_contacts done for tenant %s: %d new, %d total, %d errors",
                tid, added, len(registry), len(errors))
    return added


def sync_from_bot_groups() -> int:
    """从 bot 所在群的成员列表拉取用户（通讯录 fallback）。

    不需要通讯录权限，只需要 im:chat:readonly。
    """
    from app.tenant.context import _current_tenant
    if _current_tenant.get() is None:
        # 启动时未设置租户上下文 -> 遍历所有飞书租户同步
        from app.tenant.registry import tenant_registry
        from app.tenant.context import set_current_tenant
        total = 0
        for tid, tenant in tenant_registry.all_tenants().items():
            if tenant.platform != "feishu":
                continue
            set_current_tenant(tenant)
            total += sync_from_bot_groups()
        _current_tenant.set(None)
        return total

    from app.tools.feishu_api import feishu_get

    tid = get_current_tenant().tenant_id
    if tid not in _last_sync_errors_map:
        _last_sync_errors_map[tid] = []
    errors = _last_sync_errors_map[tid]
    
    registry = _get_registry()
    added = 0

    # 拉取 bot 所在的群
    chats_data = feishu_get("/im/v1/chats", params={"page_size": 50})
    if isinstance(chats_data, str):
        errors.append(f"群列表: {chats_data}")
        return added

    chats = chats_data.get("data", {}).get("items", [])
    for chat in chats:
        chat_id = chat.get("chat_id", "")
        if not chat_id:
            continue

        # 拉取群成员
        member_page_token = ""
        while True:
            member_params: dict = {
                "member_id_type": "open_id",
                "page_size": 100,
            }
            if member_page_token:
                member_params["page_token"] = member_page_token

            member_data = feishu_get(
                f"/im/v1/chats/{chat_id}/members",
                params=member_params,
            )
            if isinstance(member_data, str):
                errors.append(f"群 {chat_id[:15]} 成员: {member_data}")
                break

            members = member_data.get("data", {}).get("items", [])
            for m in members:
                oid = m.get("member_id", "")
                name = m.get("name", "")
                if oid and name and oid not in registry:
                    registry[oid] = name
                    added += 1

            if not member_data.get("data", {}).get("has_more", False):
                break
            member_page_token = member_data.get("data", {}).get("page_token", "")
            if not member_page_token:
                break

    logger.info("sync_from_bot_groups done for tenant %s: %d new, %d total", tid, added, len(registry))
    return added
