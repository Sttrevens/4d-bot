"""Admin Dashboard API + 页面

所有接口需要 Bearer token 认证（ADMIN_TOKEN 环境变量）。
Dashboard 是纯前端单页应用，通过 JSON API 获取数据。

跨容器架构：每个容器只加载自己的租户，但 admin dashboard 需要看到所有租户。
解决方案：每个容器启动时将自己的租户元数据发布到 Redis（admin:tenant:{tid}），
admin API 从 Redis 读取全量租户列表，实现跨容器可见。
"""

from __future__ import annotations

import json
import os
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.tenant.registry import tenant_registry
from app.tenant.config import ChannelConfig
from app.services import redis_client as redis
from app.services.metering import get_usage_summary, get_daily_breakdown
from app.services.trial import (
    list_trial_users, get_user_info, approve_user, block_user,
    reset_user, set_user_notes,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
_security = HTTPBearer(auto_error=False)

_DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"

_TENANT_META_FIELDS = (
    "tenant_id", "name", "platform", "trial_enabled", "trial_duration_hours",
    "approval_duration_days", "quota_user_tokens_6h", "quota_monthly_api_calls",
    "quota_monthly_tokens", "rate_limit_rpm", "rate_limit_user_rpm",
    "deploy_free_quota", "wecom_kf_open_kfid",
    "memory_diary_enabled", "memory_context_enabled", "memory_chat_rounds",
    "memory_chat_ttl", "memory_journal_max", "custom_persona",
)

_REMOVED_TENANTS_KEY = "admin:removed_tenants"


def _get_removed_tenants() -> set[str]:
    """获取已标记删除的租户 ID 集合"""
    try:
        if redis.available():
            members = redis.execute("SMEMBERS", _REMOVED_TENANTS_KEY)
            if isinstance(members, list):
                return set(members)
    except Exception:
        logger.warning("_get_removed_tenants failed", exc_info=True)
    return set()


def _mark_tenant_removed(tenant_id: str) -> None:
    """标记租户为已删除（防止其他容器重新发布）"""
    try:
        if redis.available():
            redis.execute("SADD", _REMOVED_TENANTS_KEY, tenant_id)
    except Exception:
        logger.warning("Failed to mark tenant %s as removed", tenant_id, exc_info=True)


def publish_tenant_meta() -> int:
    """Publish local tenant metadata to Redis for cross-container admin visibility.

    Called from main.py startup. Each container publishes its own tenants.
    No TTL — keys persist until explicitly deleted. Prevents dashboard losing
    tenants when containers are temporarily down.
    """
    if not redis.available():
        logger.warning("publish_tenant_meta: Redis not available (missing UPSTASH env vars?), skipping %d tenants",
                        len(tenant_registry.all_tenants()))
        return 0
    # 获取已标记删除的租户，避免重新发布
    removed = _get_removed_tenants()
    count = 0
    failed = 0
    for tid, t in tenant_registry.all_tenants().items():
        if tid in removed:
            continue  # 已被 dashboard 删除，不重新发布
        meta = {f: getattr(t, f, None) for f in _TENANT_META_FIELDS}
        meta["tenant_id"] = tid
        try:
            result = redis.execute(
                "SET", f"admin:tenant:{tid}",
                json.dumps(meta, ensure_ascii=False),
            )
            if result is None:
                # Circuit breaker open or silent failure — key was NOT written
                failed += 1
                logger.warning("publish_tenant_meta: SET returned None for %s (circuit breaker open?)", tid)
            else:
                count += 1
        except Exception:
            failed += 1
            logger.warning("publish_tenant_meta failed for %s", tid, exc_info=True)
    if failed:
        logger.warning("publish_tenant_meta: %d/%d tenants failed to publish — "
                        "other containers will NOT see these tenants in dashboard",
                        failed, count + failed)
    return count


def _scan_redis_keys(pattern: str) -> list[tuple[str, str]]:
    """SCAN Redis for keys matching pattern. Returns [(extracted_id, full_key), ...]."""
    results: list[tuple[str, str]] = []
    prefix = pattern.replace("*", "")  # e.g. "admin:tenant:" or "tenant_cfg:"
    cursor = "0"
    for _ in range(50):
        result = redis.execute("SCAN", cursor, "MATCH", pattern, "COUNT", "50")
        if not result or not isinstance(result, list) or len(result) < 2:
            break
        cursor = str(result[0])
        keys = result[1] if isinstance(result[1], list) else []
        for key in keys:
            tid = key.replace(prefix, "", 1) if isinstance(key, str) else ""
            if tid:
                results.append((tid, key))
        if cursor == "0":
            break
    return results


def _get_all_tenants() -> list[dict]:
    """Get all tenant metadata from local registry + Redis (cross-container).

    Three sources (priority order):
    1. Local registry (current container's tenants)
    2. Redis admin:tenant:* (metadata published by all containers, no TTL)
    3. Redis tenant_cfg:* (full configs for dashboard-added tenants, no TTL)

    Excludes tenants in admin:removed_tenants set.
    """
    removed = _get_removed_tenants()
    tenants_map: dict[str, dict] = {}

    # Source 1: Local registry (exclude removed)
    for tid, t in tenant_registry.all_tenants().items():
        if tid in removed:
            continue
        tenants_map[tid] = {f: getattr(t, f, None) for f in _TENANT_META_FIELDS}
        tenants_map[tid]["tenant_id"] = tid

    # Source 2 + 3: Redis
    if redis.available():
        try:
            # Phase 1: SCAN admin:tenant:* (metadata from all containers)
            remote_keys = [
                (tid, key) for tid, key in _scan_redis_keys("admin:tenant:*")
                if tid not in tenants_map and tid not in removed
            ]

            logger.info("_get_all_tenants: found %d remote admin:tenant:* keys (excluding %d local)",
                        len(remote_keys), len(tenants_map))

            # Phase 2: SCAN tenant_cfg:* (dashboard-added tenants, full config)
            cfg_keys = [
                (tid, key) for tid, key in _scan_redis_keys("tenant_cfg:*")
                if tid not in tenants_map and tid not in removed and not any(t == tid for t, _ in remote_keys)
            ]
            logger.info("_get_all_tenants: found %d tenant_cfg:* keys", len(cfg_keys))

            # Phase 3: pipeline batch GET
            all_keys = remote_keys + cfg_keys
            if all_keys:
                commands = [["GET", key] for _, key in all_keys]
                responses = redis.pipeline(commands)
                for (tid, _), data in zip(all_keys, responses):
                    if data and tid not in tenants_map:
                        try:
                            parsed = json.loads(data)
                            # tenant_cfg:* has full config; extract meta fields
                            meta = {}
                            for f in _TENANT_META_FIELDS:
                                if f in parsed:
                                    meta[f] = parsed[f]
                            meta["tenant_id"] = tid
                            tenants_map[tid] = meta
                        except (json.JSONDecodeError, TypeError):
                            pass
        except Exception:
            logger.warning("_get_all_tenants Redis scan failed", exc_info=True)
    else:
        logger.info("_get_all_tenants: Redis not available, returning only %d local tenants", len(tenants_map))

    logger.info("_get_all_tenants: returning %d tenants total", len(tenants_map))
    return sorted(tenants_map.values(), key=lambda t: t.get("tenant_id", ""))


def _resolve_tenant(tenant_id: str):
    """Resolve a TenantConfig from local registry or Redis.

    Returns TenantConfig or None. For remote tenants, constructs a minimal
    TenantConfig from Redis data so that set_current_tenant() works.
    """
    # 1) Local registry (best)
    t = tenant_registry.get(tenant_id)
    if t:
        return t

    if not redis.available():
        return None

    # 2) Redis tenant_cfg: (full config from dashboard)
    raw = redis.execute("GET", f"tenant_cfg:{tenant_id}")
    if raw:
        try:
            data = json.loads(raw)
            from app.tenant.config import TenantConfig
            t = TenantConfig(tenant_id=tenant_id, **{
                k: v for k, v in data.items()
                if k != "tenant_id" and hasattr(TenantConfig, k)
            })
            return t
        except Exception:
            logger.warning("_resolve_tenant: failed to construct from tenant_cfg:%s", tenant_id, exc_info=True)

    # 3) Redis admin:tenant: (metadata only — minimal config)
    raw = redis.execute("GET", f"admin:tenant:{tenant_id}")
    if raw:
        try:
            meta = json.loads(raw)
            from app.tenant.config import TenantConfig
            t = TenantConfig(
                tenant_id=tenant_id,
                name=meta.get("name", ""),
                platform=meta.get("platform", "feishu"),
            )
            return t
        except Exception:
            logger.warning("_resolve_tenant: failed to construct from admin:tenant:%s", tenant_id, exc_info=True)

    return None


# ── 认证 ──

async def _verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    """验证 admin Bearer token"""
    import hmac
    if not _ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN not configured on server")
    if not credentials or not hmac.compare_digest(credentials.credentials, _ADMIN_TOKEN):
        raise HTTPException(401, "Invalid or missing admin token")
    return credentials.credentials


# ── Dashboard 页面 ──

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """返回 dashboard 单页应用 HTML"""
    if _DASHBOARD_HTML.exists():
        return HTMLResponse(_DASHBOARD_HTML.read_text("utf-8"))
    return HTMLResponse("<h1>Dashboard HTML not found</h1>", status_code=500)


# ── Redis 诊断 ──

@router.get("/api/redis-debug")
async def api_redis_debug(_token: str = Depends(_verify_token)):
    """Redis connectivity diagnostics for debugging cross-container visibility."""
    diag = redis.diagnostics()

    # Check admin:tenant:* keys
    admin_keys = []
    cfg_keys = []
    if diag["ping"]:
        admin_keys = _scan_redis_keys("admin:tenant:*")
        cfg_keys = _scan_redis_keys("tenant_cfg:*")

    return JSONResponse({
        "redis": diag,
        "local_tenants": list(tenant_registry.all_tenants().keys()),
        "admin_tenant_keys": [tid for tid, _ in admin_keys],
        "tenant_cfg_keys": [tid for tid, _ in cfg_keys],
        "summary": {
            "local_count": len(tenant_registry.all_tenants()),
            "admin_tenant_count": len(admin_keys),
            "tenant_cfg_count": len(cfg_keys),
        },
    })


# ── 租户列表 ──

@router.get("/api/tenants")
async def api_tenants(_token: str = Depends(_verify_token)):
    """列出所有租户基本信息（跨容器，从 Redis 读取）+ co-host 关系"""
    tenants = _get_all_tenants()

    # Build co-host mapping from provisioner registry
    co_host_map = {}  # tenant_id → host instance_id
    try:
        from app.services.provisioner import _registry, INSTANCES_DIR
        for tid, info in _registry.list_all().items():
            inst_tenants = INSTANCES_DIR / tid / "tenants.json"
            if not inst_tenants.exists():
                continue
            try:
                data = json.loads(inst_tenants.read_text())
                for t in data.get("tenants", []):
                    t_id = t.get("tenant_id", "")
                    if t_id and t_id != tid:
                        co_host_map[t_id] = tid
            except Exception:
                logger.warning("Failed to parse co-tenant config for %s", tid, exc_info=True)
    except Exception:
        logger.warning("Failed to build co-host map from provisioner", exc_info=True)

    for t in tenants:
        tid = t.get("tenant_id", "")
        if tid in co_host_map:
            t["host_instance"] = co_host_map[tid]

    return {"tenants": tenants}


# ── 租户配置编辑 ──

# 可通过 dashboard 安全编辑的字段（不含密钥/凭证）
_EDITABLE_FIELDS = (
    "name", "llm_system_prompt", "custom_persona", "greeting_message",
    "trial_enabled", "trial_duration_hours", "approval_duration_days",
    "quota_user_tokens_6h", "quota_monthly_api_calls", "quota_monthly_tokens",
    "rate_limit_rpm", "rate_limit_user_rpm", "deploy_free_quota",
    "memory_diary_enabled", "memory_context_enabled", "memory_org_recall_enabled",
    "memory_journal_max", "memory_chat_rounds", "memory_chat_ttl",
    "admin_names", "tools_enabled", "capability_modules",
    "self_iteration_enabled", "instance_management_enabled",
    "coworker_mode_enabled", "coworker_scan_interval_hours",
    "coworker_scan_groups", "coworker_msg_count",
    "coworker_quiet_hours_start", "coworker_quiet_hours_end",
    "allowed_users", "owner", "access_deny_msg",
)


@router.get("/api/tenants/{tenant_id}/config")
async def api_get_tenant_config(
    tenant_id: str, _token: str = Depends(_verify_token),
):
    """获取租户完整可编辑配置（不含密钥）"""
    # 1) 本地 registry（字段最全，直接从内存读）
    t = tenant_registry.get(tenant_id)
    if t:
        config = {f: getattr(t, f, None) for f in _EDITABLE_FIELDS}
        config["tenant_id"] = tenant_id
        config["platform"] = t.platform
        config["_source"] = "local"
        return config

    if not redis.available():
        raise HTTPException(404, f"Tenant {tenant_id} not found")

    # 2) Redis tenant_cfg:（dashboard 添加/编辑过的完整配置）
    raw = redis.execute("GET", f"tenant_cfg:{tenant_id}")
    if raw:
        try:
            full = json.loads(raw)
            config = {f: full.get(f) for f in _EDITABLE_FIELDS}
            config["tenant_id"] = tenant_id
            config["platform"] = full.get("platform", "")
            config["_source"] = "redis_cfg"
            return config
        except (json.JSONDecodeError, TypeError):
            pass

    # 3) Redis admin:tenant: 元数据（只有摘要，标记为 partial）
    raw = redis.execute("GET", f"admin:tenant:{tenant_id}")
    if raw:
        try:
            meta = json.loads(raw)
            meta["_source"] = "redis_meta_partial"
            return meta
        except (json.JSONDecodeError, TypeError):
            pass

    raise HTTPException(404, f"Tenant {tenant_id} not found")


@router.put("/api/tenants/{tenant_id}/config")
async def api_update_tenant_config(
    tenant_id: str, request: Request, _token: str = Depends(_verify_token),
):
    """更新租户配置（非密钥字段）

    支持本地租户和跨容器租户（通过 Redis tenant_cfg: 读取完整配置）。
    """
    body = await request.json()

    # 只允许编辑安全字段
    updates = {k: v for k, v in body.items() if k in _EDITABLE_FIELDS}
    if not updates:
        raise HTTPException(400, "No editable fields provided")

    # 1) 获取当前完整配置（本地 registry → Redis tenant_cfg: → 失败）
    from dataclasses import asdict
    full_config = None

    t = tenant_registry.get(tenant_id)
    if t:
        full_config = asdict(t)
    elif redis.available():
        raw = redis.execute("GET", f"tenant_cfg:{tenant_id}")
        if raw:
            try:
                full_config = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass

    if not full_config:
        raise HTTPException(404, f"Tenant {tenant_id} not found (local or Redis)")

    # 2) 叠加编辑字段
    full_config.update(updates)

    # 3) 更新本地 registry（如果是本地租户或已被热加载的）
    try:
        tenant_registry.register_from_dict(full_config)
        logger.info("Updated tenant %s config in local registry: %s", tenant_id, list(updates.keys()))
    except Exception as e:
        logger.warning("register_from_dict for %s: %s (may be cross-container)", tenant_id, e)

    # 4) 写入本地 tenants.json（只写 updates，不覆盖 ${VAR}；:ro 挂载下会静默失败）
    _update_local_tenants_json(tenant_id, updates)

    # 5) 持久化到 Redis + 通知其他容器
    from app.services.tenant_sync import publish_tenant_update
    synced = publish_tenant_update("update", full_config)

    # 6) 更新 Redis meta
    try:
        publish_tenant_meta()
    except Exception:
        logger.warning("publish_tenant_meta failed after config update for %s", tenant_id, exc_info=True)

    return {
        "ok": True, "tenant_id": tenant_id,
        "updated_fields": list(updates.keys()),
        "synced_to_redis": synced,
    }


def _update_local_tenants_json(tenant_id: str, updates: dict):
    """更新本容器的 tenants.json 中指定租户的配置。

    只写入 updates 中提供的字段，不触碰原有 ${VAR} 引用。
    """
    from pathlib import Path
    for candidate in ("/app/tenants.json", "tenants.json"):
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            tenants = data.get("tenants", [])
            found = False
            for i, t in enumerate(tenants):
                if t.get("tenant_id") == tenant_id:
                    # 只更新用户实际修改的安全字段
                    safe_updates = {k: v for k, v in updates.items()
                                    if k in _EDITABLE_FIELDS}
                    tenants[i].update(safe_updates)
                    found = True
                    break
            if found:
                data["tenants"] = tenants
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                logger.info("Updated tenant %s in %s (fields: %s)",
                            tenant_id, path, list(updates.keys()))
            return
        except Exception as e:
            logger.warning("_update_local_tenants_json failed for %s: %s", tenant_id, e)


# ── 白名单管理 ──

@router.get("/api/tenants/{tenant_id}/allowed-users")
async def api_get_allowed_users(tenant_id: str, _token: str = Depends(_verify_token)):
    """获取租户白名单"""
    t = _resolve_tenant(tenant_id)
    if not t:
        raise HTTPException(404, f"Tenant {tenant_id} not found")
    return {
        "tenant_id": tenant_id,
        "allowed_users": t.allowed_users or [],
        "owner": t.owner or "",
        "access_deny_msg": t.access_deny_msg,
    }


@router.get("/api/tenants/{tenant_id}/chat-users")
async def api_get_chat_users(tenant_id: str, _token: str = Depends(_verify_token)):
    """获取跟该 bot 聊过天的用户列表（从 user_registry 读取）。
    用于白名单下拉选择。"""
    from app.services import user_registry
    from app.tenant.context import set_current_tenant
    t = _resolve_tenant(tenant_id)
    if not t:
        raise HTTPException(404, f"Tenant {tenant_id} not found")
    # 临时设置 tenant context 读取对应的 registry
    set_current_tenant(t)
    all_known = user_registry.all_users()
    # 如果内存为空（可能是 hot-loaded 租户，startup 时没加载），从 Redis 补加载
    if not all_known:
        user_registry.load_user_names_from_redis()
        all_known = user_registry.all_users()
    # 已在白名单中的标记
    allowed_ids = {u.get("external_userid", "") for u in (t.allowed_users or []) if isinstance(u, dict)}
    users = []
    for uid, name in all_known.items():
        users.append({
            "external_userid": uid,
            "nickname": name,
            "in_whitelist": uid in allowed_ids,
        })
    return {"tenant_id": tenant_id, "users": users}


# ── 用量统计 ──

@router.get("/api/usage/{tenant_id}")
async def api_usage(tenant_id: str, month: str = "", _token: str = Depends(_verify_token)):
    """单租户月度用量"""
    summary = get_usage_summary(tenant_id, month)
    daily = get_daily_breakdown(tenant_id, month)
    return {"tenant_id": tenant_id, "month": month or "current", "summary": summary, "daily": daily}


@router.get("/api/usage")
async def api_usage_all(month: str = "", _token: str = Depends(_verify_token)):
    """所有租户月度用量（跨容器）— 用 pipeline 批量查询"""
    from datetime import datetime, timezone

    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    tenants = _get_all_tenants()
    tids = [t.get("tenant_id", "") for t in tenants if t.get("tenant_id")]

    if not tids or not redis.available():
        return {"month": month, "tenants": {}}

    # 一次 pipeline 查所有租户的月度汇总（1 次 HTTP 代替 N 次）
    commands = [["HGETALL", f"meter:{tid}:{month}"] for tid in tids]
    responses = redis.pipeline(commands)

    result = {}
    for tid, data in zip(tids, responses):
        if data and isinstance(data, list) and len(data) >= 2:
            summary = {}
            for i in range(0, len(data) - 1, 2):
                try:
                    summary[data[i]] = int(data[i + 1])
                except (ValueError, TypeError):
                    summary[data[i]] = data[i + 1]
            if summary:
                result[tid] = summary

    return {"month": month, "tenants": result}


# ── 试用用户管理 ──

@router.get("/api/trial/{tenant_id}/users")
async def api_trial_users(tenant_id: str, _token: str = Depends(_verify_token)):
    """列出租户下所有试用用户"""
    users = list_trial_users(tenant_id)
    # 返回 trial_duration_hours 供前端计算剩余时间
    duration_hours = 48
    t = _resolve_tenant(tenant_id)
    if t:
        duration_hours = getattr(t, "trial_duration_hours", 48) or 48
    return {"tenant_id": tenant_id, "users": users, "total": len(users),
            "trial_duration_hours": duration_hours}


@router.get("/api/trial/{tenant_id}/user/{user_id}")
async def api_trial_user_detail(
    tenant_id: str, user_id: str, _token: str = Depends(_verify_token),
):
    """获取单个用户详情"""
    info = get_user_info(tenant_id, user_id)
    if not info:
        raise HTTPException(404, "User not found")
    return info


@router.post("/api/trial/{tenant_id}/user/{user_id}/approve")
async def api_approve_user(
    tenant_id: str, user_id: str, request: Request,
    _token: str = Depends(_verify_token),
):
    """审批用户（可选 duration_days 有效期）"""
    duration_days = 0
    try:
        body = await request.json()
        duration_days = int(body.get("duration_days", 0))
    except Exception:
        logger.warning("Failed to parse approve request body for %s/%s", tenant_id, user_id, exc_info=True)
    ok = approve_user(tenant_id, user_id, approved_by="admin_dashboard",
                      duration_days=duration_days)
    if not ok:
        raise HTTPException(500, "Approve failed (Redis unavailable?)")
    return {"status": "approved", "tenant_id": tenant_id, "user_id": user_id,
            "duration_days": duration_days}


@router.post("/api/trial/{tenant_id}/user/{user_id}/block")
async def api_block_user(
    tenant_id: str, user_id: str, _token: str = Depends(_verify_token),
):
    """封禁用户"""
    ok = block_user(tenant_id, user_id)
    if not ok:
        raise HTTPException(500, "Block failed")
    return {"status": "blocked", "tenant_id": tenant_id, "user_id": user_id}


@router.post("/api/trial/{tenant_id}/user/{user_id}/reset")
async def api_reset_user(
    tenant_id: str, user_id: str, _token: str = Depends(_verify_token),
):
    """重置用户为新试用"""
    ok = reset_user(tenant_id, user_id)
    if not ok:
        raise HTTPException(500, "Reset failed")
    return {"status": "reset", "tenant_id": tenant_id, "user_id": user_id}


@router.post("/api/trial/{tenant_id}/user/{user_id}/notes")
async def api_set_notes(
    tenant_id: str, user_id: str, request: Request,
    _token: str = Depends(_verify_token),
):
    """设置用户备注"""
    body = await request.json()
    notes = body.get("notes", "")
    ok = set_user_notes(tenant_id, user_id, notes)
    if not ok:
        raise HTTPException(500, "Set notes failed")
    return {"status": "ok"}


# ── 实例管理 ──

@router.get("/api/instances")
async def api_list_instances(_token: str = Depends(_verify_token)):
    """列出所有已供应的容器实例"""
    try:
        from app.services.provisioner import list_instances
        return {"instances": list_instances()}
    except Exception as e:
        logger.warning("list_instances failed: %s", e)
        return {"instances": [], "warning": str(e)}


@router.get("/api/instances/{tenant_id}")
async def api_instance_status(
    tenant_id: str, _token: str = Depends(_verify_token),
):
    """查看单个实例详细状态"""
    from app.services.provisioner import instance_status
    result = instance_status(tenant_id)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Not found"))
    return result


@router.get("/api/module-registry")
async def api_module_registry(_token: str = Depends(_verify_token)):
    """返回能力模组注册表（供 dashboard 模组选择器使用）"""
    registry_path = os.path.join(
        os.path.dirname(__file__), "..", "knowledge", "modules", "registry.json"
    )
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except FileNotFoundError:
        return {"modules": []}


@router.post("/api/instances")
async def api_provision_instance(
    request: Request, _token: str = Depends(_verify_token),
):
    """创建新的 bot 实例

    Body JSON:
    {
        "tenant_id": "customer-abc",
        "name": "客户ABC AI助手",
        "platform": "feishu" | "wecom" | "wecom_kf",

        // 平台凭证（按 platform 类型填写）
        // feishu:
        "app_id": "", "app_secret": "", "verification_token": "", "encrypt_key": "",
        // wecom:
        "wecom_corpid": "", "wecom_corpsecret": "", "wecom_agent_id": 0,
        "wecom_token": "", "wecom_encoding_aes_key": "",
        // wecom_kf:
        "wecom_corpid": "", "wecom_kf_secret": "", "wecom_kf_token": "",
        "wecom_kf_encoding_aes_key": "", "wecom_kf_open_kfid": "",

        // LLM 配置（可选，有默认值）
        "llm_system_prompt": "",
        "custom_persona": false,

        // 试用/配额（可选）
        "trial_enabled": false,
        "trial_duration_hours": 48,
        "approval_duration_days": 30,
        "quota_user_tokens_6h": 0
    }
    """
    from app.services.provisioner import provision

    body = await request.json()

    tenant_id = body.get("tenant_id", "").strip()
    name = body.get("name", "").strip()
    platform = body.get("platform", "").strip()

    if not tenant_id or not name or not platform:
        raise HTTPException(400, "Missing required fields: tenant_id, name, platform")

    # 按 platform 提取凭证
    cred_fields = {
        "feishu": ["app_id", "app_secret", "verification_token", "encrypt_key",
                    "oauth_redirect_uri"],
        "wecom": ["wecom_corpid", "wecom_corpsecret", "wecom_agent_id",
                   "wecom_token", "wecom_encoding_aes_key"],
        "wecom_kf": ["wecom_corpid", "wecom_kf_secret", "wecom_kf_token",
                      "wecom_kf_encoding_aes_key", "wecom_kf_open_kfid"],
        "qq": ["qq_app_id", "qq_app_secret", "qq_token"],
    }
    fields = cred_fields.get(platform, [])
    credentials = {f: body.get(f, "") for f in fields if body.get(f)}

    # 额外配置（传给 provision 后注入到 tenant_config）
    extra_config = {}
    for key in ("trial_enabled", "trial_duration_hours", "approval_duration_days",
                "quota_user_tokens_6h", "tools_enabled", "capability_modules",
                "rate_limit_rpm", "rate_limit_user_rpm",
                "quota_monthly_api_calls", "quota_monthly_tokens",
                "deploy_free_quota", "admin_names",
                "memory_diary_enabled", "memory_context_enabled",
                "memory_journal_max", "memory_chat_rounds", "memory_chat_ttl"):
        if key in body:
            extra_config[key] = body[key]

    # 合入 credentials 以便 provision 写入 tenants.json
    credentials.update(extra_config)

    result = provision(
        tenant_id=tenant_id,
        name=name,
        platform=platform,
        credentials=credentials,
        llm_system_prompt=body.get("llm_system_prompt", ""),
        custom_persona=body.get("custom_persona", False),
        tools_enabled=body.get("tools_enabled"),
        capability_modules=body.get("capability_modules"),
    )

    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Provision failed"))
    return result


@router.get("/api/logs")
async def api_self_logs(
    lines: int = 200,
    since: str = "",
    grep: str = "",
    level: str = "",
    _token: str = Depends(_verify_token),
):
    """获取当前容器自身的日志（优先从内存缓冲区读取，零 I/O 延迟）"""
    from datetime import datetime, timezone
    lines = min(max(lines, 10), 2000)

    log_lines: list[str] = []
    log_source = "memory"

    # ── 方案1（首选）：直接从内存环形缓冲区读取 ──
    # LOG_BUFFER 是 deque，存活在当前进程里，零文件 I/O，永远是最新的
    try:
        from app.main import LOG_BUFFER
        log_lines = list(LOG_BUFFER)[-lines:]
    except Exception:
        logger.warning("Failed to read LOG_BUFFER from app.main", exc_info=True)
        log_lines = []

    # ── 方案2（fallback）：读日志文件 ──
    if not log_lines:
        for candidate in (Path("/app/logs/bot.log"), Path("logs/bot.log")):
            if candidate.exists():
                try:
                    all_lines = candidate.read_text(errors="replace").strip().split("\n")
                    log_lines = all_lines[-lines:]
                    log_source = "file"
                except Exception:
                    logger.warning("Failed to read log file %s", candidate, exc_info=True)
                break

    if not log_lines:
        log_lines = ["(No logs available — container may have just started)"]
        log_source = "none"

    # 过滤
    if since and log_lines:
        cutoff = _parse_since_to_datetime(since)
        if cutoff:
            log_lines = [l for l in log_lines if _log_line_after(l, cutoff)]
    if level:
        level_upper = level.upper()
        log_lines = [l for l in log_lines if level_upper in l]
    if grep:
        grep_lower = grep.lower()
        log_lines = [l for l in log_lines if grep_lower in l.lower()]

    log_lines = _deduplicate_consecutive(log_lines)

    error_lines = [l for l in log_lines if "ERROR" in l or "CRITICAL" in l
                   or "Traceback" in l or "Exception" in l]

    return JSONResponse(
        content={
            "ok": True,
            "tenant_id": "_self",
            "container": "self",
            "log_source": log_source,
            "buffer_size": len(LOG_BUFFER) if "LOG_BUFFER" in dir() else -1,
            "total_lines": len(log_lines),
            "logs": "\n".join(log_lines),
            "error_count": len(error_lines),
            "recent_errors": "\n".join(error_lines[-20:]) if error_lines else "",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


def _parse_since_to_datetime(since: str):
    """将 '5m', '30m', '1h', '2h' 等转为 datetime cutoff。"""
    from datetime import datetime, timedelta, timezone
    import re
    m = re.match(r"^(\d+)(m|h|d)$", since.strip())
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=val), "h": timedelta(hours=val),
             "d": timedelta(days=val)}.get(unit)
    if not delta:
        return None
    return datetime.now(timezone.utc) - delta


def _log_line_after(line: str, cutoff) -> bool:
    """检查日志行的时间戳是否晚于 cutoff。解析失败时保留该行。"""
    from datetime import datetime, timezone
    # 格式: "2026-03-09 06:53:28,386 [INFO] ..."
    try:
        ts_str = line[:23]  # "2026-03-09 06:53:28,386"
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
        return ts >= cutoff
    except (ValueError, IndexError):
        return True  # 解析失败保留


def _deduplicate_consecutive(lines: list[str]) -> list[str]:
    """去除连续重复的日志行。"""
    if not lines:
        return lines
    result = [lines[0]]
    for line in lines[1:]:
        if line != result[-1]:
            result.append(line)
    return result


@router.get("/api/instances/{tenant_id}/logs")
async def api_instance_logs(
    tenant_id: str,
    lines: int = 200,
    since: str = "",
    grep: str = "",
    level: str = "",
    _token: str = Depends(_verify_token),
):
    """获取实例日志

    如果 tenant 在当前容器中运行，直接从内存 LOG_BUFFER 读取（实时）。
    否则通过 provisioner 走 HTTP 代理/文件/docker 三层获取。

    Query params:
        lines: 返回行数（默认200，最大2000）
        since: 时间过滤（如 "1h", "30m"）
        grep: 关键词过滤
        level: 日志级别过滤（ERROR, WARNING, INFO）
    """
    lines = min(max(lines, 10), 2000)

    # ── 单容器模式：如果没有独立 instance 记录，但 tenant 在当前容器中运行，直接读 LOG_BUFFER ──
    # 注意：tenant_sync 会把 dashboard/Redis 租户同步进每个容器的 tenant_registry；
    # 因此 “tenant_registry 有这个 tenant” 不能直接等价于“它运行在当前容器”。
    from app.services.provisioner import _registry as _instance_registry
    has_instance_record = _instance_registry.get(tenant_id) is not None
    from app.tenant.registry import tenant_registry
    if not has_instance_record and tenant_registry.get(tenant_id) is not None:
        return await api_self_logs(lines=lines, since=since, grep=grep,
                                   level=level, _token=_token)

    # ── 非本容器的 tenant：优先从 Redis 读取（_log_push_loop 每 30s 推送）──
    cached_log_response: JSONResponse | None = None
    try:
        import json as _json
        from app.services import redis_client as redis
        if redis.available():
            cached = redis.execute("GET", f"logs:{tenant_id}")
            if cached:
                data = _json.loads(cached)
                cached_lines = data.get("lines", [])
                if cached_lines:
                    log_lines = cached_lines[-lines:]
                    # 应用过滤
                    if since:
                        cutoff = _parse_since_to_datetime(since)
                        if cutoff:
                            log_lines = [l for l in log_lines if _log_line_after(l, cutoff)]
                    if level:
                        level_upper = level.upper()
                        log_lines = [l for l in log_lines if level_upper in l]
                    if grep:
                        grep_lower = grep.lower()
                        log_lines = [l for l in log_lines if grep_lower in l.lower()]
                    log_lines = _deduplicate_consecutive(log_lines)
                    error_lines = [l for l in log_lines if "ERROR" in l or "CRITICAL" in l
                                   or "Traceback" in l or "Exception" in l]
                    from datetime import datetime, timezone
                    cached_log_response = JSONResponse(
                        content={
                            "ok": True,
                            "tenant_id": tenant_id,
                            "container": "redis-cache",
                            "log_source": "redis",
                            "buffer_size": data.get("count", len(cached_lines)),
                            "total_lines": len(log_lines),
                            "logs": "\n".join(log_lines),
                            "error_count": len(error_lines),
                            "recent_errors": "\n".join(error_lines[-20:]) if error_lines else "",
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        },
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
                    )
                    cached_count = int(data.get("count", len(cached_lines)) or len(cached_lines))
                    if cached_count >= lines:
                        return cached_log_response
                    logger.info(
                        "Redis log cache for %s has only %d lines (< requested %d); trying full source",
                        tenant_id, cached_count, lines,
                    )
    except Exception:
        logger.warning("Redis log cache read failed for %s, falling back to provisioner", tenant_id, exc_info=True)

    # ── Fallback：走 provisioner 三层获取（HTTP 代理 → 文件 → docker logs）──
    from app.services.provisioner import get_instance_logs
    result = get_instance_logs(tenant_id, lines=lines, since=since,
                                grep=grep, level=level)
    if not result.get("ok"):
        if cached_log_response is not None:
            return cached_log_response
        # 不要 404，返回 200 + 错误信息让前端能显示
        error_msg = result.get("error", "Not found")
        return {
            "ok": False,
            "tenant_id": tenant_id,
            "total_lines": 0,
            "logs": f"(Log unavailable: {error_msg})\n\n"
                    "Possible causes:\n"
                    "- Instance not provisioned on this server\n"
                    "- Docker socket not accessible from this container\n"
                    "- Container not running",
            "error_count": 0,
            "recent_errors": "",
        }
    return JSONResponse(
        content=result,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.post("/api/instances/{tenant_id}/restart")
async def api_restart_instance(
    tenant_id: str, _token: str = Depends(_verify_token),
):
    """重启实例"""
    from app.services.provisioner import restart_instance
    result = restart_instance(tenant_id)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Restart failed"))
    return result


@router.post("/api/instances/{tenant_id}/stop")
async def api_stop_instance(
    tenant_id: str, _token: str = Depends(_verify_token),
):
    """停止实例"""
    from app.services.provisioner import stop_instance
    result = stop_instance(tenant_id)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Stop failed"))
    return result


@router.delete("/api/instances/{tenant_id}")
async def api_destroy_instance(
    tenant_id: str, _token: str = Depends(_verify_token),
):
    """销毁实例（不可逆！停止容器 + 删除所有文件）"""
    from app.services.provisioner import destroy_instance
    result = destroy_instance(tenant_id)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Destroy failed"))
    return result


# ── Co-tenant 管理（同容器多 KF 租户）──

@router.get("/api/instances/{instance_id}/co-tenants")
async def api_list_co_tenants(
    instance_id: str, _token: str = Depends(_verify_token),
):
    """列出实例下所有租户（含 primary + co-hosted）"""
    from app.services.provisioner import list_co_tenants
    return {"instance_id": instance_id, "tenants": list_co_tenants(instance_id)}


@router.post("/api/instances/{instance_id}/co-tenants")
async def api_add_co_tenant(
    instance_id: str, request: Request, _token: str = Depends(_verify_token),
):
    """添加 co-hosted KF 租户到已有实例

    默认从 primary tenant 继承全部配置（工具集、记忆、配额等），
    只覆盖必须不同的字段。body 中传入的字段会覆盖继承值。

    Body JSON:
    {
        "tenant_id": "kf-new-bot",           // 必填
        "name": "New KF Bot",                // 必填
        "wecom_kf_open_kfid": "wkXXXXXX",   // 必填
        "template": "inherit" | "minimal",   // 可选，默认 inherit
        "llm_system_prompt": "",             // 可选覆盖
        "custom_persona": false,             // 可选覆盖
        "trial_enabled": false,              // 可选覆盖
        "trial_duration_hours": 48,          // 可选覆盖
        "quota_user_tokens_6h": 0,           // 可选覆盖
        "tools_enabled": [],                 // 可选覆盖（空=继承 primary）
        // ... 其他任何 TenantConfig 字段均可覆盖
    }
    """
    from app.services.provisioner import _cohost_tenant, _registry, INSTANCES_DIR

    info = _registry.get(instance_id)
    if not info:
        raise HTTPException(404, f"Instance {instance_id} not found")
    if info.platform != "wecom_kf":
        raise HTTPException(400, "Co-hosting only supported for wecom_kf instances")

    body = await request.json()
    tenant_id = body.get("tenant_id", "").strip()
    name = body.get("name", "").strip()
    open_kfid = body.get("wecom_kf_open_kfid", "").strip()

    if not tenant_id or not name or not open_kfid:
        raise HTTPException(400, "Missing required fields: tenant_id, name, wecom_kf_open_kfid")

    # Read primary tenant's config to inherit from
    inst_tenants = INSTANCES_DIR / instance_id / "tenants.json"
    if not inst_tenants.exists():
        raise HTTPException(500, "Instance tenants.json not found")

    data = json.loads(inst_tenants.read_text())
    primary = data.get("tenants", [{}])[0]

    template = body.get("template", "inherit")

    if template == "minimal":
        # 最小配置：只有凭证 + 基础 LLM，tools_enabled 为空（全启用）
        new_config = {
            "tenant_id": tenant_id,
            "name": name,
            "platform": "wecom_kf",
            "wecom_corpid": primary.get("wecom_corpid", ""),
            "wecom_kf_secret": primary.get("wecom_kf_secret", ""),
            "wecom_kf_token": primary.get("wecom_kf_token", ""),
            "wecom_kf_encoding_aes_key": primary.get("wecom_kf_encoding_aes_key", ""),
            "wecom_kf_open_kfid": open_kfid,
            "llm_provider": primary.get("llm_provider", "gemini"),
            "llm_api_key": primary.get("llm_api_key", "${GEMINI_API_KEY}"),
            "llm_model": primary.get("llm_model", "gemini-3-flash-preview"),
            "llm_model_strong": primary.get("llm_model_strong", "gemini-3.1-pro-preview"),
            "coding_model": "",
        }
    else:
        # inherit 模板：从 primary 深拷贝全部配置
        new_config = dict(primary)
        # 覆盖必须不同的字段
        new_config["tenant_id"] = tenant_id
        new_config["name"] = name
        new_config["wecom_kf_open_kfid"] = open_kfid
        # co-tenant 默认关闭实例管理和自修复（安全考虑）
        new_config.setdefault("instance_management_enabled", False)
        new_config.setdefault("self_iteration_enabled", False)

    # body 中传入的任何字段都覆盖（支持精细控制）
    _OVERRIDE_FIELDS = (
        "llm_system_prompt", "custom_persona", "trial_enabled",
        "trial_duration_hours", "approval_duration_days",
        "quota_user_tokens_6h", "quota_monthly_api_calls",
        "quota_monthly_tokens", "rate_limit_rpm", "rate_limit_user_rpm",
        "tools_enabled", "capability_modules", "deploy_free_quota",
        "self_iteration_enabled", "instance_management_enabled",
        "memory_diary_enabled", "memory_journal_max",
        "memory_chat_rounds", "memory_chat_ttl", "memory_context_enabled",
        "social_media_api_provider", "social_media_api_key",
        "admin_names",
    )
    for field in _OVERRIDE_FIELDS:
        if field in body:
            new_config[field] = body[field]

    # 通过 Redis 队列通知目标容器 hot-load 新租户
    # （bridge 网络下容器间 127.0.0.1 不互通，不能用 HTTP）
    from app.services.tenant_sync import publish_tenant_update
    synced = publish_tenant_update("add", new_config)

    # 也注册到本地 registry（admin 容器自己可能也需要知道这个租户）
    try:
        tenant_registry.register_from_dict(new_config)
    except Exception:
        logger.warning("Failed to register co-tenant %s in local registry", tenant_id, exc_info=True)

    # 同步到根 tenants.json（CI/CD source of truth，如果可访问）
    try:
        _cohost_tenant(info, new_config)
    except Exception as e:
        logger.warning("_cohost_tenant fallback: %s (expected if INSTANCES_DIR missing)", e)

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "co_hosted_with": instance_id,
        "port": info.port,
        "hot_loaded": synced,
        "hot_load_note": "Synced via Redis — target container picks up within 5s" if synced else "Redis unavailable, restart needed",
    }


@router.delete("/api/instances/{instance_id}/co-tenants/{co_tenant_id}")
async def api_remove_co_tenant(
    instance_id: str, co_tenant_id: str, _token: str = Depends(_verify_token),
):
    """移除 co-hosted 租户"""
    from app.services.provisioner import remove_co_tenant, _registry
    from app.services.tenant_sync import publish_tenant_update

    # 先通过 provisioner 清理（kf_dispatch、root tenants.json 等）
    # 必须在 Redis 通知之前，否则失败时其他容器已错误移除
    result = remove_co_tenant(instance_id, co_tenant_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Remove failed"))

    # 标记为已删除（防止其他容器 publish_tenant_meta 重新发布）
    _mark_tenant_removed(co_tenant_id)

    # 成功后再通过 Redis 通知目标容器移除内存中的注册
    publish_tenant_update("remove", {"tenant_id": co_tenant_id})

    return result


@router.get("/api/instances/{instance_id}/kf-accounts")
async def api_list_kf_accounts(
    instance_id: str, _token: str = Depends(_verify_token),
):
    """通过企微 API 列出该实例 corp 下的所有客服账号（返回 open_kfid）

    需要该实例容器内的 wecom_kf_secret 来获取 access_token。
    支持 provisioned 实例（读 instances/{tid}/tenants.json）和
    auto-discovered 实例（读本地 tenant_registry）。
    """
    from app.services.provisioner import INSTANCES_DIR, _registry

    info = _registry.get(instance_id)
    if not info:
        raise HTTPException(404, f"Instance {instance_id} not found")

    # Try reading credentials from instance's tenants.json (provisioned)
    corpid = ""
    kf_secret = ""
    bound_kfids: dict[str, str] = {}  # open_kfid → tenant_id

    inst_tenants = INSTANCES_DIR / instance_id / "tenants.json"
    if inst_tenants.exists():
        data = json.loads(inst_tenants.read_text())
        primary = data.get("tenants", [{}])[0]
        corpid = primary.get("wecom_corpid", "")
        kf_secret = primary.get("wecom_kf_secret", "")
        for t in data.get("tenants", []):
            kfid = t.get("wecom_kf_open_kfid", "")
            if kfid:
                bound_kfids[kfid] = t.get("tenant_id", "")

    # Fallback: read from local tenant registry (auto-discovered instances)
    if not corpid or not kf_secret:
        t = tenant_registry.get(instance_id)
        if t:
            corpid = getattr(t, "wecom_corpid", "")
            kf_secret = getattr(t, "wecom_kf_secret", "")
            # Find all tenants with same corp (co-tenants)
            if corpid and kf_secret:
                for tid, tt in tenant_registry.all_tenants().items():
                    if (tt.platform == "wecom_kf"
                            and getattr(tt, 'wecom_corpid', '') == corpid
                            and getattr(tt, 'wecom_kf_secret', '') == kf_secret):
                        kfid = getattr(tt, 'wecom_kf_open_kfid', '')
                        if kfid:
                            bound_kfids[kfid] = tid

    if not corpid or not kf_secret:
        raise HTTPException(400, "Instance has no wecom_kf credentials")

    # Call WeChat API directly
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            token_resp = await client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": corpid, "corpsecret": kf_secret},
            )
            token_data = token_resp.json()
            if token_data.get("errcode", -1) != 0:
                return {"instance_id": instance_id, "accounts": [],
                        "error": f"WeChat token error (errcode={token_data.get('errcode')}): {token_data.get('errmsg', '')}",
                        "hint": "检查 corpid/kf_secret 是否正确，以及服务器 IP 是否在企微白名单中"}

            access_token = token_data["access_token"]

            acct_resp = await client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/kf/account/list",
                params={"access_token": access_token},
            )
            acct_data = acct_resp.json()
            if acct_data.get("errcode", -1) != 0:
                return {"instance_id": instance_id, "accounts": [],
                        "error": f"WeChat KF list error (errcode={acct_data.get('errcode')}): {acct_data.get('errmsg', '')}",
                        "hint": "客服账号列表获取失败，可能是 kf_secret 权限不足"}

        accounts = acct_data.get("account_list", [])

        # Mark which accounts are already bound to tenants
        for acct in accounts:
            kfid = acct.get("open_kfid", "")
            acct["bound_tenant"] = bound_kfids.get(kfid)

        return {"instance_id": instance_id, "accounts": accounts}
    except httpx.HTTPError as e:
        return {"instance_id": instance_id, "accounts": [],
                "error": f"WeChat API 请求失败: {e}",
                "hint": "网络错误，请检查服务器能否访问 qyapi.weixin.qq.com"}


# ── 超级管理员身份管理 ──

@router.get("/api/superadmin")
async def api_get_superadmin(_token: str = Depends(_verify_token)):
    """获取超管配置"""
    from app.services.super_admin import get_config
    return get_config()


@router.put("/api/superadmin")
async def api_set_superadmin(
    request: Request, _token: str = Depends(_verify_token),
):
    """更新超管配置（name + identities）"""
    from app.services.super_admin import set_config
    body = await request.json()
    name = body.get("name", "").strip()
    identities = body.get("identities", [])
    if not name:
        raise HTTPException(400, "Missing name")
    ok = set_config(name, identities)
    if not ok:
        raise HTTPException(500, "Failed to save (Redis unavailable?)")
    return {"ok": True}


@router.post("/api/superadmin/identity")
async def api_add_identity(
    request: Request, _token: str = Depends(_verify_token),
):
    """添加超管平台身份"""
    from app.services.super_admin import add_identity
    body = await request.json()
    ok = add_identity(
        platform=body.get("platform", ""),
        user_id=body.get("user_id", ""),
        tenant_id=body.get("tenant_id", ""),
        label=body.get("label", ""),
    )
    if not ok:
        raise HTTPException(400, "Missing user_id or save failed")
    return {"ok": True}


@router.delete("/api/superadmin/identity/{user_id:path}")
async def api_remove_identity(
    user_id: str, _token: str = Depends(_verify_token),
):
    """移除超管平台身份"""
    from app.services.super_admin import remove_identity
    ok = remove_identity(user_id)
    if not ok:
        raise HTTPException(404, "Identity not found")
    return {"ok": True}


# ── 开通审批管理 ──

@router.get("/api/provision-requests")
async def api_list_provision_requests(
    status: str = "all", _token: str = Depends(_verify_token),
):
    """列出开通请求"""
    from app.services.provision_approval import list_all, list_pending
    if status == "pending":
        return {"requests": list_pending()}
    return {"requests": list_all()}


@router.get("/api/provision-requests/{request_id}")
async def api_get_provision_request(
    request_id: str, _token: str = Depends(_verify_token),
):
    """获取单个请求详情"""
    from app.services.provision_approval import get_request
    req = get_request(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    return req


@router.post("/api/provision-requests/{request_id}/approve")
async def api_approve_provision_request(
    request_id: str, _token: str = Depends(_verify_token),
):
    """审批通过开通请求"""
    from app.services.provision_approval import approve_request
    result = approve_request(request_id, approved_by="dashboard_admin")
    if not result:
        raise HTTPException(404, "Request not found")
    return result


@router.post("/api/provision-requests/{request_id}/reject")
async def api_reject_provision_request(
    request_id: str, request: Request, _token: str = Depends(_verify_token),
):
    """拒绝开通请求"""
    from app.services.provision_approval import reject_request
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    reason = body.get("reason", "")
    result = reject_request(request_id, rejected_by="dashboard_admin", reason=reason)
    if not result:
        raise HTTPException(404, "Request not found")
    return result


# ── Deploy Quota Management ──────────────────────────────────────


@router.get("/api/deploy-quotas/{tenant_id}")
async def api_list_deploy_quotas(
    tenant_id: str, _token: str = Depends(_verify_token),
):
    """列出某租户下所有用户的部署配额"""
    from app.services.deploy_quota import list_all_quotas
    return list_all_quotas(tenant_id)


@router.get("/api/deploy-quotas/{tenant_id}/{user_id}")
async def api_get_deploy_quota(
    tenant_id: str, user_id: str, _token: str = Depends(_verify_token),
):
    """获取指定用户的部署配额详情"""
    from app.services.deploy_quota import get_user_quota
    info = get_user_quota(tenant_id, user_id)
    if not info:
        raise HTTPException(404, "No quota record found")
    return info


@router.post("/api/deploy-quotas/{tenant_id}/{user_id}/set")
async def api_set_deploy_quota(
    tenant_id: str, user_id: str, request: Request,
    _token: str = Depends(_verify_token),
):
    """管理员手动设置用户部署配额"""
    from app.services.deploy_quota import set_user_quota
    body = await request.json()
    total = body.get("total", 1)
    notes = body.get("notes", "")
    ok = set_user_quota(tenant_id, user_id, int(total), notes)
    if not ok:
        raise HTTPException(500, "Failed to set quota")
    return {"ok": True, "total": total}


@router.post("/api/deploy-quotas/{tenant_id}/{user_id}/reset")
async def api_reset_deploy_quota(
    tenant_id: str, user_id: str, _token: str = Depends(_verify_token),
):
    """重置用户部署配额（清零已使用次数）"""
    from app.services.deploy_quota import reset_user_quota
    ok = reset_user_quota(tenant_id, user_id)
    if not ok:
        raise HTTPException(500, "Failed to reset quota")
    return {"ok": True}


# ── 数据质量诊断报告 ──


@router.get("/api/diagnostics")
async def api_list_diagnostics(_token: str = Depends(_verify_token)):
    """列出待处理的数据质量诊断报告"""
    from app.services import redis_client as redis
    if not redis.available():
        return {"diagnostics": [], "error": "Redis unavailable"}

    pending = redis.execute("LRANGE", "diagnostic:pending", 0, 49)
    if not pending:
        return {"diagnostics": []}

    import json
    results = []
    for diag_id in pending:
        raw = redis.execute("GET", f"diagnostic:{diag_id}")
        if raw:
            try:
                diag = json.loads(raw)
                results.append({
                    "diag_id": diag_id,
                    "category": diag.get("category", ""),
                    "created_at": diag.get("created_at", 0),
                    "errors": diag.get("errors", []),
                })
            except (json.JSONDecodeError, TypeError):
                continue
    return {"diagnostics": results}


@router.get("/api/diagnostics/{diag_id}")
async def api_get_diagnostic(diag_id: str, _token: str = Depends(_verify_token)):
    """获取单条诊断报告详情"""
    from app.services import redis_client as redis
    if not redis.available():
        raise HTTPException(503, "Redis unavailable")

    raw = redis.execute("GET", f"diagnostic:{diag_id}")
    if not raw:
        raise HTTPException(404, "Diagnostic not found or expired")

    import json
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(500, "Invalid diagnostic data")


@router.post("/api/diagnostics/{diag_id}/fix")
async def api_trigger_fix(diag_id: str, _token: str = Depends(_verify_token)):
    """管理员审批：触发自动修复"""
    from app.services.auto_fix import admin_trigger_fix
    result = await admin_trigger_fix(diag_id)
    return {"ok": True, "diag_id": diag_id, "result": result}


@router.post("/api/diagnostics/{diag_id}/dismiss")
async def api_dismiss_diagnostic(diag_id: str, _token: str = Depends(_verify_token)):
    """管理员忽略此诊断报告"""
    from app.services import redis_client as redis
    if redis.available():
        redis.execute("DEL", f"diagnostic:{diag_id}")
        redis.execute("LREM", "diagnostic:pending", 0, diag_id)
    return {"ok": True, "diag_id": diag_id}


# ── 跨平台身份管理 API ──

@router.get("/api/identities/{tenant_id}")
async def api_list_identities(tenant_id: str, _token: str = Depends(_verify_token)):
    """列出指定租户下的所有跨平台身份"""
    from app.services.identity import list_identities
    identities = list_identities(bot_id=tenant_id, limit=100)
    return {"identities": identities, "count": len(identities)}


@router.get("/api/identities/{tenant_id}/{identity_id}")
async def api_get_identity(tenant_id: str, identity_id: str, _token: str = Depends(_verify_token)):
    """获取单个身份详情"""
    from app.services.identity import get_identity, get_linked_platforms
    info = get_identity(identity_id)
    if not info:
        raise HTTPException(404, "identity not found")
    info["linked_platforms"] = get_linked_platforms(identity_id, tenant_id)
    return info


@router.post("/api/identities/{tenant_id}/link")
async def api_link_identity(tenant_id: str, request: Request, _token: str = Depends(_verify_token)):
    """手动绑定跨平台身份（硬绑定）

    Body: {identity_id, platform, platform_user_id}
    如果 identity_id 为空，则创建新身份。
    """
    from app.services.identity import create_identity, link_identity, get_identity
    body = await request.json()
    identity_id = body.get("identity_id", "").strip()
    platform = body.get("platform", "").strip()
    pid = body.get("platform_user_id", "").strip()
    name = body.get("name", "").strip()

    if not platform or not pid:
        raise HTTPException(400, "platform and platform_user_id required")

    if identity_id:
        # 关联到已有身份
        ok = link_identity(identity_id, platform, pid, bot_id=tenant_id)
        if not ok:
            raise HTTPException(500, "link failed")
        return {"ok": True, "identity_id": identity_id, "action": "linked"}
    else:
        # 创建新身份
        if not name:
            raise HTTPException(400, "name required for new identity")
        new_id = create_identity(name=name, platform=platform, platform_user_id=pid, bot_id=tenant_id)
        if not new_id:
            raise HTTPException(500, "create failed")
        return {"ok": True, "identity_id": new_id, "action": "created"}


@router.delete("/api/identities/{tenant_id}/{identity_id}/{platform}")
async def api_unlink_identity(
    tenant_id: str, identity_id: str, platform: str,
    _token: str = Depends(_verify_token),
):
    """解除某平台的身份关联"""
    from app.services.identity import get_linked_platforms, unlink_identity
    linked = get_linked_platforms(identity_id, tenant_id)
    pid = linked.get(platform)
    if not pid:
        raise HTTPException(404, f"no {platform} link found for this identity")
    ok = unlink_identity(platform, pid, bot_id=tenant_id)
    if not ok:
        raise HTTPException(500, "unlink failed")
    return {"ok": True, "identity_id": identity_id, "platform": platform}


# ── 租户 Channel 管理 API ──

@router.get("/api/tenants/{tenant_id}/channels")
async def api_get_channels(tenant_id: str, _token: str = Depends(_verify_token)):
    """获取租户的所有 channel 配置（本地 + Redis 统一通过 _resolve_tenant_channels）"""
    t, channels, _is_local = _resolve_tenant_channels(tenant_id)
    if not t and not channels:
        raise HTTPException(404, "tenant not found")

    result = []
    for ch in channels:
        if not ch.enabled:
            continue
        result.append({
            "channel_id": ch.channel_id,
            "platform": ch.platform,
            "enabled": ch.enabled,
            "has_feishu": bool(ch.app_id),
            "has_wecom": bool(ch.wecom_corpid and ch.wecom_corpsecret),
            "has_wecom_kf": bool(ch.wecom_kf_open_kfid),
            "has_qq": bool(ch.qq_app_id),
        })
    return {"channels": result, "count": len(result)}


# ── Channel 凭证字段映射（按平台分组）──

_CHANNEL_CRED_FIELDS = {
    "feishu": ("app_id", "app_secret", "verification_token", "encrypt_key",
               "oauth_redirect_uri", "bot_open_id"),
    "wecom": ("wecom_corpid", "wecom_corpsecret", "wecom_agent_id",
              "wecom_token", "wecom_encoding_aes_key"),
    "wecom_kf": ("wecom_corpid", "wecom_kf_secret", "wecom_kf_token",
                 "wecom_kf_encoding_aes_key", "wecom_kf_open_kfid"),
    "qq": ("qq_app_id", "qq_app_secret", "qq_token"),
}

# 所有合法的 ChannelConfig 字段名
_CHANNEL_ALL_FIELDS = {f.name for f in ChannelConfig.__dataclass_fields__.values()}


def _resolve_tenant_channels(tenant_id: str):
    """获取租户和 channels（本地 registry → Redis tenant_cfg/admin:tenant fallback）。

    Returns (tenant_or_none, channels: list[ChannelConfig], is_local: bool)
    """
    t = tenant_registry.get(tenant_id)
    if t:
        # 本地租户：如果 t.channels 已有值（tenants.json 或之前 add 过），直接用
        if t.channels:
            return t, t.channels, True
        # t.channels 为空：尝试从 Redis 加载（dashboard 添加的 channels 持久化在这里）
        if redis.available():
            for key_prefix in (f"tenant_cfg:{tenant_id}", f"admin:tenant:{tenant_id}"):
                raw = redis.execute("GET", key_prefix)
                if not raw:
                    continue
                try:
                    cfg = json.loads(raw)
                    ch_dicts = cfg.get("channels", [])
                    if ch_dicts:
                        for ch_d in ch_dicts:
                            ch_kwargs = {k: v for k, v in ch_d.items() if k in _CHANNEL_ALL_FIELDS}
                            t.channels.append(ChannelConfig(**ch_kwargs))
                        logger.info("Loaded %d channels from Redis for local tenant %s",
                                    len(t.channels), tenant_id)
                        return t, t.channels, True
                except Exception as e:
                    logger.warning("Failed to load Redis channels for %s: %s", tenant_id, e)
        # Fallback：从顶层凭证构造 primary channel
        primary = t._build_primary_channel()
        t.channels.append(primary)
        return t, t.channels, True

    # 非本地：从 Redis tenant_cfg → admin:tenant 加载
    if redis.available():
        for key_prefix in (f"tenant_cfg:{tenant_id}", f"admin:tenant:{tenant_id}"):
            raw = redis.execute("GET", key_prefix)
            if not raw:
                continue
            try:
                cfg = json.loads(raw)
                ch_list = []
                for ch_d in cfg.get("channels", []):
                    ch_kwargs = {k: v for k, v in ch_d.items() if k in _CHANNEL_ALL_FIELDS}
                    ch_list.append(ChannelConfig(**ch_kwargs))
                # 如果 Redis 里没有 channels，从顶层字段构造 primary
                if not ch_list:
                    platform = cfg.get("platform", "")
                    if platform:
                        ch_list.append(ChannelConfig(
                            channel_id=f"{tenant_id}-{platform}",
                            platform=platform,
                            enabled=True,
                        ))
                return None, ch_list, False
            except (json.JSONDecodeError, TypeError, Exception) as e:
                logger.warning("Failed to load channels from Redis for %s via %s: %s",
                               tenant_id, key_prefix, e)

    return None, [], False


def _channels_to_dicts(channels: list) -> list[dict]:
    """将 ChannelConfig 列表序列化为 dict 列表（用于持久化）。"""
    from dataclasses import asdict
    result = []
    for ch in channels:
        d = asdict(ch) if hasattr(ch, "__dataclass_fields__") else dict(ch)
        # 过滤掉空默认值，减小 JSON 体积
        result.append({k: v for k, v in d.items() if v or k in ("channel_id", "platform", "enabled")})
    return result


def _persist_channels(tenant_id: str, channels: list):
    """将 channel 变更持久化到 tenants.json + Redis + 通知其他容器。"""
    ch_dicts = _channels_to_dicts(channels)

    # 1) 更新本地 tenants.json
    for candidate in ("/app/tenants.json", "tenants.json"):
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            for t in data.get("tenants", []):
                if t.get("tenant_id") == tenant_id:
                    t["channels"] = ch_dicts
                    break
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            logger.info("Persisted channels for %s to %s", tenant_id, path)
            break
        except Exception as e:
            logger.warning("Failed to persist channels to %s: %s", path, e)

    # 2) 持久化到 Redis（必须保证写入，不能静默跳过）
    if redis.available():
        written = False
        cfg_key = f"tenant_cfg:{tenant_id}"
        raw = redis.execute("GET", cfg_key)
        if raw:
            try:
                cfg = json.loads(raw)
                cfg["channels"] = ch_dicts
                redis.execute("SET", cfg_key, json.dumps(cfg, ensure_ascii=False))
                logger.info("Persisted channels for %s to Redis tenant_cfg", tenant_id)
                written = True
            except Exception:
                logger.warning("Failed to persist channels to Redis tenant_cfg for %s", tenant_id, exc_info=True)
        if not written:
            # tenant_cfg 不存在：更新或创建 admin:tenant 元数据
            meta_key = f"admin:tenant:{tenant_id}"
            meta_raw = redis.execute("GET", meta_key)
            try:
                meta = json.loads(meta_raw) if meta_raw else {"tenant_id": tenant_id}
                meta["channels"] = ch_dicts
                redis.execute("SET", meta_key, json.dumps(meta, ensure_ascii=False))
                redis.execute("EXPIRE", meta_key, 86400)
                logger.info("Persisted channels for %s to Redis admin:tenant (created=%s)",
                            tenant_id, not meta_raw)
            except Exception as e:
                logger.warning("Failed to persist channels to Redis for %s: %s", tenant_id, e)

    # 3) 通知其他容器
    from app.services.tenant_sync import publish_tenant_update
    publish_tenant_update("update", {"tenant_id": tenant_id, "channels": ch_dicts})

    # 4) 更新 Redis meta
    try:
        publish_tenant_meta()
    except Exception:
        logger.warning("publish_tenant_meta failed after channel persist for %s", tenant_id, exc_info=True)


@router.post("/api/tenants/{tenant_id}/channels")
async def api_add_channel(
    tenant_id: str, request: Request, _token: str = Depends(_verify_token),
):
    """添加新 channel 到租户"""
    t, channels, is_local = _resolve_tenant_channels(tenant_id)
    if not t and not channels:
        raise HTTPException(404, "tenant not found (local or Redis)")

    body = await request.json()
    platform = body.get("platform", "").strip()
    if platform not in ("feishu", "wecom", "wecom_kf", "qq"):
        raise HTTPException(400, "invalid platform")

    # channel_id: 用户指定或自动生成
    channel_id = body.get("channel_id", "").strip()
    if not channel_id:
        channel_id = f"{tenant_id}-{platform}"
    # 避免 ID 冲突
    existing_ids = {ch.channel_id for ch in channels}
    base_id = channel_id
    suffix = 2
    while channel_id in existing_ids:
        channel_id = f"{base_id}-{suffix}"
        suffix += 1

    # 构造 ChannelConfig
    ch_kwargs = {"channel_id": channel_id, "platform": platform, "enabled": body.get("enabled", True)}
    for field_name in _CHANNEL_ALL_FIELDS:
        if field_name in ("channel_id", "platform", "enabled"):
            continue
        if field_name in body and body[field_name]:
            ch_kwargs[field_name] = body[field_name]

    new_ch = ChannelConfig(**ch_kwargs)
    channels.append(new_ch)

    if is_local:
        t.channels = channels
    _persist_channels(tenant_id, channels)

    return {"ok": True, "channel_id": channel_id, "action": "added"}


@router.put("/api/tenants/{tenant_id}/channels/{channel_id}")
async def api_update_channel(
    tenant_id: str, channel_id: str, request: Request,
    _token: str = Depends(_verify_token),
):
    """更新 channel 配置"""
    t, channels, is_local = _resolve_tenant_channels(tenant_id)
    if not t and not channels:
        raise HTTPException(404, "tenant not found (local or Redis)")

    body = await request.json()

    # 找到目标 channel
    target = None
    for ch in channels:
        if ch.channel_id == channel_id:
            target = ch
            break
    if not target:
        raise HTTPException(404, f"channel {channel_id} not found")

    # 更新字段
    updated = []
    for field_name in _CHANNEL_ALL_FIELDS:
        if field_name in ("channel_id",):
            continue  # ID 不可改
        if field_name in body:
            setattr(target, field_name, body[field_name])
            updated.append(field_name)

    if is_local:
        t.channels = channels
    _persist_channels(tenant_id, channels)

    return {"ok": True, "channel_id": channel_id, "updated_fields": updated}


@router.delete("/api/tenants/{tenant_id}/channels/{channel_id}")
async def api_delete_channel(
    tenant_id: str, channel_id: str,
    _token: str = Depends(_verify_token),
):
    """删除 channel"""
    t, channels, is_local = _resolve_tenant_channels(tenant_id)
    if not t and not channels:
        raise HTTPException(404, "tenant not found (local or Redis)")

    before = len(channels)
    channels = [ch for ch in channels if ch.channel_id != channel_id]
    if len(channels) == before:
        raise HTTPException(404, f"channel {channel_id} not found")

    if is_local:
        t.channels = channels
    _persist_channels(tenant_id, channels)

    return {"ok": True, "channel_id": channel_id, "action": "deleted"}


# ── Per-Tool 可观测性 API（GTC 借鉴）──

@router.get("/api/tools/{tenant_id}/stats")
async def api_tool_stats(tenant_id: str, _token: str = Depends(_verify_token)):
    """获取租户下所有工具的性能统计汇总（调用次数、成功率、平均延迟等）"""
    from app.services.tool_tracker import get_all_tool_stats_summary
    summaries = get_all_tool_stats_summary(tenant_id)
    return {
        "tenant_id": tenant_id,
        "tool_count": len(summaries),
        "tools": summaries,
    }


@router.get("/api/tools/{tenant_id}/combos")
async def api_tool_combos(tenant_id: str, _token: str = Depends(_verify_token)):
    """获取租户下的高频工具调用组合"""
    from app.services.tool_tracker import get_frequent_combos
    combos = get_frequent_combos(tenant_id, min_freq=2)
    return {
        "tenant_id": tenant_id,
        "combos": [{"sequence": c, "count": n} for c, n in combos],
    }


@router.get("/api/tools/{tenant_id}/lessons")
async def api_tool_lessons(tenant_id: str, _token: str = Depends(_verify_token)):
    """获取租户下的工具使用经验教训"""
    from app.services.tool_tracker import get_recent_lessons
    lessons = get_recent_lessons(tenant_id, limit=50)
    return {
        "tenant_id": tenant_id,
        "lessons": lessons,
    }
