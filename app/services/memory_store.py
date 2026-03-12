"""持久化存储层 —— Upstash Redis 多租户记忆存储

Key 设计:
  {tenant_id}:mem:{key}      → String (JSON 数据，用户画像/计划/调度等)
  {tenant_id}:mem:journal    → List   (事件日志，RPUSH 原子追加)

对比旧版 GitHub 存储:
- 并发安全：Redis RPUSH 原子追加，无 409 SHA 冲突
- 延迟低：<5ms vs GitHub API 200-500ms
- 多租户：key 前缀隔离，60 个租户零干扰
- 无状态：Railway 部署友好，不依赖本地文件系统

容量规划（60 租户）:
- 每租户 ~50 个 key（用户画像 + 计划 + 调度 + 索引）
- journal 每条 ~200 bytes，1000 条 ≈ 200KB/租户
- 压缩后远期记忆更紧凑，总计 < 30MB，Upstash 免费方案 256MB 绑绑有余
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.services import redis_client as redis
from app.tenant.context import get_current_tenant

logger = logging.getLogger(__name__)

# 内存缓存：full_redis_key → (data, timestamp)
_cache: dict[str, tuple[Any, float]] = {}
_CACHE_TTL = 300  # 5 分钟


def _prefix() -> str:
    """当前租户的 Redis key 前缀"""
    tenant = get_current_tenant()
    return f"{tenant.tenant_id}:mem"


# ── 核心读写 ──


def read_json(key: str) -> dict | list | None:
    """读取 JSON 数据。带内存缓存，避免频繁 Redis 调用。"""
    full_key = f"{_prefix()}:{key}"

    cached = _cache.get(full_key)
    if cached and time.time() - cached[1] < _CACHE_TTL:
        return cached[0]

    raw = redis.execute("GET", full_key)
    if raw is None:
        return None

    try:
        result = json.loads(raw)
        _cache[full_key] = (result, time.time())
        return result
    except (json.JSONDecodeError, TypeError):
        logger.warning("memory_store: failed to parse key=%s", key)
        return None


def write_json(key: str, data: dict | list, message: str = "") -> bool:
    """写入 JSON 数据。"""
    full_key = f"{_prefix()}:{key}"
    value = json.dumps(data, ensure_ascii=False)

    _MEM_TTL = 365 * 86400  # 1 year
    result = redis.execute("SET", full_key, value, "EX", str(_MEM_TTL))
    if result == "OK":
        _cache[full_key] = (data, time.time())
        return True

    logger.warning("memory_store: write_json failed key=%s", key)
    return False


def append_journal(entry: dict) -> int:
    """追加一条记录到 journal（Redis List，原子操作）。

    返回当前 journal 长度。调用方可据此判断是否需要触发压缩。
    硬上限 2000 条（安全阀，防止压缩失败时无限膨胀）。
    """
    full_key = f"{_prefix()}:journal"

    entry.setdefault("ts", time.time())
    value = json.dumps(entry, ensure_ascii=False)

    _JOURNAL_TTL = 180 * 86400  # 180 days
    # RPUSH + EXPIRE + LLEN 用 pipeline 减少 RTT
    results = redis.pipeline([
        ["RPUSH", full_key, value],
        ["EXPIRE", full_key, str(_JOURNAL_TTL)],
        ["LLEN", full_key],
    ])

    length = results[2] if len(results) > 2 and isinstance(results[2], int) else 0

    # 硬上限：压缩失败时的安全阀
    if length > 2000:
        redis.execute("LTRIM", full_key, str(-1500), str(-1))
        logger.warning("journal hard ceiling hit (%d), trimmed to 1500", length)

    return length


def read_journal(limit: int = 50) -> list[dict]:
    """读取最近的 journal 条目。"""
    full_key = f"{_prefix()}:journal"

    items = redis.execute("LRANGE", full_key, str(-limit), str(-1))
    if not items or not isinstance(items, list):
        return []

    entries = []
    for item in items:
        try:
            entries.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


def read_journal_all() -> list[dict]:
    """读取全部 journal 条目（压缩时用）。"""
    full_key = f"{_prefix()}:journal"

    items = redis.execute("LRANGE", full_key, str(0), str(-1))
    if not items or not isinstance(items, list):
        return []

    entries = []
    for item in items:
        try:
            entries.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


def rewrite_journal(entries: list[dict]) -> bool:
    """原子重写整个 journal（压缩后替换用）。

    用 pipeline: DEL + RPUSH 所有条目，最小化数据丢失窗口。
    """
    full_key = f"{_prefix()}:journal"

    if not entries:
        redis.execute("DEL", full_key)
        return True

    values = [json.dumps(e, ensure_ascii=False) for e in entries]

    _JOURNAL_TTL = 180 * 86400  # 180 days
    # DEL + RPUSH + EXPIRE pipeline
    cmds: list[list[str]] = [["DEL", full_key]]
    # RPUSH 支持多个 value
    cmds.append(["RPUSH", full_key] + values)
    cmds.append(["EXPIRE", full_key, str(_JOURNAL_TTL)])
    results = redis.pipeline(cmds)

    ok = results[-1] is not None if results else False
    if ok:
        logger.info("journal rewritten: %d entries", len(entries))
    else:
        logger.warning("journal rewrite failed")
    return ok


def journal_length() -> int:
    """获取当前 journal 长度。"""
    full_key = f"{_prefix()}:journal"
    result = redis.execute("LLEN", full_key)
    return int(result) if result else 0


def invalidate_cache(key: str | None = None) -> None:
    """清除缓存。key=None 清除全部。"""
    if key is None:
        _cache.clear()
    else:
        full_key = f"{_prefix()}:{key}"
        _cache.pop(full_key, None)
