"""Feishu Code Bot - 入口文件"""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from fastapi import FastAPI

from fastapi.responses import HTMLResponse

from app.config import settings
from app.webhook.handler import router as webhook_router
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
LOG_BUFFER: _deque = _deque(maxlen=3000)

class _BufferLogHandler(logging.Handler):
    """把每条日志格式化后存入内存环形缓冲区，dashboard API 直接读这里。"""
    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass

_buffer_handler = _BufferLogHandler()
_buffer_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
_buffer_handler.setLevel(logging.INFO)

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
    from app.services.user_registry import sync_org_contacts, sync_from_bot_groups, load_p2p_chats_from_redis
    try:
        p2p_count = load_p2p_chats_from_redis()
        logger.info("startup: restored %d p2p chat mappings from Redis", p2p_count)
    except Exception:
        logger.warning("startup: p2p chat restore failed", exc_info=True)

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


def _verify_internal_token(request: Request):
    """验证 /_internal/* 接口的认证 token（复用 ADMIN_TOKEN）"""
    import os
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if not token:
        # ADMIN_TOKEN 未配置时允许访问（向后兼容本地开发）
        return
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        req_token = auth[7:]
    else:
        req_token = request.headers.get("x-internal-token", "")
    import hmac
    if not hmac.compare_digest(req_token, token):
        from fastapi.responses import JSONResponse
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
    """每 12 小时刷新 tenant metadata 到 Redis，防止 24h TTL 过期后 dashboard 看不到租户。"""
    while True:
        await asyncio.sleep(12 * 3600)  # 12 小时
        try:
            from app.admin.routes import publish_tenant_meta
            count = publish_tenant_meta()
            if count:
                logger.info("tenant_meta_refresh: published %d tenants", count)
        except Exception:
            logger.debug("tenant_meta_refresh failed", exc_info=True)


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
        await asyncio.sleep(300)


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
    is_short_restart = downtime < 30

    from app.services.user_registry import _p2p_chat_ids_map

    for tenant_id, tenant in tenant_registry.all_tenants().items():
        if tenant.platform != "feishu":
            continue
        set_current_tenant(tenant)

        try:
            # 获取 bot 的 open_id（复用 handler 的多层 fallback 逻辑）
            bot_open_id = wh._get_bot_open_id(tenant)

            if not bot_open_id:
                logger.warning(
                    "startup recovery: can't get bot open_id for tenant=%s, skipping",
                    tenant_id,
                )
                continue

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

            data = feishu_get("/im/v1/chats", params={"page_size": 50})
            if isinstance(data, str):
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
            logger.warning("startup recovery: kf recovery failed for %s", tenant_id, exc_info=True)

    if total_recovered:
        logger.info("startup recovery: dispatched %d missed messages total", total_recovered)
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
