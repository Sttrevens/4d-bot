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
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
)

_client_lock = threading.Lock()
_shared_client: httpx.Client | None = None

_TRANSIENT_CB_THRESHOLD = int(os.getenv("REDIS_TRANSIENT_CB_THRESHOLD", "3"))
_TRANSIENT_CB_COOLDOWN = float(os.getenv("REDIS_TRANSIENT_CB_COOLDOWN", "20"))
_ERROR_LOG_COOLDOWN = float(os.getenv("REDIS_ERROR_LOG_COOLDOWN", "60"))

_state_lock = threading.Lock()
_transient_cb_open_until: float = 0.0
_consecutive_transient_failures: int = 0
_last_error_type: str = ""
_last_error_message: str = ""
_last_error_at: float = 0.0
_last_error_log_at: dict[str, float] = {}
_suppressed_error_counts: dict[str, int] = {}

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


def _transient_cb_is_open() -> bool:
    """Short fail-open breaker for repeated Redis network/client timeouts."""
    global _transient_cb_open_until
    if _transient_cb_open_until <= 0:
        return False
    if time.monotonic() >= _transient_cb_open_until:
        _transient_cb_open_until = 0.0
        logger.info("Redis transient circuit breaker CLOSED (cooldown expired)")
        return False
    return True


def _is_rate_limit_error(error_str: str) -> bool:
    """检测是否为限额错误"""
    return "max requests limit" in error_str.lower() or "rate limit" in error_str.lower()


def _record_success() -> None:
    global _consecutive_transient_failures
    with _state_lock:
        _consecutive_transient_failures = 0


def _record_failure(exc: Exception) -> None:
    """Track Redis failures for diagnostics and transient fail-open behavior."""
    global _transient_cb_open_until, _consecutive_transient_failures
    global _last_error_type, _last_error_message, _last_error_at

    now = time.monotonic()
    with _state_lock:
        _last_error_type = type(exc).__name__
        _last_error_message = str(exc)[:300]
        _last_error_at = time.time()
        if isinstance(exc, _TRANSIENT_ERRORS):
            _consecutive_transient_failures += 1
            if _consecutive_transient_failures >= _TRANSIENT_CB_THRESHOLD:
                _transient_cb_open_until = now + _TRANSIENT_CB_COOLDOWN
        else:
            _consecutive_transient_failures = 0


def _log_failure(operation: str, exc: Exception) -> None:
    """Log the first traceback for repeated Redis failures, then aggregate."""
    now = time.monotonic()
    key = f"{operation}:{type(exc).__name__}"
    with _state_lock:
        last = _last_error_log_at.get(key, 0.0)
        if now - last < _ERROR_LOG_COOLDOWN:
            _suppressed_error_counts[key] = _suppressed_error_counts.get(key, 0) + 1
            logger.debug(
                "Redis %s failed (%s); traceback suppressed (%d repeat(s))",
                operation,
                type(exc).__name__,
                _suppressed_error_counts[key],
            )
            return
        suppressed = _suppressed_error_counts.pop(key, 0)
        _last_error_log_at[key] = now

    suffix = f" ({suppressed} repeated traceback(s) suppressed)" if suppressed else ""
    logger.warning("Redis %s failed%s", operation, suffix, exc_info=True)

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
            if timeout is None:
                return client.post(url, json=json_data)
            return client.post(url, json=json_data, timeout=timeout)
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
    if _transient_cb_is_open():
        return None

    try:
        resp = _post_with_retry(_REDIS_URL, [str(a) for a in args])
        data = resp.json()
        _record_success()
        if "error" in data:
            err = data["error"]
            if _is_rate_limit_error(str(err)):
                _cb_trip(str(err))
            else:
                logger.warning("Redis error: %s (cmd=%s)", err, args[0])
            return None
        return data.get("result")
    except Exception as e:
        _record_failure(e)
        _log_failure(f"command {args[0] if args else '?'}", e)
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
    if _transient_cb_is_open():
        return [None] * len(commands)

    try:
        resp = _post_with_retry(
            f"{_REDIS_URL}/pipeline",
            [[str(a) for a in cmd] for cmd in commands],
        )
        results = resp.json()
        _record_success()
        if isinstance(results, list):
            # 检查 pipeline 结果中是否有限额错误
            for r in results:
                if isinstance(r, dict) and "error" in r and _is_rate_limit_error(str(r["error"])):
                    _cb_trip(str(r["error"]))
                    return [None] * len(commands)
            return [r.get("result") if isinstance(r, dict) else r for r in results]
        return [None] * len(commands)
    except Exception as e:
        _record_failure(e)
        _log_failure("pipeline", e)
        return [None] * len(commands)


def ping() -> bool:
    """测试 Redis 连通性"""
    result = execute("PING")
    return result == "PONG"


def diagnostics() -> dict:
    """Return Redis diagnostic info for admin debugging."""
    cb_remaining = 0.0
    if _cb_open_until > 0:
        cb_remaining = max(0, _cb_open_until - time.monotonic())
    transient_remaining = 0.0
    if _transient_cb_open_until > 0:
        transient_remaining = max(0, _transient_cb_open_until - time.monotonic())
    rate_cb_open = _cb_is_open()
    transient_cb_open = _transient_cb_is_open()
    return {
        "configured": available(),
        "url_set": bool(_REDIS_URL),
        "token_set": bool(_REDIS_TOKEN),
        "proxy": _REDIS_PROXY or "(none)",
        "circuit_breaker_open": rate_cb_open or transient_cb_open,
        "rate_limit_circuit_breaker_open": rate_cb_open,
        "circuit_breaker_remaining_s": round(cb_remaining, 1),
        "transient_circuit_open": transient_cb_open,
        "transient_circuit_remaining_s": round(transient_remaining, 1),
        "consecutive_transient_failures": _consecutive_transient_failures,
        "last_error_type": _last_error_type,
        "last_error_message": _last_error_message,
        "last_error_at": _last_error_at,
        "ping": ping() if available() and not rate_cb_open and not transient_cb_open else False,
    }
