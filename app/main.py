"""Feishu Code Bot - 入口文件"""

from __future__ import annotations

import asyncio
import json as _json
import logging

import uvicorn
from fastapi import FastAPI, Request

from fastapi.responses import HTMLResponse

from app.config import settings
from app.webhook.handler import router as webhook_router
from app.webhook.cc_bridge import router as cc_bridge_router
from app.webhook.wecom_handler import router as wecom_router
from app.webhook.wecom_kf_handler import router as wecom_kf_router
from app.webhook.qq_handler import router as qq_router
from app.admin.routes import router as admin_router
from app.services.oauth_store import exchange_code, get_token_info, init_tokens, start_background_refresh

import os as _os
from collections import deque as _deque

# ── 日志文件路径（uvicorn log_config 和 admin API 都用这个路径）──
_log_file = _os.getenv("BOT_LOG_FILE", "/app/logs/bot.log")
_log_dir = _os.path.dirname(_log_file)
if _log_dir:
    _os.makedirs(_log_dir, exist_ok=True)

# ── 内存日志环形缓冲区（dashboard 实时读取，彻底绕开文件系统）──
# 20000 条 × ~200 字节/条 ≈ 4MB，可存约 8-12 小时的日志（排除了 health check 噪音）
LOG_BUFFER: _deque = _deque(maxlen=20000)
_LOG_CACHE_TTL_SECONDS = 120
_LOG_PUSH_INTERVAL = 30
_LOG_PUSH_LINES = 1000
_LOG_CACHE_PAYLOAD_MAX_BYTES = int(_os.getenv("LOG_CACHE_PAYLOAD_MAX_BYTES", "180000"))
_LOG_PUSH_BATCH_SIZE = int(_os.getenv("LOG_PUSH_BATCH_SIZE", "3"))

# health check 等高频噪音 pattern，只排除在 buffer 外（文件/console 照常写）
_BUFFER_SKIP_PATTERNS = (
    '"GET /health HTTP',
    '"GET /admin/api/logs',   # dashboard 自己的轮询
)

class _BufferLogHandler(logging.Handler):
    """把每条日志格式化后存入内存环形缓冲区，dashboard API 直接读这里。

    过滤掉高频噪音（health check 每 30s 一条 = 一天 2880 条），
    让有限的 buffer 装更多有意义的日志。文件/console 不受影响。
    """
    def emit(self, record):
        try:
            msg = self.format(record)
            # 跳过 health check 等噪音（只是不存 buffer，file/console 照常写）
            for pat in _BUFFER_SKIP_PATTERNS:
                if pat in msg:
                    return
            LOG_BUFFER.append(msg)
        except Exception:
            pass

_buffer_handler = _BufferLogHandler()
_buffer_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
_buffer_handler.setLevel(logging.INFO)


def _read_tenant_ids_from_file(path: str) -> set[str]:
    if not path:
        return set()
    if not _os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = _json.load(fh)
    except Exception:
        logger.debug("log cache: failed to read tenant config %s", path, exc_info=True)
        return set()

    tenants = data.get("tenants", [data]) if isinstance(data, dict) else data
    if not isinstance(tenants, list):
        return set()
    ids: set[str] = set()
    for tenant in tenants:
        if not isinstance(tenant, dict):
            continue
        tid = str(tenant.get("tenant_id") or "").strip()
        if tid:
            ids.add(tid)
    return ids


def _local_log_cache_tenant_ids(registry_tenant_ids: list[str]) -> list[str]:
    """Return tenant ids physically mounted in this container for log cache writes.

    tenant_registry may contain Redis hot-loaded tenants from other containers. The
    mounted tenants.json is the physical ownership source of truth for log cache
    targets. If no tenants.json exists, keep the prior single-process behavior.
    """
    local_ids: set[str] = set()
    seen_paths: set[str] = set()
    for path in (_os.getenv("TENANTS_CONFIG_PATH", "").strip(), "/app/tenants.json"):
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        local_ids.update(_read_tenant_ids_from_file(path))

    cleaned_registry_ids = [tid for tid in registry_tenant_ids if tid]
    if not local_ids:
        return cleaned_registry_ids

    local_in_registry = [tid for tid in cleaned_registry_ids if tid in local_ids]
    return local_in_registry or sorted(local_ids)


def _utf8_tail(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[-max(0, max_bytes):].decode("utf-8", errors="ignore")


def _log_cache_payload(
    lines: list[str],
    *,
    now: float,
    max_payload_bytes: int,
    original_count: int,
) -> str:
    selected = list(lines)
    truncated = original_count > len(selected)
    while True:
        payload = {
            "lines": selected,
            "ts": now,
            "count": len(selected),
            "raw_count": original_count,
            "truncated": truncated,
        }
        raw = _json.dumps(payload, ensure_ascii=False)
        if len(raw.encode("utf-8")) <= max_payload_bytes:
            return raw
        if len(selected) > 1:
            selected = selected[1:]
            truncated = True
            continue
        if selected:
            selected = [_utf8_tail(selected[-1], max_payload_bytes // 2)]
            truncated = True
            continue
        return raw


def _filter_log_cache_lines(log_lines: list[str], tenant_id: str, max_lines: int) -> list[str]:
    tail = log_lines[-max_lines * 2:] if len(log_lines) > max_lines * 2 else log_lines
    filtered = [
        line for line in tail
        if f"tenant={tenant_id}" in line or "tenant=" not in line
    ]
    return filtered[-max_lines:]


def _build_log_cache_commands(
    log_lines: list[str],
    tenant_ids: list[str],
    *,
    now: float | None = None,
    max_lines: int | None = None,
    max_payload_bytes: int | None = None,
) -> list[list[str]]:
    """Build Redis SET commands for dashboard log cache snapshots."""
    if not log_lines or not tenant_ids:
        return []
    import time as _t

    max_lines = max(1, max_lines or _LOG_PUSH_LINES)
    max_payload_bytes = max(512, max_payload_bytes or _LOG_CACHE_PAYLOAD_MAX_BYTES)
    ts = _t.time() if now is None else now
    commands: list[list[str]] = []
    for tid in tenant_ids:
        filtered = _filter_log_cache_lines(log_lines, tid, max_lines)
        payload = _log_cache_payload(
            filtered,
            now=ts,
            max_payload_bytes=max_payload_bytes,
            original_count=len(filtered),
        )
        commands.append(["SET", f"logs:{tid}", payload, "EX", str(_LOG_CACHE_TTL_SECONDS)])
    return commands


def _chunk_log_cache_commands(commands: list[list[str]], batch_size: int | None = None) -> list[list[list[str]]]:
    size = max(1, batch_size or _LOG_PUSH_BATCH_SIZE)
    return [commands[i:i + size] for i in range(0, len(commands), size)]

# ── 预启动日志（uvicorn 启动前的 import-time 日志用 basicConfig 输出到 stderr）──
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# 预启动阶段也挂上 buffer handler，捕获 import-time 日志
logging.getLogger().addHandler(_buffer_handler)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── uvicorn log_config：让 ALL 日志（含 access / health check）同时写 console + file ──
# 这是让日志文件实时更新的唯一可靠方法。
# 之前用 module-level RotatingFileHandler + startup 挂载到 uvicorn loggers 的方案不可靠，
# 因为 uvicorn 的 access/error logger 配置了 propagate=False，
# 加上 double-import + dictConfig 交互导致 handler 可能丢失。
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": _LOG_FORMAT},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "stream": "ext://sys.stderr",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "standard",
            "filename": _log_file,
            "maxBytes": 5_242_880,     # 5 MB
            "backupCount": 3,
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "uvicorn": {
            "handlers": ["console", "file"], "level": "INFO", "propagate": False,
        },
        "uvicorn.error": {
            "level": "INFO",
        },
        "uvicorn.access": {
            "handlers": ["console", "file"], "level": "INFO", "propagate": False,
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
}

logger = logging.getLogger(__name__)

app = FastAPI(title="Feishu Code Bot", version="0.1.0")

# 跟踪所有后台任务，shutdown 时统一取消
_bg_tasks: list[asyncio.Task] = []
app.include_router(webhook_router)
app.include_router(cc_bridge_router)
app.include_router(wecom_router)
app.include_router(wecom_kf_router)
app.include_router(qq_router)
app.include_router(admin_router)


@app.on_event("startup")
async def _startup_sync():
    """启动时：加载租户 + 恢复 OAuth tokens + 同步通讯录 + 启动自我修复"""

    # ── 把 buffer handler 挂到 uvicorn loggers（dictConfig 后它们才存在）──
    # uvicorn loggers 设 propagate=False，root 上的 buffer handler 收不到它们的日志，
    # 必须直接挂到 uvicorn / uvicorn.access 上。
    for _uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _uv_lg = logging.getLogger(_uv_name)
        if _buffer_handler not in _uv_lg.handlers:
            _uv_lg.addHandler(_buffer_handler)
    logger.info("startup: log buffer active (size=%d, capacity=%d)", len(LOG_BUFFER), LOG_BUFFER.maxlen)

    # 加载多租户配置
    from app.tenant.registry import tenant_registry
    tenants_path = settings.tenants_config_path
    # 容器化部署时 tenants.json 通过 volume 挂载到 /app/tenants.json
    # 如果 TENANTS_CONFIG_PATH 环境变量没设但文件存在，自动加载
    if not tenants_path and _os.path.isfile("/app/tenants.json"):
        tenants_path = "/app/tenants.json"
    if tenants_path:
        count = tenant_registry.load_from_file(tenants_path)
        logger.info("startup: loaded %d tenants from %s", count, tenants_path)
    else:
        tenant_registry.load_default_from_env()
        logger.info("startup: using default tenant from env vars")

    # 从 Redis 加载 dashboard 添加的租户（tenants.json 是只读挂载，dashboard 写不进去）
    try:
        from app.services.tenant_sync import load_persisted_tenants
        redis_count = load_persisted_tenants()
        if redis_count:
            logger.info("startup: loaded %d dashboard-added tenants from Redis", redis_count)
    except Exception:
        logger.warning("startup: Redis tenant load failed", exc_info=True)

    # 发布租户元数据到 Redis（跨容器 admin dashboard 可见）
    try:
        from app.admin.routes import publish_tenant_meta
        meta_count = publish_tenant_meta()
        logger.info("startup: published %d tenant metadata to Redis for admin", meta_count)
    except Exception:
        logger.warning("startup: tenant meta publish failed", exc_info=True)

    # 先恢复 tokens（此时 logging 已初始化，能看到诊断日志）
    init_tokens()
    # 启动后台线程，在 token 过期前主动刷新（保持 refresh 链不断）
    start_background_refresh()

    # 从 Redis 恢复 P2P chat_id 映射（重启后立即可用）
    from app.services.user_registry import sync_org_contacts, sync_from_bot_groups, load_p2p_chats_from_redis, load_user_names_from_redis
    try:
        p2p_count = load_p2p_chats_from_redis()
        logger.info("startup: restored %d p2p chat mappings from Redis", p2p_count)
    except Exception:
        logger.warning("startup: p2p chat restore failed", exc_info=True)
    try:
        names_count = load_user_names_from_redis()
        logger.info("startup: restored %d user name mappings from Redis", names_count)
    except Exception:
        logger.warning("startup: user name restore failed", exc_info=True)

    # 遍历所有飞书租户，各自用自己的凭证同步用户（open_id 是 per-app 的）
    try:
        count = sync_org_contacts()
        logger.info("startup: synced %d org contacts (all tenants)", count)
    except Exception:
        logger.warning("startup: org contact sync failed", exc_info=True)
    try:
        count = sync_from_bot_groups()
        logger.info("startup: synced %d group members (all tenants)", count)
    except Exception:
        logger.warning("startup: group member sync failed", exc_info=True)

    # 注入自动修复回调：每次 record_error 时自动触发修复检查
    from app.services.error_log import set_error_callback
    from app.services.auto_fix import maybe_trigger_fix, startup_health_check
    set_error_callback(maybe_trigger_fix)
    logger.info("startup: auto-fix callback registered")
    logger.info(
        "startup: self_repo=%s/%s, github_repo=%s/%s, github_token=%s",
        settings.self_repo_owner, settings.self_repo_name,
        settings.github.repo_owner, settings.github.repo_name,
        "set" if settings.github.token else "MISSING",
    )

    # 预缓存所有飞书租户的 bot open_id（避免群聊 @mention 过滤失败 → 多 bot 重复回复）
    from app.webhook.handler import precache_bot_open_ids
    try:
        bot_id_count = precache_bot_open_ids()
        logger.info("startup: cached %d bot open_ids", bot_id_count)
    except Exception:
        logger.warning("startup: bot open_id precache failed", exc_info=True)

    # 恢复用户模式（/full /safe）
    from app.webhook.handler import load_user_modes_from_redis
    try:
        mode_count = load_user_modes_from_redis()
        if mode_count:
            logger.info("startup: restored %d user modes from Redis", mode_count)
    except Exception:
        logger.warning("startup: user mode restore failed", exc_info=True)

    # 恢复调度器中卡住的步骤
    from app.services.scheduler import recover_stale_steps
    try:
        recovered = recover_stale_steps()
        if recovered:
            logger.info("startup: recovered %d stuck scheduler steps", recovered)
    except Exception:
        logger.warning("startup: scheduler step recovery failed", exc_info=True)

    # 异步启动健康检查（不阻塞启动流程）
    _bg_tasks.append(asyncio.create_task(startup_health_check()))

    # 启动自主调度器（后台执行计划步骤）
    from app.services.scheduler import start_scheduler
    start_scheduler()
    logger.info("startup: autonomous scheduler started")

    # 启动 Cron Agent 调度器（NanoClaw 启发的定时 Agent 任务）
    from app.services.cron_agent import start_cron_scheduler
    _bg_tasks.append(asyncio.create_task(start_cron_scheduler()))
    logger.info("startup: cron agent scheduler started")

    # 插件系统预扫描（NanoClaw 启发的可插拔架构）
    from app.plugins.registry import plugin_registry
    plugin_count = plugin_registry.discover()
    logger.info("startup: plugin registry discovered %d tool modules", plugin_count)

    # 记录出口 IP 并启动 IP 变化监控
    _bg_tasks.append(asyncio.create_task(_log_and_monitor_ip()))

    # 启动心跳（记录 last_alive 到 Redis，用于重启后计算停机时间）
    _bg_tasks.append(asyncio.create_task(_heartbeat_loop()))

    # 定时刷新 tenant metadata 到 Redis（admin dashboard 跨容器可见性）
    _bg_tasks.append(asyncio.create_task(_tenant_meta_refresh_loop()))

    # 启动 task watchdog（后台检测未完成任务并重试）
    from app.services.task_watchdog import watchdog_loop
    _bg_tasks.append(asyncio.create_task(watchdog_loop()))

    # 启动跨容器租户配置同步（Redis 队列轮询）
    from app.services.tenant_sync import start_sync_listener
    start_sync_listener()
    logger.info("startup: tenant sync listener started")

    # 启动跨容器日志推送（每 30 秒把 LOG_BUFFER 尾部写入 Redis，供 dashboard 跨容器读取）
    _bg_tasks.append(asyncio.create_task(_log_push_loop()))

    # 重启恢复：检查停机期间的未回复消息
    _bg_tasks.append(asyncio.create_task(_recover_missed_messages()))


async def _fetch_public_ip() -> str | None:
    """获取当前出口公网 IP。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            return resp.json().get("ip")
    except Exception:
        return None


async def _log_and_monitor_ip() -> None:
    """启动时记录出口 IP，之后每 30 分钟检查是否变化。"""
    ip = await _fetch_public_ip()
    if ip:
        logger.info("startup: outbound IP = %s  (请确认已加入飞书/企微 IP 白名单)", ip)
    else:
        logger.warning("startup: 无法获取出口 IP，请手动访问 /debug/ip 确认")

    last_ip = ip
    while True:
        await asyncio.sleep(30 * 60)  # 每 30 分钟检查一次
        current_ip = await _fetch_public_ip()
        if current_ip and current_ip != last_ip:
            logger.warning(
                "⚠ 出口 IP 发生变化: %s → %s  — 需要更新飞书/企微 IP 白名单！",
                last_ip, current_ip,
            )
            last_ip = current_ip


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/diagnostics")
async def health_diagnostics():
    """深度健康检查：Redis 连通性 + LLM 代理 + 租户状态 + 错误统计。

    用于系统性排查 bot 宕机/不回复/出问题 的根因。
    """
    import time as _time
    from app.tenant.registry import tenant_registry

    checks = {"timestamp": _time.time(), "status": "ok", "checks": {}}

    # 1) Redis 连通性
    try:
        from app.services import redis_client as redis
        if redis.available():
            pong = redis.execute("PING")
            checks["checks"]["redis"] = {"status": "ok", "response": str(pong)}
        else:
            checks["checks"]["redis"] = {"status": "unavailable", "response": "redis not configured"}
    except Exception as e:
        checks["checks"]["redis"] = {"status": "error", "error": str(e)}
        checks["status"] = "degraded"

    # 2) LLM 代理（CF Worker）连通性
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=5.0)) as client:
            # 尝试访问 CF Worker 的 health 或根路径
            tenants = list(tenant_registry.all_tenants().values())
            base_url = ""
            for t in tenants:
                if t.llm_base_url:
                    base_url = t.llm_base_url
                    break
            if base_url:
                resp = await client.get(base_url.rstrip("/") + "/")
                checks["checks"]["llm_proxy"] = {
                    "status": "ok" if resp.status_code < 500 else "error",
                    "base_url": base_url,
                    "status_code": resp.status_code,
                }
            else:
                checks["checks"]["llm_proxy"] = {"status": "skip", "reason": "no llm_base_url configured"}
    except Exception as e:
        checks["checks"]["llm_proxy"] = {"status": "error", "error": str(e)[:200]}
        checks["status"] = "degraded"

    # 3) 租户列表
    tenants_info = []
    for tid, t in tenant_registry.all_tenants().items():
        tenants_info.append({
            "tenant_id": tid,
            "name": t.name,
            "platform": t.platform,
            "trial_enabled": t.trial_enabled,
            "has_greeting": bool(getattr(t, "greeting_message", "")),
        })
    checks["checks"]["tenants"] = {"count": len(tenants_info), "list": tenants_info}

    # 4) LOG_BUFFER 大小
    try:
        checks["checks"]["log_buffer"] = {"size": len(LOG_BUFFER), "max": LOG_BUFFER.maxlen}
    except Exception:
        checks["checks"]["log_buffer"] = {"status": "unavailable"}

    # 5) 最近错误（从 LOG_BUFFER 中扫描 ERROR/WARNING）
    recent_errors = []
    try:
        for line in list(LOG_BUFFER)[-500:]:
            if "[ERROR]" in line or "[WARNING]" in line:
                recent_errors.append(line[:200])
        checks["checks"]["recent_errors"] = {
            "count": len(recent_errors),
            "last_5": recent_errors[-5:] if recent_errors else [],
        }
    except Exception:
        checks["checks"]["recent_errors"] = {"status": "unavailable"}

    # 6) 静默降级检测（从最近日志中扫描已知的 fail-open 模式）
    degraded_systems = []
    try:
        recent_lines = list(LOG_BUFFER)[-2000:]
        _DEGRADED_PATTERNS = {
            "intent_fallback": ("keyword fallback", "意图分类 100% 走关键词，LLM JSON 输出不可用"),
            "exit_review_fallback": ("cannot parse scores", "Exit review 形同虚设，质量检查不工作"),
            "trial_fail_open": ("trial check failed", "试用期检查失败，过期用户可能放行"),
            "quota_fail_open": ("quota check failed", "配额检查失败，可能超额使用"),
            "redis_circuit_breaker": ("circuit breaker open", "Redis 断路器打开，所有 Redis 功能降级"),
            "metering_failed": ("metering record failed", "计量记录失败，用量数据不准确"),
            "token_quota_fail": ("token quota check failed", "Token 配额检查失败"),
        }
        for key, (pattern, desc) in _DEGRADED_PATTERNS.items():
            count = sum(1 for line in recent_lines if pattern in line)
            if count > 0:
                degraded_systems.append({"system": key, "description": desc, "occurrences": count})
        if degraded_systems:
            checks["status"] = "degraded"
        checks["checks"]["degraded_systems"] = {
            "count": len(degraded_systems),
            "systems": degraded_systems,
        }
    except Exception:
        checks["checks"]["degraded_systems"] = {"status": "unavailable"}

    return checks


def _verify_internal_token(request: Request):
    """验证 /_internal/* 接口的认证 token（复用 ADMIN_TOKEN）"""
    import os
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if not token:
        return  # 未配置则跳过验证
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        req_token = auth[7:]
    else:
        req_token = request.headers.get("x-internal-token", "")
    import hmac
    if not hmac.compare_digest(req_token, token):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/_internal/reload-tenants")
async def reload_tenants(request: Request):
    """热加载 tenants.json，不需要重启容器。

    用于 dashboard 添加 co-tenant 后触发，或 CI/CD 更新配置后调用。
    """
    _verify_internal_token(request)
    from app.tenant.registry import tenant_registry
    count = tenant_registry.reload_from_file()
    # 刷新 Redis 元数据
    try:
        from app.admin.routes import publish_tenant_meta
        publish_tenant_meta()
    except Exception:
        pass
    return {"status": "ok", "tenants_loaded": count}


@app.post("/_internal/add-tenant")
async def internal_add_tenant(request: Request):
    """接收租户配置，写入本容器的 tenants.json 并注册到 registry。

    用于跨容器 co-tenant 添加：admin API 无法直接写入其他容器的
    mounted tenants.json，所以通过 HTTP 让目标容器自己写入。
    """
    _verify_internal_token(request)
    import json
    from pathlib import Path
    from app.tenant.registry import tenant_registry

    body = await request.json()
    tenant_config = body.get("tenant_config")
    if not tenant_config or not tenant_config.get("tenant_id"):
        return {"status": "error", "message": "Missing tenant_config or tenant_id"}

    tid = tenant_config["tenant_id"]

    # 1) 写入本容器的 tenants.json（持久化，重启后仍在）
    written = False
    for candidate in ("/app/tenants.json", "tenants.json"):
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            tenants = data.get("tenants", [])
            # 去重：已存在则更新
            found = False
            for i, t in enumerate(tenants):
                if t.get("tenant_id") == tid:
                    tenants[i] = tenant_config
                    found = True
                    break
            if not found:
                tenants.append(tenant_config)
            data["tenants"] = tenants
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            written = True
            logger.info("_internal/add-tenant: wrote %s to %s", tid, path)
            break
        except Exception as e:
            logger.warning("_internal/add-tenant: failed to write %s to %s: %s", tid, path, e)

    # 2) 注册到内存 registry（立即生效）
    try:
        tenant_registry.register_from_dict(tenant_config)
        logger.info("_internal/add-tenant: registered %s in tenant_registry", tid)
    except Exception as e:
        logger.warning("_internal/add-tenant: register failed for %s: %s", tid, e)
        return {"status": "error", "message": str(e)}

    # 3) 刷新 Redis 元数据
    try:
        from app.admin.routes import publish_tenant_meta
        publish_tenant_meta()
    except Exception:
        pass

    return {"status": "ok", "tenant_id": tid, "written_to_file": written}


@app.post("/_internal/remove-tenant")
async def internal_remove_tenant(request: Request):
    """从本容器的 tenants.json 中移除租户并反注册。"""
    _verify_internal_token(request)
    import json
    from pathlib import Path
    from app.tenant.registry import tenant_registry

    body = await request.json()
    tenant_id = body.get("tenant_id", "")
    if not tenant_id:
        return {"status": "error", "message": "Missing tenant_id"}

    # 1) 从 tenants.json 移除
    removed = False
    for candidate in ("/app/tenants.json", "tenants.json"):
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            tenants = data.get("tenants", [])
            original_len = len(tenants)
            tenants = [t for t in tenants if t.get("tenant_id") != tenant_id]
            if len(tenants) < original_len:
                data["tenants"] = tenants
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                removed = True
                logger.info("_internal/remove-tenant: removed %s from %s", tenant_id, path)
            break
        except Exception as e:
            logger.warning("_internal/remove-tenant: failed for %s: %s", tenant_id, e)

    # 2) 从内存 registry 移除
    all_tenants = tenant_registry.all_tenants()
    if tenant_id in all_tenants:
        tenant_registry._tenants.pop(tenant_id, None)
        logger.info("_internal/remove-tenant: unregistered %s from registry", tenant_id)

    return {"status": "ok", "tenant_id": tenant_id, "removed_from_file": removed}


@app.get("/debug/ip")
async def debug_ip():
    """查询当前部署的出口 IP（用于更新企微 IP 白名单）"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            return resp.json()
    except Exception as e:
        return {"error": str(e)}




async def _tenant_meta_refresh_loop() -> None:
    """刷新 tenant metadata 到 Redis。

    启动后 90 秒首次重试（绕过 circuit breaker 60s 冷却），之后每 12 小时刷新。
    """
    # 启动后 90 秒延迟重试：如果首次 publish 被 circuit breaker 阻断，
    # 等冷却期过后再试一次（circuit breaker cooldown = 60s）
    await asyncio.sleep(90)
    try:
        from app.admin.routes import publish_tenant_meta
        count = publish_tenant_meta()
        logger.info("tenant_meta_refresh: startup retry published %d tenants", count)
    except Exception:
        logger.warning("tenant_meta_refresh: startup retry failed", exc_info=True)

    while True:
        await asyncio.sleep(12 * 3600)  # 12 小时
        try:
            from app.admin.routes import publish_tenant_meta
            count = publish_tenant_meta()
            if count:
                logger.info("tenant_meta_refresh: published %d tenants", count)
        except Exception:
            logger.warning("tenant_meta_refresh failed", exc_info=True)


async def _log_push_loop() -> None:
    """每 30 秒把 LOG_BUFFER 最近 N 行推到 Redis，供 dashboard 跨容器读取日志。

    Redis key: logs:{tenant_id} → JSON {lines, ts, tenant_id}
    TTL: 120 秒（容器停了自动过期，不留垃圾数据）

    每个容器只推自己加载的租户的日志（所有本容器租户共享同一个 LOG_BUFFER，
    因为一个容器只有一个 uvicorn 进程）。
    """
    from app.services import redis_client as redis
    from app.tenant.registry import tenant_registry

    await asyncio.sleep(15)  # 等启动完成，LOG_BUFFER 有内容

    while True:
        try:
            if redis.available() and LOG_BUFFER:
                buf_list = list(LOG_BUFFER)
                all_tids = list(tenant_registry.all_tenants().keys())
                local_tids = _local_log_cache_tenant_ids(all_tids)
                cmds = _build_log_cache_commands(buf_list, local_tids)
                for batch in _chunk_log_cache_commands(cmds):
                    redis.pipeline(batch)
        except Exception:
            pass  # fail-open，不影响业务
        await asyncio.sleep(_LOG_PUSH_INTERVAL)


async def _heartbeat_loop() -> None:
    """每 5 分钟写入心跳到 Redis，用于重启时计算停机时间。"""
    import json as _json
    import time as _t
    from app.services import redis_client as redis
    while True:
        try:
            if redis.available():
                redis.execute("SET", "bot:last_alive", str(_t.time()), "EX", "7200")
                # 也同步保存 in-flight 状态，防止 SIGKILL 时 shutdown handler 来不及跑
                from app.webhook.handler import get_in_flight_messages
                in_flight = get_in_flight_messages()
                if in_flight:
                    redis.execute(
                        "SET", "bot:in_flight",
                        _json.dumps(in_flight, ensure_ascii=False),
                        "EX", "600",
                    )
                else:
                    # 没有 in-flight 请求时清掉旧数据，避免重启后误恢复
                    redis.execute("DEL", "bot:in_flight")
        except Exception:
            pass
        await asyncio.sleep(600)  # 10 分钟 (was 5min→10min; saves ~144 cmds/day per container)


async def _recover_missed_messages() -> None:
    """重启后主动检查群聊中未回复的 @bot 消息。

    两层恢复：
    1. 被动恢复：临时放宽旧消息过滤阈值，让飞书重试的消息能被接收
    2. 主动恢复：精确找到停机期间 @bot 但未回复的消息，逐条处理

    精确性保证：
    - 时间窗口严格限定为 [last_alive - 60s, now]（只看停机期间）
    - 必须通过 bot open_id + mentions 字段精确匹配，不用内容猜测
    - 检查 bot 是否已有回复（parent_id），避免重复回复
    """
    await asyncio.sleep(10)  # 等待所有服务初始化完成

    import json as _json
    import time as _t
    from app.services import redis_client as redis
    from app.tenant.registry import tenant_registry
    from app.tenant.context import set_current_tenant
    from app.tools.feishu_api import feishu_get
    from app.services.feishu import FeishuClient
    from app.webhook import handler as wh

    # ── 恢复 inbox 中未处理的消息：最优先 ──
    # msg:inbox:{tenant_id} 是在 webhook 返回 200 前写入 Redis 的，
    # 如果消息处理完成会被 HDEL 清除。残留的条目 = 收到了但没处理完。
    # 这是最早期的持久化点，优先于 in_flight 和 pending_resume。
    inbox_recovered_ids: set[str] = set()
    try:
        for tenant_id, tenant in tenant_registry.all_tenants().items():
            if tenant.platform != "feishu":
                continue
            inbox_key = f"msg:inbox:{tenant_id}"
            if not redis.available():
                break
            inbox_data = redis.execute("HGETALL", inbox_key)
            if not inbox_data or not isinstance(inbox_data, list):
                continue
            # HGETALL returns [field1, value1, field2, value2, ...]
            pairs = list(zip(inbox_data[0::2], inbox_data[1::2]))
            if not pairs:
                continue
            logger.info("startup recovery: found %d inbox messages for tenant=%s", len(pairs), tenant_id)
            set_current_tenant(tenant)
            for msg_id, payload_json in pairs:
                try:
                    payload = _json.loads(payload_json)
                except (ValueError, TypeError):
                    continue
                # 跳过过旧的 inbox 条目（>10 分钟，可能是 TTL 没生效的残留）
                received_at = payload.get("received_at", 0)
                if received_at and _t.time() - received_at > 660:
                    redis.execute("HDEL", inbox_key, msg_id)
                    continue
                try:
                    await wh._dispatch_message(
                        payload.get("msg_type", "text"),
                        payload.get("message", {}),
                        payload.get("message_id", msg_id),
                        payload.get("sender_id", ""),
                        payload.get("chat_id", ""),
                        payload.get("chat_type", ""),
                    )
                    inbox_recovered_ids.add(msg_id)
                    logger.info("startup recovery: re-dispatched inbox msg %s for %s",
                                msg_id[:15], tenant_id)
                except Exception:
                    logger.warning("startup recovery: inbox re-dispatch failed for %s", msg_id[:15])
                await asyncio.sleep(0.5)
            # 清除已恢复的 inbox（处理中的会在 finally 中自行 HDEL）
            redis.execute("DEL", inbox_key)
    except Exception:
        logger.warning("startup recovery: inbox check failed", exc_info=True)

    # ── 恢复自我部署后的未完成任务：重启后自动继续 ──
    # 这个必须在 in-flight 之前处理：如果是 self_safe_deploy 触发的重启，
    # 我们要重新投递原始任务让 bot 继续，而不是告诉用户"请重发"
    resume_msg_ids: set[str] = set()
    try:
        resume_json = redis.execute("GET", "bot:pending_resume") if redis.available() else None
        if resume_json:
            redis.execute("DEL", "bot:pending_resume")
            resume_tasks: dict = _json.loads(resume_json)
            logger.info("startup recovery: found %d pending resume tasks (self-deploy)", len(resume_tasks))
            for msg_id, info in resume_tasks.items():
                tid = info.get("tenant_id", "")
                tenant = tenant_registry.get(tid)
                if not tenant or tenant.platform != "feishu":
                    continue
                set_current_tenant(tenant)
                preview = info.get("text_preview", "")
                sender_id = info.get("sender_id", "")
                chat_id = info.get("chat_id", "")
                chat_type = info.get("chat_type", "")
                if not sender_id or not preview:
                    continue
                # 重新投递，加上"继续"的上下文前缀
                resume_text = (
                    f"[系统消息：你刚刚修改了自己的代码并重启了服务，现在用新代码继续完成用户的任务]\n"
                    f"用户原始消息：{preview}"
                )
                try:
                    await wh._dispatch_message(
                        "text",
                        {"content": _json.dumps({"text": resume_text})},
                        msg_id, sender_id, chat_id, chat_type,
                    )
                    resume_msg_ids.add(msg_id)
                    logger.info("startup recovery: resumed task for sender=%s preview=%s",
                                sender_id[:12], preview[:30])
                except Exception:
                    logger.warning("startup recovery: resume dispatch failed for msg %s", msg_id[:15])
                await asyncio.sleep(0.5)
    except Exception:
        logger.warning("startup recovery: pending resume check failed", exc_info=True)

    # ── 恢复被中断的 wecom_kf in-flight 请求：优先继续原任务 ──
    try:
        kf_in_flight_json = redis.execute("GET", "bot:kf_in_flight") if redis.available() else None
        if kf_in_flight_json:
            redis.execute("DEL", "bot:kf_in_flight")
            kf_in_flight: dict = _json.loads(kf_in_flight_json)
            if kf_in_flight:
                logger.info("startup recovery: found %d interrupted wecom_kf in-flight requests", len(kf_in_flight))
                from app.webhook import wecom_kf_handler as kf_wh
                for req_id, info in kf_in_flight.items():
                    tid = info.get("tenant_id", "")
                    tenant = tenant_registry.get(tid)
                    if not tenant or tenant.platform != "wecom_kf":
                        continue
                    set_current_tenant(tenant)
                    ch = tenant.get_channel("wecom_kf")
                    if ch:
                        set_current_channel(ch)
                    external_userid = info.get("external_userid", "")
                    preview = info.get("text_preview", "")
                    if not external_userid or not preview:
                        continue
                    resume_text = (
                        "[系统消息：你刚刚在处理这个用户的请求时服务重启了，请用新代码继续完成同一个任务]\n"
                        f"用户原始消息：{preview}"
                    )
                    try:
                        await kf_wh._process_and_reply(
                            external_userid,
                            resume_text,
                            request_id=req_id,
                        )
                        logger.info(
                            "startup recovery: resumed wecom_kf task for user=%s preview=%s",
                            external_userid[:12], preview[:30],
                        )
                    except Exception:
                        logger.warning("startup recovery: wecom_kf resume failed for %s", req_id[:15], exc_info=True)
                        try:
                            await wecom_kf_client.reply_text(
                                external_userid,
                                "抱歉，你刚才的消息在处理过程中因服务更新被中断了，请重新发一下～"
                                + (f"\n\n（你说的是：{preview}...）" if preview else ""),
                            )
                        except Exception:
                            logger.warning("startup recovery: wecom_kf notify failed for %s", req_id[:15], exc_info=True)
                    await asyncio.sleep(0.5)
    except Exception:
        logger.warning("startup recovery: wecom_kf in-flight check failed", exc_info=True)

    # ── 恢复被中断的 in-flight 请求：通知用户重发 ──
    # 跳过已经通过 inbox 或 pending_resume 恢复的消息
    _already_recovered = inbox_recovered_ids | resume_msg_ids
    try:
        in_flight_json = redis.execute("GET", "bot:in_flight") if redis.available() else None
        if in_flight_json:
            redis.execute("DEL", "bot:in_flight")
            in_flight: dict = _json.loads(in_flight_json)
            # 过滤掉已经通过 inbox / resume 重新投递的
            in_flight = {k: v for k, v in in_flight.items() if k not in _already_recovered}
            if in_flight:
                logger.info("startup recovery: found %d interrupted in-flight requests", len(in_flight))
                for msg_id, info in in_flight.items():
                    tid = info.get("tenant_id", "")
                    tenant = tenant_registry.get(tid)
                    if not tenant or tenant.platform != "feishu":
                        continue
                    set_current_tenant(tenant)
                    client = FeishuClient()
                    preview = info.get("text_preview", "")
                    try:
                        await client.reply_text(
                            msg_id,
                            "抱歉，你的消息在处理过程中因服务更新被中断了，请重新发送一下～"
                            + (f"\n\n（你说的是：{preview}...）" if preview else ""),
                        )
                        logger.info("startup recovery: notified user about interrupted msg %s", msg_id[:15])
                    except Exception:
                        logger.warning("startup recovery: failed to notify for msg %s", msg_id[:15])
    except Exception:
        logger.warning("startup recovery: in-flight check failed", exc_info=True)

    # ── 恢复被中断的 batch 消息：直接重新投递 ──
    try:
        pending_json = redis.execute("GET", "bot:pending_batch") if redis.available() else None
        if pending_json:
            redis.execute("DEL", "bot:pending_batch")
            pending_batch: dict = _json.loads(pending_json)
            total_pending = sum(len(msgs) for msgs in pending_batch.values())
            logger.info("startup recovery: found %d pending batch messages from %d users",
                        total_pending, len(pending_batch))
            for uk, msgs in pending_batch.items():
                for m in msgs:
                    mid = m.get("message_id", "")
                    cid = m.get("chat_id", "")
                    ct = m.get("chat_type", "")
                    text = m.get("text", "")
                    if not mid or not text:
                        continue
                    # 使用默认租户（batch 消息来自 webhook，已经过了路由）
                    set_current_tenant(tenant_registry.get_default())
                    try:
                        await wh._dispatch_message("text", {"content": _json.dumps({"text": text})}, mid, uk.split(":", 1)[-1] if ":" in uk else uk, cid, ct)
                        logger.info("startup recovery: re-dispatched pending batch msg %s", mid[:15])
                    except Exception:
                        logger.warning("startup recovery: batch re-dispatch failed for %s", mid[:15])
                    await asyncio.sleep(0.5)
    except Exception:
        logger.warning("startup recovery: batch check failed", exc_info=True)

    # ── 计算停机时间 ──
    last_alive_str = None
    if redis.available():
        try:
            last_alive_str = redis.execute("GET", "bot:last_alive")
        except Exception:
            pass

    if last_alive_str:
        try:
            last_alive_ts = float(last_alive_str)
            downtime = _t.time() - last_alive_ts
        except (ValueError, TypeError):
            downtime = 300
            last_alive_ts = _t.time() - 300
    else:
        downtime = 0  # 首次启动，无需恢复
        last_alive_ts = _t.time()

    # 立即更新心跳
    try:
        if redis.available():
            redis.execute("SET", "bot:last_alive", str(_t.time()), "EX", "7200")
    except Exception:
        pass

    if downtime <= 0:
        logger.info("startup recovery: first boot or no downtime, skipping")
        return

    logger.info("startup recovery: downtime=%.0fs, starting recovery", downtime)

    # ── 被动恢复：临时放宽旧消息过滤阈值 ──
    # 即使是短暂重启（5-15s）也需要，因为飞书在下线期间发的 webhook 被拒后会重试
    # 同时放宽 WeChat KF handler 的阈值，sync_msg 会返回停机期间的消息
    from app.webhook import wecom_kf_handler as kf_wh
    from app.services.wecom_kf import wecom_kf_client
    original_threshold = wh._STALE_MSG_THRESHOLD
    original_kf_threshold = kf_wh._STALE_MSG_THRESHOLD
    raised = max(original_threshold, int(downtime) + 120)
    wh._STALE_MSG_THRESHOLD = raised
    kf_wh._STALE_MSG_THRESHOLD = raised
    logger.info("startup recovery: stale threshold temporarily raised to %ds (feishu + wecom_kf)", raised)

    async def _reset_threshold():
        await asyncio.sleep(180)  # 3 分钟后恢复
        wh._STALE_MSG_THRESHOLD = original_threshold
        kf_wh._STALE_MSG_THRESHOLD = original_kf_threshold
        logger.info("startup recovery: stale threshold restored to %ds", original_threshold)
    asyncio.create_task(_reset_threshold())

    # ── 主动恢复：扫描停机期间未回复的消息 ──
    # 时间窗口：last_alive 前 60s（心跳间隔兜底）到现在
    # 最多回溯 30 分钟，防止异常 last_alive 值导致扫描过多
    window_start = max(last_alive_ts - 60, _t.time() - 1800)
    window_end = _t.time()
    total_recovered = 0
    recovery_failures: list[str] = []
    is_short_restart = downtime < 30

    from app.services.user_registry import _p2p_chat_ids_map

    for tenant_id, tenant in tenant_registry.all_tenants().items():
        if tenant.platform != "feishu":
            continue
        set_current_tenant(tenant)

        try:
            # ── P2P 私聊恢复（任何停机都扫，API 开销小）──
            p2p_map = _p2p_chat_ids_map.get(tenant_id, {})
            for open_id, chat_id in list(p2p_map.items())[:20]:  # 最多扫 20 个私聊
                try:
                    msgs_data = feishu_get(
                        "/im/v1/messages",
                        params={
                            "container_id": chat_id,
                            "container_id_type": "chat",
                            "page_size": 5,
                            "sort_type": "ByCreateTimeDesc",
                        },
                    )
                    if isinstance(msgs_data, str):
                        continue
                    items = msgs_data.get("data", {}).get("items", [])
                    if not items:
                        continue

                    # 找到最后一条用户消息和最后一条 bot 消息
                    last_user_msg = None
                    last_bot_msg_time = 0.0
                    for msg in items:
                        sender = msg.get("sender", {})
                        create_time_str = msg.get("create_time", "")
                        try:
                            msg_epoch = int(create_time_str) / 1000.0
                        except (ValueError, TypeError):
                            continue

                        if sender.get("sender_type") == "app":
                            last_bot_msg_time = max(last_bot_msg_time, msg_epoch)
                        elif last_user_msg is None:
                            last_user_msg = (msg, msg_epoch)

                    if not last_user_msg:
                        continue

                    msg, msg_epoch = last_user_msg
                    message_id = msg.get("message_id", "")

                    # 条件：用户消息在停机窗口内，且 bot 没有在之后回复
                    if msg_epoch < window_start or msg_epoch > window_end:
                        continue
                    if last_bot_msg_time > msg_epoch:
                        continue  # bot 已回复

                    # dedup
                    if message_id in wh._processed_event_ids:
                        continue
                    wh._processed_event_ids[message_id] = _t.monotonic()

                    sender_id = msg.get("sender", {}).get("id", open_id)
                    msg_type = msg.get("msg_type", "text")
                    body_content = msg.get("body", {}).get("content", "{}")

                    logger.info(
                        "startup recovery: missed P2P msg %s (type=%s, age=%.0fs) from %s",
                        message_id[:15], msg_type, _t.time() - msg_epoch, sender_id[:12],
                    )

                    _tenant = tenant
                    async def _dispatch_p2p(
                        mt=msg_type, bc=body_content, mid=message_id,
                        sid=sender_id, cid=chat_id,
                    ):
                        set_current_tenant(_tenant)
                        try:
                            await wh._dispatch_message(mt, {"content": bc}, mid, sid, cid, "p2p")
                        except Exception:
                            logger.exception("startup recovery: P2P dispatch failed for %s", mid[:15])

                    asyncio.create_task(_dispatch_p2p())
                    total_recovered += 1
                    await asyncio.sleep(1)

                except Exception:
                    logger.debug("startup recovery: P2P check failed for chat %s", chat_id[:15])

            # ── 群聊恢复（仅长停机，API 开销大）──
            if is_short_restart:
                continue  # 短停机只扫 P2P，群聊靠飞书重试

            # bot open_id 只对群聊 @bot 恢复必需；不要因为它失败而跳过 P2P 恢复。
            bot_open_id = wh._get_bot_open_id(tenant)
            if not bot_open_id:
                logger.warning(
                    "startup recovery: can't get bot open_id for tenant=%s, skipping group recovery only",
                    tenant_id,
                )
                recovery_failures.append(f"feishu:{tenant_id}:missing_bot_open_id")
                continue

            data = feishu_get("/im/v1/chats", params={"page_size": 50})
            if isinstance(data, str):
                recovery_failures.append(f"feishu:{tenant_id}:list_chats_failed")
                continue

            groups = data.get("data", {}).get("items", [])

            for group in groups:
                chat_id = group.get("chat_id", "")
                if not chat_id:
                    continue

                msgs_data = feishu_get(
                    "/im/v1/messages",
                    params={
                        "container_id": chat_id,
                        "container_id_type": "chat",
                        "page_size": 30,
                        "sort_type": "ByCreateTimeDesc",
                    },
                )
                if isinstance(msgs_data, str):
                    continue

                items = msgs_data.get("data", {}).get("items", [])
                if not items:
                    continue

                # 记录 bot 已回复过的消息 ID（通过 parent_id 关联）
                bot_replied_parents: set[str] = set()
                for msg in items:
                    if msg.get("sender", {}).get("sender_type") == "app":
                        parent = msg.get("parent_id", "")
                        if parent:
                            bot_replied_parents.add(parent)

                # 从旧到新扫描，只处理停机期间的 @bot 消息
                for msg in reversed(items):
                    sender = msg.get("sender", {})
                    if sender.get("sender_type") == "app":
                        continue

                    create_time_str = msg.get("create_time", "")
                    try:
                        msg_epoch = int(create_time_str) / 1000.0
                    except (ValueError, TypeError):
                        continue

                    if msg_epoch < window_start or msg_epoch > window_end:
                        continue

                    message_id = msg.get("message_id", "")
                    if message_id in bot_replied_parents:
                        continue

                    mentions = msg.get("mentions")
                    if not isinstance(mentions, list) or not mentions:
                        continue

                    is_at_bot = False
                    for m in mentions:
                        m_id = m.get("id", {})
                        oid = m_id.get("open_id", "") if isinstance(m_id, dict) else str(m_id)
                        if oid == bot_open_id:
                            is_at_bot = True
                            break

                    if not is_at_bot:
                        continue

                    if message_id in wh._processed_event_ids:
                        continue
                    wh._processed_event_ids[message_id] = _t.monotonic()

                    sender_id = sender.get("id", "")
                    msg_type = msg.get("msg_type", "text")
                    body_content = msg.get("body", {}).get("content", "{}")

                    logger.info(
                        "startup recovery: missed group msg %s (type=%s, age=%.0fs) from %s in %s",
                        message_id[:15], msg_type, _t.time() - msg_epoch,
                        sender_id[:12], chat_id[:15],
                    )

                    _tenant = tenant
                    async def _dispatch_group(
                        mt=msg_type, bc=body_content, mid=message_id,
                        sid=sender_id, cid=chat_id,
                    ):
                        set_current_tenant(_tenant)
                        try:
                            await wh._dispatch_message(mt, {"content": bc}, mid, sid, cid, "group")
                        except Exception:
                            logger.exception("startup recovery: group dispatch failed for %s", mid[:15])

                    asyncio.create_task(_dispatch_group())
                    total_recovered += 1
                    await asyncio.sleep(1)

        except Exception:
            recovery_failures.append(f"feishu:{tenant_id}:exception")
            logger.warning("startup recovery: failed for tenant=%s", tenant_id, exc_info=True)

    # ── 企微客服恢复：用保存的 cursor+token 拉取停机期间的消息 ──
    for tenant_id, tenant in tenant_registry.all_tenants().items():
        if tenant.platform != "wecom_kf":
            continue
        set_current_tenant(tenant)

        try:
            saved_cursor, saved_token = kf_wh._load_kf_sync_state(tenant_id)
            if not saved_cursor or not saved_token:
                logger.debug("startup recovery: no saved kf cursor for %s, skip", tenant_id)
                continue

            logger.info("startup recovery: trying kf sync_msg for %s with saved cursor", tenant_id)

            data = await wecom_kf_client.sync_msg(
                callback_token=saved_token,
                cursor=saved_cursor,
                open_kfid=tenant.wecom_kf_open_kfid,
                limit=200,
            )

            if data.get("errcode", -1) != 0:
                logger.warning(
                    "startup recovery: kf sync_msg failed for %s (token may be expired): %s",
                    tenant_id, data.get("errmsg", ""),
                )
                recovery_failures.append(
                    f"wecom_kf:{tenant_id}:sync_msg_err:{data.get('errcode', -1)}"
                )
                continue

            msg_list = data.get("msg_list", [])
            if msg_list:
                logger.info("startup recovery: kf sync_msg returned %d messages for %s",
                            len(msg_list), tenant_id)
                for msg in msg_list:
                    try:
                        await kf_wh._dispatch_kf_message(msg)
                        total_recovered += 1
                    except Exception:
                        logger.warning("startup recovery: kf dispatch failed", exc_info=True)
                    await asyncio.sleep(0.5)

            # 更新 cursor
            new_cursor = data.get("next_cursor", "")
            if new_cursor:
                kf_wh._save_kf_sync_state(tenant_id, new_cursor, saved_token)
        except Exception:
            recovery_failures.append(f"wecom_kf:{tenant_id}:exception")
            logger.warning("startup recovery: kf recovery failed for %s", tenant_id, exc_info=True)

    if total_recovered:
        logger.info("startup recovery: dispatched %d missed messages total", total_recovered)
    elif recovery_failures:
        logger.warning(
            "startup recovery: recovered 0 messages but encountered %d recovery blockers: %s",
            len(recovery_failures),
            ", ".join(recovery_failures[:8]),
        )
    else:
        logger.info("startup recovery: no missed messages found (downtime=%.0fs)", downtime)


@app.on_event("shutdown")
async def _shutdown():
    """关闭时保存状态到 Redis + 取消后台任务，下次启动可恢复。"""
    import time as _t
    import json as _json
    from app.services import redis_client as redis

    # ── 1. 停止调度器循环 ──
    try:
        from app.services.scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        pass

    # ── 2. 保存状态到 Redis ──
    try:
        if redis.available():
            redis.execute("SET", "bot:last_alive", str(_t.time()), "EX", "7200")

            # 保存 in-flight 请求到 Redis，重启后可通知用户重发
            from app.webhook.handler import get_in_flight_messages
            in_flight = get_in_flight_messages()
            if in_flight:
                redis.execute(
                    "SET", "bot:in_flight",
                    _json.dumps(in_flight, ensure_ascii=False),
                    "EX", "600",  # 10 分钟 TTL
                )
                logger.info("shutdown: saved %d in-flight requests to Redis", len(in_flight))

            # 保存 wecom_kf 的 in-flight 请求，重启后优先尝试续跑
            from app.webhook.wecom_kf_handler import get_in_flight_messages as get_kf_in_flight_messages
            kf_in_flight = get_kf_in_flight_messages()
            if kf_in_flight:
                redis.execute(
                    "SET", "bot:kf_in_flight",
                    _json.dumps(kf_in_flight, ensure_ascii=False),
                    "EX", "600",
                )
                logger.info("shutdown: saved %d wecom_kf in-flight requests to Redis", len(kf_in_flight))

            # 保存待处理的批量消息，重启后可恢复处理
            from app.webhook.handler import get_pending_batch_messages
            pending = get_pending_batch_messages()
            if pending:
                redis.execute(
                    "SET", "bot:pending_batch",
                    _json.dumps(pending, ensure_ascii=False),
                    "EX", "600",
                )
                logger.info("shutdown: saved %d users' pending batch messages to Redis", len(pending))
    except Exception:
        logger.warning("shutdown: failed to save state", exc_info=True)

    # ── 3. 取消所有后台任务 ──
    for task in _bg_tasks:
        if not task.done():
            task.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
        logger.info("shutdown: cancelled %d background tasks", len(_bg_tasks))
    _bg_tasks.clear()

    # ── 4. 刷新日志缓冲区 ──
    for _h in logging.getLogger().handlers:
        try:
            _h.flush()
        except Exception:
            pass
    logging.shutdown()


async def _handle_oauth_callback(code: str, state: str, tenant_id_from_path: str = ""):
    """OAuth 回调核心逻辑（共用）"""
    logger.info("/oauth/callback received: code=%s state=%s tenant_path=%s",
                bool(code), bool(state), tenant_id_from_path or "(global)")
    if not code:
        logger.error("/oauth/callback: no code provided")
        return HTMLResponse("<h2>授权失败：未收到授权码</h2>", status_code=400)

    result = await exchange_code(code, state)
    if result:
        name = result["name"]
        scope = result.get("scope", "")
        scope_display = scope.replace(" ", ", ") if scope else "(未返回 scope 信息)"
        
        # 检查关键权限
        warnings = []
        # 邮箱权限检查（新的细粒度权限）
        mail_scopes = ["mail:user_mailbox.folder:read", "mail:user_mailbox.message:send", 
                       "mail:user_mailbox.message.body:read", "mail:user_mailbox.message.subject:read"]
        has_mail_scope = any(s in scope for s in mail_scopes)
        if not has_mail_scope:
            warnings.append("<li><b>⚠️ 邮箱权限未获得</b>：无法读取/发送邮件。<b>如果您需要使用邮件功能，请重新发送 /auth 命令并勾选所有邮箱权限</b></li>")
        if "calendar:calendar" not in scope:
            warnings.append("<li><b>⚠️ 日历权限未获得</b>：无法管理日历</li>")
        if "task:task" not in scope:
            warnings.append("<li><b>⚠️ 任务权限未获得</b>：无法管理任务</li>")
        
        warning_html = ""
        if warnings:
            warning_html = f"""
            <div style="background: #fff3cd; border: 1px solid #ffc107; padding: 10px; margin: 10px 0; border-radius: 4px;">
                <h4>权限缺失警告：</h4>
                <ul>{''.join(warnings)}</ul>
                <p>如需使用这些功能，请：<br/>
                1. 联系管理员确认飞书开发者后台已开通相关权限<br/>
                2. 重新发送 /auth 命令并<b>勾选所有需要的权限</b></p>
            </div>
            """
        
        return HTMLResponse(
            f"<h2>授权成功！</h2>"
            f"<p>用户: <b>{name}</b></p>"
            f"<p>已获取权限: {scope_display}</p>"
            f"{warning_html}"
            f"<p>可以关闭此页面，回到飞书跟 bot 说话。</p>",
        )
    return HTMLResponse("<h2>授权失败，请重试</h2>", status_code=400)


@app.get("/oauth/{tenant_id}/callback")
async def oauth_callback_per_tenant(tenant_id: str, code: str = "", state: str = ""):
    """Per-tenant OAuth 回调：nginx 按 tenant_id 路由到对应容器"""
    return await _handle_oauth_callback(code, state, tenant_id_from_path=tenant_id)


@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = ""):
    """全局 OAuth 回调（向后兼容：未迁移的租户仍走 /oauth/callback）"""
    return await _handle_oauth_callback(code, state)


def main():
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_config=_UVICORN_LOG_CONFIG,
    )


if __name__ == "__main__":
    main()
