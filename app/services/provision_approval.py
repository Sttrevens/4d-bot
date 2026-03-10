"""开通审批队列

客户请求开通 bot → 创建 pending request → 超管审批 → 自动开通。

流程：
1. 客户和 steven-ai 聊天，确认需求
2. steven-ai 调 create_request() → Redis 存 pending 请求
3. 超管下次和任意 bot 聊天时，system prompt 注入待审批提醒
4. 超管说"批了" → bot 调 approve_request() → 自动 provision
5. 超管说"拒了" → bot 调 reject_request() → 标记拒绝

Redis 数据结构：
    provision_req:{request_id} → JSON {
        request_id, status, requester_id, requester_name,
        tenant_id, name, platform, credentials (加密/脱敏),
        llm_system_prompt, custom_persona, capability_modules,
        created_at, reviewed_at, reviewed_by, reject_reason,
        provision_result
    }

    provision_req_index → ZSET (score=created_at, member=request_id)

fail-open：审批系统 Redis 不可用时，不自动放行开通（fail-closed）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from app.services import redis_client as redis

logger = logging.getLogger(__name__)

_PREFIX = "provision_req:"
_INDEX_KEY = "provision_req_index"
_TTL = 30 * 86400  # 30 天保留


def _fire_and_forget_notify(request_data: dict):
    """创建请求后异步通知超管（不阻塞 create_request 返回）。"""
    async def _do_notify():
        try:
            from app.services.super_admin import notify_super_admin
            msg = (
                f"📋 新的 Bot 开通申请\n"
                f"请求人: {request_data.get('requester_name', '未知')}\n"
                f"Bot 名称: {request_data.get('name', '')}\n"
                f"平台: {request_data.get('platform', '')}\n"
                f"请求 ID: {request_data.get('request_id', '')}\n"
                f"—— 回复任意 bot「审批」或到 Admin Dashboard 处理"
            )
            await notify_super_admin(msg)
        except Exception:
            logger.debug("provision notify failed", exc_info=True)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_notify())
    except RuntimeError:
        # 没有 event loop（同步调用场景），跳过通知
        logger.debug("no event loop for provision notification")


def create_request(
    requester_id: str,
    requester_name: str,
    tenant_id: str,
    name: str,
    platform: str,
    credentials: dict,
    llm_system_prompt: str = "",
    custom_persona: bool = False,
    capability_modules: list[str] | None = None,
    notes: str = "",
    source_tenant_id: str = "",
) -> dict | None:
    """创建开通请求（pending 状态）。

    Returns:
        请求详情 dict，或 None（Redis 不可用）
    """
    if not redis.available():
        return None

    request_id = f"req_{uuid.uuid4().hex[:12]}"
    now = time.time()

    # 脱敏凭证（只保留 key 名，不存完整值）
    cred_keys = list(credentials.keys()) if credentials else []
    # 完整凭证单独存（有 TTL，审批后立即用掉）
    cred_key = f"{_PREFIX}{request_id}:creds"

    # 快照路由关键字段到请求数据（凭证有 7 天 TTL 会过期，
    # 但 approve 时仍需 wecom_kf_open_kfid 等字段来创建 tenant_cfg）
    _ROUTING_SNAPSHOT_FIELDS = (
        "wecom_kf_open_kfid", "wecom_corpid", "wecom_kf_secret",
        "app_id", "app_secret",
    )

    data = {
        "request_id": request_id,
        "status": "pending",
        "requester_id": requester_id,
        "requester_name": requester_name,
        "tenant_id": tenant_id,
        "name": name,
        "platform": platform,
        "credential_fields": cred_keys,
        "llm_system_prompt": llm_system_prompt,
        "custom_persona": custom_persona,
        "capability_modules": capability_modules or [],
        "notes": notes,
        "source_tenant_id": source_tenant_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_at": "",
        "reviewed_by": "",
        "reject_reason": "",
        "provision_result": None,
    }
    # 路由关键字段快照（即使凭证 TTL 过期也能恢复）
    for sf in _ROUTING_SNAPSHOT_FIELDS:
        if credentials and credentials.get(sf):
            data[sf] = credentials[sf]

    try:
        redis.pipeline([
            ["SET", f"{_PREFIX}{request_id}", json.dumps(data, ensure_ascii=False)],
            ["EXPIRE", f"{_PREFIX}{request_id}", str(_TTL)],
            # 凭证单独存，审批后用完即删
            ["SET", cred_key, json.dumps(credentials, ensure_ascii=False)],
            ["EXPIRE", cred_key, str(7 * 86400)],  # 7 天过期
            # 索引
            ["ZADD", _INDEX_KEY, str(now), request_id],
        ])
        logger.info("provision_approval: created %s by %s for %s",
                     request_id, requester_name, tenant_id)
        # 异步通知超管
        _fire_and_forget_notify(data)
        return data
    except Exception:
        logger.warning("provision_approval: create failed", exc_info=True)
        return None


def get_request(request_id: str) -> dict | None:
    """获取单个请求详情。"""
    if not redis.available():
        return None
    try:
        raw = redis.execute("GET", f"{_PREFIX}{request_id}")
        if raw and isinstance(raw, str):
            return json.loads(raw)
    except Exception:
        pass
    return None


def list_pending() -> list[dict]:
    """列出所有 pending 状态的请求（按时间排序）。"""
    if not redis.available():
        return []
    try:
        # 读取最近 50 个请求
        members = redis.execute("ZREVRANGE", _INDEX_KEY, "0", "49")
        if not members or not isinstance(members, list):
            return []
        pending = []
        for rid in members:
            if not isinstance(rid, str):
                continue
            req = get_request(rid)
            if req and req.get("status") == "pending":
                pending.append(req)
        return pending
    except Exception:
        logger.debug("provision_approval: list_pending failed", exc_info=True)
        return []


def list_all(limit: int = 50) -> list[dict]:
    """列出所有请求（供 dashboard 展示）。"""
    if not redis.available():
        return []
    try:
        members = redis.execute("ZREVRANGE", _INDEX_KEY, "0", str(limit - 1))
        if not members or not isinstance(members, list):
            return []
        results = []
        for rid in members:
            if not isinstance(rid, str):
                continue
            req = get_request(rid)
            if req:
                results.append(req)
        return results
    except Exception:
        return []


def approve_request(request_id: str, approved_by: str = "admin") -> dict | None:
    """审批通过 → 自动尝试 provision。

    Returns:
        更新后的请求 dict（含 provision_result），或 None。
    """
    req = get_request(request_id)
    if not req:
        return None
    if req.get("status") != "pending":
        return req  # 已处理过

    # 获取完整凭证
    cred_key = f"{_PREFIX}{request_id}:creds"
    cred_raw = redis.execute("GET", cred_key)
    credentials = {}
    if cred_raw and isinstance(cred_raw, str):
        try:
            credentials = json.loads(cred_raw)
        except Exception:
            pass

    # 标记已审批
    req["status"] = "approved"
    req["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    req["reviewed_by"] = approved_by

    # 尝试自动 provision
    provision_result = None
    if credentials:
        try:
            from app.services.provisioner import provision
            kwargs = {}
            if req.get("llm_system_prompt"):
                kwargs["llm_system_prompt"] = req["llm_system_prompt"]
            if req.get("custom_persona"):
                kwargs["custom_persona"] = True
            if req.get("capability_modules"):
                kwargs["capability_modules"] = req["capability_modules"]

            provision_result = provision(
                tenant_id=req["tenant_id"],
                name=req["name"],
                platform=req["platform"],
                credentials=credentials,
                **kwargs,
            )
            req["provision_result"] = provision_result

            # provision 成功后绑定客户 + 消耗部署配额
            if provision_result and provision_result.get("ok"):
                try:
                    from app.services.customer_store import bind_customer
                    bind_customer(
                        external_userid=req["requester_id"],
                        tenant_id=req["tenant_id"],
                        name=req["requester_name"],
                        platform=req["platform"],
                        port=provision_result.get("port", 0),
                    )
                except Exception:
                    logger.debug("auto bind_customer failed", exc_info=True)

                # 消耗部署配额（仅成功才扣）
                try:
                    from app.services.deploy_quota import consume_deploy_quota
                    source_tenant = req.get("source_tenant_id", "")
                    if source_tenant:
                        from app.tenant.registry import tenant_registry
                        src_cfg = tenant_registry.get(source_tenant)
                        free_q = src_cfg.deploy_free_quota if src_cfg else 1
                        consume_deploy_quota(
                            source_tenant, req["requester_id"],
                            req["tenant_id"], free_q,
                        )
                except Exception:
                    logger.debug("deploy_quota consume failed", exc_info=True)

        except Exception as e:
            logger.warning("provision_approval: auto-provision failed: %s", e)
            req["provision_result"] = {"ok": False, "error": str(e)}
    else:
        req["provision_result"] = {"ok": False, "error": "凭证已过期，请客户重新提供"}

    # ── 始终发布到 Redis tenant_cfg（无论 provision 是否成功、凭证是否过期）──
    # 容器内没有 Docker 时 provision() 会失败，但 co-host 容器可以通过
    # tenant_sync 从 Redis 热加载新租户配置（_try_hot_load_tenant）。
    # BUG FIX: 之前在 `if credentials:` 里，凭证 7 天 TTL 过期后 tenant_cfg
    # 永远不会创建，导致 co-host 租户在容器重启后消失。
    try:
        from app.services.tenant_sync import publish_tenant_update
        tenant_config = {
            "tenant_id": req["tenant_id"],
            "name": req["name"],
            "platform": req["platform"],
            "coding_model": "",
        }
        # 凭证可用时合入（wecom_corpid, wecom_kf_open_kfid 等关键路由字段）
        if credentials:
            tenant_config.update(credentials)
        # 凭证过期时，从请求快照中恢复路由关键字段
        else:
            for snap_field in ("wecom_kf_open_kfid", "wecom_corpid",
                               "wecom_kf_secret", "app_id", "app_secret"):
                if req.get(snap_field):
                    tenant_config[snap_field] = req[snap_field]
        if req.get("llm_system_prompt"):
            tenant_config["llm_system_prompt"] = req["llm_system_prompt"]
        if req.get("custom_persona"):
            tenant_config["custom_persona"] = True
        if req.get("capability_modules"):
            tenant_config["capability_modules"] = req["capability_modules"]
        # 继承 LLM 配置默认值
        tenant_config.setdefault("llm_provider", "gemini")
        tenant_config.setdefault("llm_api_key", "${GEMINI_API_KEY}")
        tenant_config.setdefault("llm_model", "gemini-3-flash-preview")
        tenant_config.setdefault("llm_model_strong", "gemini-3.1-pro-preview")
        publish_tenant_update("add", tenant_config)
        logger.info("provision_approval: published tenant_cfg:%s to Redis",
                    req["tenant_id"])
    except Exception as e:
        logger.warning("provision_approval: tenant_sync publish failed: %s", e)

    # 保存更新 + 删除凭证
    try:
        redis.pipeline([
            ["SET", f"{_PREFIX}{request_id}", json.dumps(req, ensure_ascii=False)],
            ["EXPIRE", f"{_PREFIX}{request_id}", str(_TTL)],
            ["DEL", cred_key],
        ])
    except Exception:
        pass

    logger.info("provision_approval: approved %s by %s, result=%s",
                request_id, approved_by, "ok" if (provision_result or {}).get("ok") else "failed")
    return req


def reject_request(request_id: str, rejected_by: str = "admin",
                   reason: str = "") -> dict | None:
    """拒绝请求。"""
    req = get_request(request_id)
    if not req:
        return None
    if req.get("status") != "pending":
        return req

    req["status"] = "rejected"
    req["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    req["reviewed_by"] = rejected_by
    req["reject_reason"] = reason

    try:
        redis.pipeline([
            ["SET", f"{_PREFIX}{request_id}", json.dumps(req, ensure_ascii=False)],
            ["EXPIRE", f"{_PREFIX}{request_id}", str(_TTL)],
            # 删除凭证
            ["DEL", f"{_PREFIX}{request_id}:creds"],
        ])
    except Exception:
        pass

    logger.info("provision_approval: rejected %s by %s", request_id, rejected_by)
    return req
