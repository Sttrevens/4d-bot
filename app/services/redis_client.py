"""Upstash Redis REST API 客户端

统一的 Redis 操作层，供 memory_store 和 oauth_store 使用。
使用 Upstash REST API，不需要 redis-py 依赖。

Upstash 免费方案限制：
- 10,000 commands/day（足够 60 租户日常使用）
- 256MB 存储（记忆数据远低于此）

╔══════════════════════════════════════════════════════════════════╗
║  ⛔ 严禁让 Redis 客户端继承 HTTPS_PROXY / HTTP_PROXY ！         ║
║                                                                  ║
║  Upstash Redis 在国内可直连，不需要代理。                         ║
║  如果 httpx 读了全局 HTTPS_PROXY（指向 xray），xray 一挂         ║
║  → 所有 Redis 操作 Connection refused                            ║
║  → 记忆/OAuth/试用期/计量/租户同步 全部瘫痪                      ║
║  → bot 表面在跑实际半废。                                        ║
║                                                                  ║
║  这个 bug 已经在生产环境造成过事故。                              ║
║  只允许读 REDIS_PROXY 环境变量（专用），绝不继承全局代理。        ║
║  修改 _get_proxy() 前请三思！详见 CLAUDE.md 的 Pitfalls 章节。   ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()


def _get_proxy() -> str | None:
    """读取 Redis 专用代理配置。

    ⛔ 只读 REDIS_PROXY（Redis 专用）。
    ⛔ 绝对不要读 HTTPS_PROXY / HTTP_PROXY / https_proxy / http_proxy！
    ⛔ 已造成过生产事故：xray 挂 → Redis 全断 → bot 全功能瘫痪。
    ⛔ Upstash 在国内可直连，不需要代理。
    """
    # ⛔ 不要在这里加 HTTPS_PROXY / HTTP_PROXY！见文件顶部警告。
    val = os.getenv("REDIS_PROXY", "").strip()
    if val and val.startswith(("http://", "https://", "socks")):
        if "=" not in val.split("://", 1)[-1]:
            return val
        logger.warning("REDIS_PROXY 格式异常（可能 .env 缺换行），已忽略: %s", val[:80])
    return None


_REDIS_PROXY = _get_proxy()


def available() -> bool:
    """Redis 是否已配置"""
    return bool(_REDIS_URL and _REDIS_TOKEN)


def execute(*args: str | int) -> Any:
    """执行单个 Redis 命令。

    Returns:
        命令结果，类型取决于命令:
        - GET → str | None
        - SET → "OK"
        - RPUSH → int (列表长度)
        - LRANGE → list[str]
        - LLEN → int
        - DEL → int
        失败时返回 None
    """
    if not available():
        logger.warning("Redis not configured, command skipped: %s", args[0] if args else "?")
        return None

    try:
        with httpx.Client(timeout=10, proxy=_REDIS_PROXY, trust_env=False) as client:
            resp = client.post(
                _REDIS_URL,
                headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                json=[str(a) for a in args],
            )
            data = resp.json()
            if "error" in data:
                logger.warning("Redis error: %s (cmd=%s)", data["error"], args[0])
                return None
            return data.get("result")
    except Exception:
        logger.warning("Redis command failed: %s", args[0] if args else "?", exc_info=True)
        return None


def pipeline(commands: list[list[str | int]]) -> list[Any]:
    """执行 Redis pipeline（多命令批量执行，减少 RTT）。

    Returns:
        每个命令结果的列表，失败时对应位置为 None
    """
    if not available():
        return [None] * len(commands)

    try:
        with httpx.Client(timeout=15, proxy=_REDIS_PROXY, trust_env=False) as client:
            resp = client.post(
                f"{_REDIS_URL}/pipeline",
                headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                json=[[str(a) for a in cmd] for cmd in commands],
            )
            results = resp.json()
            if isinstance(results, list):
                return [r.get("result") if isinstance(r, dict) else r for r in results]
            return [None] * len(commands)
    except Exception:
        logger.warning("Redis pipeline failed", exc_info=True)
        return [None] * len(commands)


def ping() -> bool:
    """测试 Redis 连通性"""
    result = execute("PING")
    return result == "PONG"
