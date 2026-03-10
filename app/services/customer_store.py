"""客户-实例关联系统

将企微客服的 external_userid 与已开通的 bot 实例绑定，
使 steven-ai 能自动识别"这个人是哪个客户"并查询对应实例状态。

Redis 数据结构：
- customer:{external_userid} → JSON {
      tenant_id:       已开通实例的 tenant_id
      name:            客户名称/公司名
      platform:        开通平台 (feishu/wecom/wecom_kf)
      port:            实例端口
      created_at:      开通时间 (ISO)
      notes:           备注
  }
- customer_index:{tenant_id} → external_userid （反向索引，按 tenant_id 查客户）

fail-open：Redis 不可用时不阻塞业务。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from app.services import redis_client as redis

logger = logging.getLogger(__name__)

_PREFIX = "customer:"
_INDEX_PREFIX = "customer_index:"
_TTL = 365 * 86400  # 1 year


def bind_customer(
    external_userid: str,
    tenant_id: str,
    name: str = "",
    platform: str = "",
    port: int = 0,
    notes: str = "",
) -> bool:
    """绑定客户与实例。provision 成功后自动调用。"""
    if not redis.available() or not external_userid or not tenant_id:
        return False
    try:
        data = {
            "tenant_id": tenant_id,
            "name": name,
            "platform": platform,
            "port": port,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "notes": notes,
        }
        redis.pipeline([
            ["SET", f"{_PREFIX}{external_userid}", json.dumps(data, ensure_ascii=False)],
            ["EXPIRE", f"{_PREFIX}{external_userid}", str(_TTL)],
            ["SET", f"{_INDEX_PREFIX}{tenant_id}", external_userid],
            ["EXPIRE", f"{_INDEX_PREFIX}{tenant_id}", str(_TTL)],
        ])
        logger.info("customer_store: bound %s → %s", external_userid[:16], tenant_id)
        return True
    except Exception:
        logger.debug("customer_store: bind failed", exc_info=True)
        return False


def get_customer(external_userid: str) -> dict | None:
    """根据 external_userid 查找客户绑定信息。"""
    if not redis.available() or not external_userid:
        return None
    try:
        raw = redis.execute("GET", f"{_PREFIX}{external_userid}")
        if raw and isinstance(raw, str):
            return json.loads(raw)
    except Exception:
        logger.debug("customer_store: get failed", exc_info=True)
    return None


def get_customer_by_tenant(tenant_id: str) -> dict | None:
    """根据 tenant_id 反向查找客户。"""
    if not redis.available() or not tenant_id:
        return None
    try:
        uid = redis.execute("GET", f"{_INDEX_PREFIX}{tenant_id}")
        if uid and isinstance(uid, str):
            return get_customer(uid)
    except Exception:
        logger.debug("customer_store: reverse lookup failed", exc_info=True)
    return None


def update_customer(external_userid: str, **updates) -> bool:
    """更新客户信息（name, notes 等）。"""
    info = get_customer(external_userid)
    if not info:
        return False
    info.update(updates)
    try:
        redis.pipeline([
            ["SET", f"{_PREFIX}{external_userid}", json.dumps(info, ensure_ascii=False)],
            ["EXPIRE", f"{_PREFIX}{external_userid}", str(_TTL)],
        ])
        return True
    except Exception:
        logger.debug("customer_store: update failed", exc_info=True)
        return False


def unbind_customer(external_userid: str) -> bool:
    """解绑客户。"""
    info = get_customer(external_userid)
    if not info:
        return False
    try:
        redis.execute("DEL", f"{_PREFIX}{external_userid}")
        tid = info.get("tenant_id", "")
        if tid:
            redis.execute("DEL", f"{_INDEX_PREFIX}{tid}")
        return True
    except Exception:
        return False


def list_customers() -> list[dict]:
    """列出所有已绑定的客户（SCAN 遍历）。"""
    if not redis.available():
        return []
    customers = []
    cursor = "0"
    try:
        for _ in range(100):
            result = redis.execute("SCAN", cursor, "MATCH", f"{_PREFIX}*", "COUNT", "50")
            if not result or not isinstance(result, list) or len(result) < 2:
                break
            cursor = str(result[0])
            keys = result[1] if isinstance(result[1], list) else []
            for key in keys:
                if not isinstance(key, str) or key.startswith(_INDEX_PREFIX):
                    continue
                uid = key[len(_PREFIX):]
                info = get_customer(uid)
                if info:
                    info["external_userid"] = uid
                    customers.append(info)
            if cursor == "0":
                break
        customers.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    except Exception:
        logger.debug("customer_store: list failed", exc_info=True)
    return customers
