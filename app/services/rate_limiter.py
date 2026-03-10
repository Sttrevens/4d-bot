"""per-tenant / per-user 滑动窗口限流

基于 Redis 实现令牌桶/滑动窗口限流，保护 LLM API 和系统资源。

三层限流:
1. 全局并发限制（已有 _api_semaphore，保持不动）
2. Per-tenant 限流：每分钟最大请求数
3. Per-user 限流：每分钟最大请求数（防单用户刷爆）

实现: Redis sorted set 滑动窗口
- 每次请求往 sorted set 里插入 timestamp 作为 score
- 窗口查询: ZCOUNT key (now - window_size) now
- 定期清理过期记录: ZREMRANGEBYSCORE key -inf (now - window_size)

fail-open: Redis 不可用时放行，不阻塞业务。
"""

from __future__ import annotations

import logging
import time
import uuid

from app.services import redis_client as redis

logger = logging.getLogger(__name__)

# 默认限流参数（可被 TenantConfig 覆盖）
DEFAULT_TENANT_RPM = 60      # 每分钟最大请求数（per tenant）
DEFAULT_USER_RPM = 10        # 每分钟最大请求数（per user）
WINDOW_SIZE = 60             # 滑动窗口大小（秒）


def check_rate_limit(
    tenant_id: str,
    sender_id: str = "",
    tenant_rpm: int = 0,
    user_rpm: int = 0,
) -> tuple[bool, str]:
    """检查请求是否被限流。

    Args:
        tenant_id: 租户 ID
        sender_id: 用户 ID（为空则只检查 tenant 级别）
        tenant_rpm: 租户每分钟限额（0 = 使用默认值）
        user_rpm: 用户每分钟限额（0 = 使用默认值）

    Returns:
        (allowed, reason): allowed=True 表示放行
    """
    if not redis.available() or not tenant_id:
        return True, ""  # fail-open

    now = time.time()
    window_start = now - WINDOW_SIZE
    member = f"{now:.6f}:{uuid.uuid4().hex[:8]}"  # 唯一成员，避免去重

    try:
        # ── 租户级限流 ──
        t_limit = tenant_rpm or DEFAULT_TENANT_RPM
        t_key = f"ratelimit:tenant:{tenant_id}"

        # Pipeline: 清理过期 + 计数 + 添加新记录 + 设置过期
        t_results = redis.pipeline([
            ["ZREMRANGEBYSCORE", t_key, "-inf", str(window_start)],
            ["ZCARD", t_key],
            ["ZADD", t_key, str(now), member],
            ["EXPIRE", t_key, str(WINDOW_SIZE * 2)],
        ])

        t_count = int(t_results[1] or 0)
        if t_count >= t_limit:
            logger.warning(
                "rate limit: tenant %s exceeded (%d/%d RPM)",
                tenant_id, t_count, t_limit,
            )
            # 回滚刚添加的记录
            redis.execute("ZREM", t_key, member)
            return False, f"请求过于频繁，请稍后再试（租户限额 {t_limit}/分钟）"

        # ── 用户级限流 ──
        if sender_id:
            u_limit = user_rpm or DEFAULT_USER_RPM
            u_key = f"ratelimit:user:{tenant_id}:{sender_id}"
            u_member = f"{now:.6f}:{uuid.uuid4().hex[:8]}"

            u_results = redis.pipeline([
                ["ZREMRANGEBYSCORE", u_key, "-inf", str(window_start)],
                ["ZCARD", u_key],
                ["ZADD", u_key, str(now), u_member],
                ["EXPIRE", u_key, str(WINDOW_SIZE * 2)],
            ])

            u_count = int(u_results[1] or 0)
            if u_count >= u_limit:
                logger.warning(
                    "rate limit: user %s@%s exceeded (%d/%d RPM)",
                    sender_id[:12], tenant_id, u_count, u_limit,
                )
                redis.execute("ZREM", u_key, u_member)
                return False, f"你的请求过于频繁，请稍后再试（个人限额 {u_limit}/分钟）"

        return True, ""

    except Exception:
        logger.debug("rate limit check failed", exc_info=True)
        return True, ""  # fail-open
