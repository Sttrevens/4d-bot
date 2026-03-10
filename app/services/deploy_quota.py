"""Per-user 部署配额系统

每个用户有 N 次免费部署 bot 的机会（默认 1 次）。
配额仅在部署成功后消耗，申请被拒绝不扣额度。
用完后引导用户付费。管理员可手动调整配额。

Redis 数据结构：
    deploy_quota:{tenant_id}:{user_id} → HASH {
        total:          总配额（默认 1）
        used:           已使用次数
        first_request:  首次请求时间 (ISO)
        last_deploy:    最后一次成功部署时间 (ISO)
        deploys:        JSON list of deployed tenant_ids
        notes:          管理员备注
    }

fail-open 策略：Redis 不可用时放行（不阻塞开通流程）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from app.services import redis_client as redis

logger = logging.getLogger(__name__)

_PREFIX = "deploy_quota:"
_TTL = 365 * 86400  # 1 year


def _key(tenant_id: str, user_id: str) -> str:
    return f"{_PREFIX}{tenant_id}:{user_id}"


def check_deploy_quota(tenant_id: str, user_id: str, free_deploys: int = 1) -> dict:
    """检查用户是否还有免费部署额度。

    Args:
        tenant_id: 当前 bot 所在租户（如 kf-steven-ai）
        user_id: 请求部署的用户 ID
        free_deploys: 每用户免费部署次数（从 tenant config 读取）

    Returns:
        {
            "allowed": bool,
            "remaining": int,     # 剩余免费额度
            "used": int,          # 已使用
            "total": int,         # 总额度
            "deploys": list,      # 已部署的 tenant_ids
        }
    """
    if free_deploys <= 0:
        # 0 = 无限制
        return {"allowed": True, "remaining": -1, "used": 0, "total": 0, "deploys": []}

    if not redis.available():
        # fail-open
        return {"allowed": True, "remaining": free_deploys, "used": 0, "total": free_deploys, "deploys": []}

    try:
        key = _key(tenant_id, user_id)
        raw = redis.execute("HGETALL", key)
        if not raw or not isinstance(raw, list):
            # 新用户，还没有记录
            return {"allowed": True, "remaining": free_deploys, "used": 0, "total": free_deploys, "deploys": []}

        # HGETALL returns [field1, val1, field2, val2, ...]
        data = {}
        for i in range(0, len(raw), 2):
            if i + 1 < len(raw):
                data[str(raw[i])] = str(raw[i + 1])

        total = int(data.get("total", str(free_deploys)))
        used = int(data.get("used", "0"))
        deploys_raw = data.get("deploys", "[]")
        try:
            deploys = json.loads(deploys_raw)
        except Exception:
            deploys = []

        remaining = max(0, total - used)
        return {
            "allowed": remaining > 0,
            "remaining": remaining,
            "used": used,
            "total": total,
            "deploys": deploys,
        }
    except Exception:
        logger.debug("deploy_quota: check failed, fail-open", exc_info=True)
        return {"allowed": True, "remaining": free_deploys, "used": 0, "total": free_deploys, "deploys": []}


def consume_deploy_quota(
    tenant_id: str, user_id: str, deployed_tenant_id: str, free_deploys: int = 1
) -> bool:
    """部署成功后消耗一次配额。

    Args:
        tenant_id: 当前 bot 所在租户
        user_id: 用户 ID
        deployed_tenant_id: 新部署的实例 tenant_id
        free_deploys: 默认免费次数

    Returns:
        True if consumed successfully
    """
    if free_deploys <= 0:
        return True  # 无限制模式不需要消耗

    if not redis.available():
        return False

    try:
        key = _key(tenant_id, user_id)
        now_iso = datetime.now(timezone.utc).isoformat()

        # 读取现有数据
        raw = redis.execute("HGETALL", key)
        data = {}
        if raw and isinstance(raw, list):
            for i in range(0, len(raw), 2):
                if i + 1 < len(raw):
                    data[str(raw[i])] = str(raw[i + 1])

        total = int(data.get("total", str(free_deploys)))
        used = int(data.get("used", "0"))
        deploys_raw = data.get("deploys", "[]")
        try:
            deploys = json.loads(deploys_raw)
        except Exception:
            deploys = []

        # 更新
        used += 1
        if deployed_tenant_id not in deploys:
            deploys.append(deployed_tenant_id)

        redis.pipeline([
            ["HSET", key, "total", str(total)],
            ["HSET", key, "used", str(used)],
            ["HSET", key, "deploys", json.dumps(deploys)],
            ["HSET", key, "last_deploy", now_iso],
            ["HSETNX", key, "first_request", now_iso],
            ["EXPIRE", key, str(_TTL)],
        ])

        logger.info(
            "deploy_quota: consumed %s/%s for user %s (deployed %s)",
            used, total, user_id[:16], deployed_tenant_id,
        )
        return True
    except Exception:
        logger.warning("deploy_quota: consume failed", exc_info=True)
        return False


def init_user_quota(tenant_id: str, user_id: str, free_deploys: int = 1) -> bool:
    """初始化用户配额记录（首次请求部署时调用）。

    如果已有记录则不覆盖。
    """
    if not redis.available():
        return False

    try:
        key = _key(tenant_id, user_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        # HSETNX: 只在字段不存在时设置
        redis.pipeline([
            ["HSETNX", key, "total", str(free_deploys)],
            ["HSETNX", key, "used", "0"],
            ["HSETNX", key, "deploys", "[]"],
            ["HSETNX", key, "first_request", now_iso],
            ["HSETNX", key, "notes", ""],
            ["EXPIRE", key, str(_TTL)],
        ])
        return True
    except Exception:
        logger.debug("deploy_quota: init failed", exc_info=True)
        return False


def set_user_quota(tenant_id: str, user_id: str, total: int, notes: str = "") -> bool:
    """管理员手动设置用户总配额。"""
    if not redis.available():
        return False

    try:
        key = _key(tenant_id, user_id)
        cmds = [
            ["HSET", key, "total", str(total)],
            ["EXPIRE", key, str(_TTL)],
        ]
        if notes:
            cmds.insert(1, ["HSET", key, "notes", notes])
        redis.pipeline(cmds)
        logger.info("deploy_quota: set total=%d for %s:%s", total, tenant_id, user_id[:16])
        return True
    except Exception:
        logger.warning("deploy_quota: set failed", exc_info=True)
        return False


def reset_user_quota(tenant_id: str, user_id: str) -> bool:
    """重置用户配额（清零已使用次数）。"""
    if not redis.available():
        return False

    try:
        key = _key(tenant_id, user_id)
        redis.pipeline([
            ["HSET", key, "used", "0"],
            ["HSET", key, "deploys", "[]"],
            ["HDEL", key, "last_deploy"],
            ["EXPIRE", key, str(_TTL)],
        ])
        logger.info("deploy_quota: reset for %s:%s", tenant_id, user_id[:16])
        return True
    except Exception:
        return False


def get_user_quota(tenant_id: str, user_id: str) -> dict | None:
    """获取用户配额详情（管理员查看用）。"""
    if not redis.available():
        return None

    try:
        key = _key(tenant_id, user_id)
        raw = redis.execute("HGETALL", key)
        if not raw or not isinstance(raw, list):
            return None

        data = {}
        for i in range(0, len(raw), 2):
            if i + 1 < len(raw):
                data[str(raw[i])] = str(raw[i + 1])

        if not data:
            return None

        deploys_raw = data.get("deploys", "[]")
        try:
            deploys = json.loads(deploys_raw)
        except Exception:
            deploys = []

        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "total": int(data.get("total", "1")),
            "used": int(data.get("used", "0")),
            "remaining": max(0, int(data.get("total", "1")) - int(data.get("used", "0"))),
            "deploys": deploys,
            "first_request": data.get("first_request", ""),
            "last_deploy": data.get("last_deploy", ""),
            "notes": data.get("notes", ""),
        }
    except Exception:
        return None


def list_all_quotas(tenant_id: str) -> list[dict]:
    """列出某租户下所有用户的部署配额（SCAN 遍历）。"""
    if not redis.available():
        return []

    results = []
    cursor = "0"
    prefix = f"{_PREFIX}{tenant_id}:"
    try:
        for _ in range(100):
            resp = redis.execute("SCAN", cursor, "MATCH", f"{prefix}*", "COUNT", "50")
            if not resp or not isinstance(resp, list) or len(resp) < 2:
                break
            cursor = str(resp[0])
            keys = resp[1] if isinstance(resp[1], list) else []
            for key in keys:
                if not isinstance(key, str):
                    continue
                user_id = key[len(prefix):]
                info = get_user_quota(tenant_id, user_id)
                if info:
                    results.append(info)
            if cursor == "0":
                break
        results.sort(key=lambda x: x.get("first_request", ""), reverse=True)
    except Exception:
        logger.debug("deploy_quota: list failed", exc_info=True)
    return results
