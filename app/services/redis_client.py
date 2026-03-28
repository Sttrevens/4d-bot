"""Upstash Redis REST API 客户端

统一的 Redis 操作层，供 memory_store 和 oauth_store 使用。
使用 Upstash REST API，不需要 redis-py 依赖。

Upstash 免费方案限制：
- 500,000 commands/day（后台轮询 3 容器 ~130K/day 空闲消耗）
- 256MB 存储（记忆数据远低于此）
- 内置 circuit breaker：限额耗尽时自动熔断 60 秒，避免无效请求

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
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── 连接池复用 ──
# 避免每次请求都新建 TCP+TLS 连接，减少 SSL 握手失败率。
# 瞬态错误（SSL EOF、超时、断连）自动重试，指数退避。
_RETRY_MAX = 3
_RETRY_BACKOFF = (0.5, 1.0, 2.0)  # 每次重试的等待秒数
_TRANSIENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
)

_client_lock = threading.Lock()
_shared_client: httpx.Client | None = None

# ── Circuit Breaker ──
# 当 Upstash 返回 "max requests limit exceeded" 时，停止请求 _CB_COOLDOWN 秒。
# 避免后台轮询（tenant_sync 5s / task_watchdog 30s）在限额耗尽后继续无效请求。
_CB_COOLDOWN = 60  # 熔断冷却期（秒）
_cb_open_until: float = 0.0  # 熔断截止时间戳（0 = 正常）


def _cb_trip(error_msg: str) -> None:
    """触发熔断"""
    global _cb_open_until
    _cb_open_until = time.monotonic() + _CB_COOLDOWN
    logger.warning("Redis circuit breaker OPEN for %ds: %s", _CB_COOLDOWN, error_msg[:120])


def _cb_is_open() -> bool:
    """检查熔断是否生效"""
    global _cb_open_until
    if _cb_open_until <= 0:
        return False
    if time.monotonic() >= _cb_open_until:
        _cb_open_until = 0.0
        logger.info("Redis circuit breaker CLOSED (cooldown expired)")
        return False
    return True


def _is_rate_limit_error(error_str: str) -> bool:
    """检测是否为限额错误"""
    return "max requests limit" in error_str.lower() or "rate limit" in error_str.lower()

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


def _get_client() -> httpx.Client:
    """获取或创建共享 httpx Client（连接池复用，减少 TLS 握手）。"""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        return _shared_client
    with _client_lock:
        # double-check
        if _shared_client is not None and not _shared_client.is_closed:
            return _shared_client
        _shared_client = httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
            proxy=_REDIS_PROXY,
            trust_env=False,  # ⛔ 绝不继承全局代理
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30,
            ),
            headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
        )
        return _shared_client


def _post_with_retry(url: str, json_data: Any, *, timeout: float | None = None) -> httpx.Response:
    """带重试的 POST 请求。瞬态网络错误（SSL EOF、超时、断连）自动重试。"""
    last_exc: Exception | None = None
    for attempt in range(_RETRY_MAX):
        try:
            client = _get_client()
            return client.post(url, json=json_data)
        except _TRANSIENT_ERRORS as e:
            last_exc = e
            if attempt < _RETRY_MAX - 1:
                wait = _RETRY_BACKOFF[attempt]
                logger.info("Redis transient error (attempt %d/%d), retrying in %.1fs: %s",
                            attempt + 1, _RETRY_MAX, wait, type(e).__name__)
                time.sleep(wait)
                # 连接可能已损坏，关闭旧 client 让下次重建
                _reset_client()
            else:
                logger.warning("Redis request failed after %d attempts: %s", _RETRY_MAX, e)
        except Exception as e:
            # 非瞬态错误（如 JSON 编码错误），直接抛出
            raise
    raise last_exc  # type: ignore[misc]


def _reset_client() -> None:
    """关闭并重置共享 client，下次请求会重建。"""
    global _shared_client
    with _client_lock:
        if _shared_client is not None:
            try:
                _shared_client.close()
            except Exception:
                pass
            _shared_client = None


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
    if _cb_is_open():
        logger.warning("Redis command skipped (circuit breaker open): %s", args[0] if args else "?")
        return None

    try:
        resp = _post_with_retry(_REDIS_URL, [str(a) for a in args])
        data = resp.json()
        if "error" in data:
            err = data["error"]
            if _is_rate_limit_error(str(err)):
                _cb_trip(str(err))
            else:
                logger.warning("Redis error: %s (cmd=%s)", err, args[0])
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
    if _cb_is_open():
        return [None] * len(commands)

    try:
        resp = _post_with_retry(
            f"{_REDIS_URL}/pipeline",
            [[str(a) for a in cmd] for cmd in commands],
        )
        results = resp.json()
        if isinstance(results, list):
            # 检查 pipeline 结果中是否有限额错误
            for r in results:
                if isinstance(r, dict) and "error" in r and _is_rate_limit_error(str(r["error"])):
                    _cb_trip(str(r["error"]))
                    return [None] * len(commands)
            return [r.get("result") if isinstance(r, dict) else r for r in results]
        return [None] * len(commands)
    except Exception:
        logger.warning("Redis pipeline failed", exc_info=True)
        return [None] * len(commands)


def ping() -> bool:
    """测试 Redis 连通性"""
    result = execute("PING")
    return result == "PONG"


def diagnostics() -> dict:
    """Return Redis diagnostic info for admin debugging."""
    import math
    cb_remaining = 0.0
    if _cb_open_until > 0:
        cb_remaining = max(0, _cb_open_until - time.monotonic())
    return {
        "configured": available(),
        "url_set": bool(_REDIS_URL),
        "token_set": bool(_REDIS_TOKEN),
        "proxy": _REDIS_PROXY or "(none)",
        "circuit_breaker_open": _cb_is_open(),
        "circuit_breaker_remaining_s": round(cb_remaining, 1),
        "ping": ping() if available() and not _cb_is_open() else False,
    }
