"""微信客服 Webhook 事件处理

面向外部微信用户的客服场景。和内部企微 handler (wecom_handler.py) 完全独立。

核心差异:
- 回调只是通知"有新消息"，不含消息内容
- 收到通知后调 sync_msg 拉取实际消息（pull 模式）
- 用户标识是 external_userid（外部微信用户）
- 发消息有 48 小时窗口限制

回调协议:
- GET:  URL 验证（和企微自建应用相同：签名 + 解密 echostr）
- POST: 事件通知（加密 XML，Event=kf_msg_or_event）
       收到后调 sync_msg 拉取消息，再逐条处理
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Request, Response

from app.services.wecom_kf import wecom_kf_client
from app.services.wecom_crypto import decrypt_callback, verify_signature, decrypt
from app.tenant.context import get_current_tenant, set_current_tenant, set_current_channel
from app.tenant.registry import tenant_registry
from app.services.error_log import record_error
from app.webhook.base import (
    MessageDedup, UserStateManager, tuk,
    split_reply, strip_markdown, handle_mode_command, handle_status_command,
    DEFAULT_PROCESS_TIMEOUT, DEFAULT_MAX_USER_TEXT_LEN,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ── 共享基础设施 ──
_dedup = MessageDedup(max_cache=2048)
_state = UserStateManager()

# ── 回调级去重：防止企微短时间内推送相同回调多次触发重复 _pull_and_process ──
_callback_dedup: dict[str, float] = {}  # key → timestamp
_CALLBACK_DEDUP_TTL = 15  # 同一回调 15 秒内不重复处理
_CALLBACK_DEDUP_MAX = 1024  # 最大缓存条目，防止内存泄漏

# enter_session 去重：同一用户短时间内只处理一次
_enter_session_last: dict[str, float] = {}
_ENTER_SESSION_COOLDOWN = 60  # 秒

# ── sync_msg cursor 持久化 + 消息归档（context backfill） ──
_KF_ARCHIVE_TTL = 604800  # 7 天
_KF_ARCHIVE_MAX_MSGS = 50  # 每用户最多保留 50 条（25 轮）


def _save_kf_sync_state(tenant_id: str, cursor: str, token: str) -> None:
    """持久化 sync_msg cursor + callback token 到 Redis。

    cursor 跨 token 有效（企微文档：不管是否更换 token，cursor 都可用来去重消息）。
    重启后用保存的 cursor 继续拉取，跳过已处理消息，获取停机期间的新消息。
    """
    try:
        from app.services import redis_client as redis
        if not redis.available():
            return
        data = json.dumps({"cursor": cursor, "token": token, "ts": time.time()})
        redis.execute("SET", f"kf_sync:{tenant_id}", data, "EX", "86400")
    except Exception:
        logger.debug("save kf sync state failed for %s", tenant_id)


def _load_kf_sync_state(tenant_id: str) -> tuple[str, str]:
    """加载保存的 sync_msg cursor + token。返回 (cursor, token)。"""
    try:
        from app.services import redis_client as redis
        if not redis.available():
            return "", ""
        raw = redis.execute("GET", f"kf_sync:{tenant_id}")
        if not raw:
            return "", ""
        data = json.loads(raw)
        return data.get("cursor", ""), data.get("token", "")
    except Exception:
        return "", ""


def _archive_kf_msg(external_userid: str, role: str, content: str) -> None:
    """归档消息到 Redis LIST，用于对话历史回填。

    chat_history 的 TTL 较短（默认 1-2 小时），过期后对话上下文丢失。
    归档 TTL 为 7 天，当 chat_history 为空时 history.py 从归档恢复上下文。
    """
    try:
        from app.services import redis_client as redis
        from app.tenant.context import get_current_tenant
        if not redis.available():
            return
        tid = get_current_tenant().tenant_id
        key = f"kf_archive:{tid}:{external_userid}"
        msg = json.dumps({"role": role, "content": content, "ts": time.time()}, ensure_ascii=False)
        # Pipeline: LPUSH + LTRIM + EXPIRE 一次 RTT
        redis.pipeline([
            ["LPUSH", key, msg],
            ["LTRIM", key, "0", str(_KF_ARCHIVE_MAX_MSGS - 1)],
            ["EXPIRE", key, str(_KF_ARCHIVE_TTL)],
        ])
    except Exception:
        logger.debug("archive kf msg failed for %s", external_userid[:12])


def _find_local_tenant_by_kfid(open_kfid: str):
    """在本容器 tenant_registry 中查找匹配 open_kfid 的租户（co-host 场景）。"""
    for t in tenant_registry.all_tenants().values():
        if t.platform == "wecom_kf" and t.wecom_kf_open_kfid == open_kfid:
            return t
    return None

_PROCESS_TIMEOUT = DEFAULT_PROCESS_TIMEOUT
_MAX_USER_TEXT_LEN = DEFAULT_MAX_USER_TEXT_LEN
_MAX_REPLY_LEN = 2000
_MAX_REPLY_BYTES = 3800  # 企微 API text 字段字节限制（~4096 减去 JSON 包装开销）
# 超过此秒数的旧消息不再处理（防止服务重启后重放历史消息）
_STALE_MSG_THRESHOLD = 300  # 5 分钟

# ── 消息合并：用户连发多条时攒在一起处理 ──
_BATCH_WAIT = 1.5  # 秒，等新消息的窗口期
_user_pending: dict[str, list[dict]] = {}   # tuk → 待处理消息列表
_batch_timers: dict[str, asyncio.Task] = {}  # tuk → 定时器任务

_MSG_TYPE_LABELS = {
    "location": "位置",
    "link": "链接",
    "business_card": "名片",
    "miniprogram": "小程序",
}

# 可当文本读取的文件后缀
_TEXT_FILE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".kt",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala",
    ".sh", ".bash", ".zsh", ".bat", ".ps1",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".css", ".scss", ".less", ".svg",
    ".md", ".txt", ".rst", ".csv", ".log", ".env", ".gitignore",
    ".sql", ".graphql", ".proto", ".dockerfile",
}

# Gemini 多模态可直接理解的富文档/音频/图片/视频格式——下载后转 data URL 传给 LLM
_RICH_FILE_EXTS = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    # 图片（用户可能把图片当文件发送，msgtype=file 而非 image）
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    # 视频（用户可能把视频当文件发送，msgtype=file 而非 video）
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
}


# ── 路由 ──

@router.get("/webhook/wecom_kf/{tenant_id}")
async def wecom_kf_verify(tenant_id: str, request: Request) -> Response:
    """微信客服回调 URL 验证 (GET)"""
    tenant = tenant_registry.get(tenant_id)
    if not tenant or not tenant.has_platform("wecom_kf"):
        return Response("unknown tenant", status_code=404)

    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    echostr = request.query_params.get("echostr", "")

    if not all([msg_signature, timestamp, nonce, echostr]):
        return Response("missing params", status_code=400)

    kf_token = tenant.wecom_kf_token
    kf_aes_key = tenant.wecom_kf_encoding_aes_key

    if not verify_signature(kf_token, timestamp, nonce, echostr, msg_signature):
        logger.warning("wecom_kf verify failed: signature mismatch for tenant=%s", tenant_id)
        return Response("signature error", status_code=403)

    try:
        plaintext, _ = decrypt(kf_aes_key, echostr)
        logger.info("wecom_kf URL verification OK for tenant=%s", tenant_id)
        return Response(plaintext, media_type="text/plain")
    except Exception:
        logger.exception("wecom_kf echostr decrypt failed for tenant=%s", tenant_id)
        return Response("decrypt error", status_code=500)


@router.post("/webhook/wecom_kf/{tenant_id}")
async def wecom_kf_callback(tenant_id: str, request: Request) -> Response:
    """微信客服事件回调 (POST)

    企微推送 kf_msg_or_event 事件通知，我们收到后调 sync_msg 拉取消息。
    """
    tenant = tenant_registry.get(tenant_id)
    if not tenant or not tenant.has_platform("wecom_kf"):
        return Response("unknown tenant", status_code=404)

    set_current_tenant(tenant)
    ch = tenant.get_channel("wecom_kf")
    if ch:
        set_current_channel(ch)

    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")

    raw_body = await request.body()
    body_str = raw_body.decode("utf-8")

    # 解密回调 XML
    try:
        decrypted_xml = decrypt_callback(
            token=tenant.wecom_kf_token,
            encoding_aes_key=tenant.wecom_kf_encoding_aes_key,
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            post_data=body_str,
        )
    except Exception:
        logger.exception("wecom_kf callback decrypt failed for tenant=%s", tenant_id)
        return Response("decrypt error", status_code=403)

    # 解析事件 XML
    try:
        root = ET.fromstring(decrypted_xml)
    except ET.ParseError:
        logger.error("wecom_kf callback: invalid XML after decrypt")
        return Response("success")

    event = _xml_text(root, "Event")
    callback_token = _xml_text(root, "Token")
    open_kfid = _xml_text(root, "OpenKfId")

    logger.info("wecom_kf callback: tenant=%s event=%s open_kfid=%s", tenant_id, event, open_kfid)

    # ── open_kfid 分发：同 corp 下多个客服 bot 共享一个回调 URL ──
    # 如果 open_kfid 不是本 tenant 的，先看本容器有没有（co-host），
    # 有就直接切 context；没有才走 HTTP 转发到其他容器。
    if (open_kfid
            and tenant.wecom_kf_open_kfid
            and open_kfid != tenant.wecom_kf_open_kfid):
        local = _find_local_tenant_by_kfid(open_kfid)
        # 热加载：如果本地没找到，尝试从 tenants.json 重新读取
        # （dashboard 新增 co-tenant 后容器未重启的场景）
        if not local:
            local = _try_hot_load_tenant(open_kfid)
        if local:
            # co-host：同容器内的另一个租户，切 context 继续处理
            tenant = local
            set_current_tenant(tenant)
            ch_local = tenant.get_channel("wecom_kf")
            if ch_local:
                set_current_channel(ch_local)
            logger.info("wecom_kf: kf_dispatch local → %s", tenant.tenant_id)
        else:
            # 跨容器转发
            asyncio.create_task(_forward_kf_callback(
                open_kfid, raw_body, dict(request.query_params),
            ))
            return Response("success", media_type="text/plain")

    if event == "kf_msg_or_event" and callback_token:
        # 回调级去重：相同 msg_signature+timestamp+nonce 短时间内不重复处理
        cb_dedup_key = f"{tenant.tenant_id}:{msg_signature}:{timestamp}:{nonce}"
        now = time.time()
        last_seen = _callback_dedup.get(cb_dedup_key)
        if last_seen and now - last_seen < _CALLBACK_DEDUP_TTL:
            logger.info("wecom_kf callback dedup: skipping duplicate callback for tenant=%s", tenant.tenant_id)
            return Response("success", media_type="text/plain")

        # 记录并清理过期条目
        _callback_dedup[cb_dedup_key] = now
        if len(_callback_dedup) > _CALLBACK_DEDUP_MAX:
            # 清理过期条目
            expired = [k for k, v in _callback_dedup.items() if now - v > _CALLBACK_DEDUP_TTL]
            for k in expired:
                del _callback_dedup[k]

        # 异步拉取消息并处理
        asyncio.create_task(
            _pull_and_process(tenant, callback_token, open_kfid)
        )

    return Response("success", media_type="text/plain")


# ── 消息拉取与处理 ──

async def _pull_and_process(tenant, callback_token: str, open_kfid: str) -> None:
    """从 sync_msg 拉取消息，逐条处理。

    cursor 持久化到 Redis：重启后从上次位置继续拉取，
    跳过已处理消息，同时获取停机期间的新消息。
    """
    set_current_tenant(tenant)
    ch = tenant.get_channel("wecom_kf")
    if ch:
        set_current_channel(ch)

    # 从 Redis 恢复上次的 cursor，跳过已处理消息
    saved_cursor, _ = _load_kf_sync_state(tenant.tenant_id)
    cursor = saved_cursor
    retried_without_cursor = False

    while True:
        try:
            data = await wecom_kf_client.sync_msg(
                callback_token=callback_token,
                cursor=cursor,
                open_kfid=open_kfid,
                limit=200,
            )
        except Exception as exc:
            logger.exception("wecom_kf sync_msg failed")
            record_error("wecom_kf", f"sync_msg error: {exc}", exc=exc)
            break

        if data.get("errcode", -1) != 0:
            errcode = data.get("errcode", -1)
            # token 失效已在 wecom_kf_client.sync_msg 内部自动重试，
            # 如果到这里仍然报错，记录详细信息
            if errcode in (95007, 42001, 40014):
                logger.error("wecom_kf sync_msg token still invalid after refresh: errcode=%d %s",
                             errcode, data.get("errmsg", ""))
                break
            # 保存的 cursor 可能已过期，回退到无 cursor 重试一次
            if cursor and not retried_without_cursor:
                logger.warning("wecom_kf sync_msg failed with saved cursor, retrying from scratch: %s",
                               data.get("errmsg", ""))
                cursor = ""
                retried_without_cursor = True
                continue
            logger.error("wecom_kf sync_msg returned error: %s", data)
            break

        msg_list = data.get("msg_list", [])
        for msg in msg_list:
            try:
                await _dispatch_kf_message(msg)
            except Exception as exc:
                logger.exception("wecom_kf dispatch error for msg=%s", msg.get("msgid", ""))
                record_error("wecom_kf", f"dispatch error: {exc}", exc=exc)

        # 翻页
        has_more = data.get("has_more", 0)
        cursor = data.get("next_cursor", "")

        # 每页处理完后持久化 cursor（崩溃恢复时从这里继续）
        if cursor:
            _save_kf_sync_state(tenant.tenant_id, cursor, callback_token)

        if not has_more or not cursor:
            break


async def _forward_kf_callback(
    open_kfid: str, raw_body: bytes, query_params: dict,
) -> None:
    """将非本 tenant 的 kf 回调转发到正确的容器。

    企微客服回调是 per-corp 的（一个 corp 只能配一个回调 URL），
    同 corp 下多个客服账号的消息都推到同一个 URL。
    按 open_kfid 查 Redis 路由表，转发到对应容器。
    """
    import httpx
    from app.services import redis_client as redis

    raw = redis.execute("GET", f"kf_dispatch:{open_kfid}")
    if not raw:
        logger.warning("kf_dispatch: no route for open_kfid=%s, dropping", open_kfid)
        return

    try:
        route = json.loads(raw) if isinstance(raw, str) else raw
        target_tid = route["tenant_id"]
        target_port = route["port"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error("kf_dispatch: invalid route for open_kfid=%s: %s", open_kfid, e)
        return

    # 检测自转发循环：如果目标端口就是本容器，不要 HTTP 转发
    # （本容器 registry 已查过没有这个 tenant，转发给自己也找不到）
    my_port = int(os.environ.get("PORT", "8000"))
    if target_port == my_port:
        logger.warning(
            "kf_dispatch: target port %d is self (open_kfid=%s, tid=%s), "
            "tenant not loaded in this container — check tenants.json",
            target_port, open_kfid, target_tid,
        )
        return

    # NOTE: HTTP 转发在 bridge 网络模式下不可靠（127.0.0.1 指向容器自己）。
    # 大多数 co-tenant 场景已被 _try_hot_load_tenant() 从 Redis 加载覆盖，
    # 这里是 fallback（仅当租户只在另一个容器的 tenants.json 中，且未通过 dashboard 添加）。
    url = f"http://127.0.0.1:{target_port}/webhook/wecom_kf/{target_tid}"
    try:
        async with httpx.AsyncClient(proxy=None, timeout=10, trust_env=False) as client:
            resp = await client.post(
                url, content=raw_body, params=query_params,
                headers={"Content-Type": "text/xml"},
                timeout=5,
            )
            logger.info("kf_dispatch: forwarded to %s → %d", url, resp.status_code)
    except Exception as e:
        logger.error(
            "kf_dispatch: forward to %s failed: %s "
            "(bridge network? add tenant via dashboard to enable Redis-based routing)",
            url, e,
        )


def _try_hot_load_tenant(open_kfid: str):
    """尝试热加载缺失的 co-tenant（dashboard 添加后容器未重启的场景）。

    查找顺序：
    1. 本地 tenants.json（容器内文件，可能是只读挂载所以不一定有新租户）
    2. Redis tenant_cfg:*（dashboard 添加时持久化的完整配置，最可靠）
    """
    from app.tenant.config import TenantConfig
    from app.tenant.registry import tenant_registry, _resolve_env

    # 1) 先查本地 tenants.json
    try:
        from pathlib import Path
        for candidate in ("/app/tenants.json", "tenants.json"):
            path = Path(candidate)
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            for t in data.get("tenants", []):
                if t.get("wecom_kf_open_kfid") == open_kfid:
                    resolved = _resolve_env(t)
                    tenant = TenantConfig(**resolved)
                    tenant_registry.register(tenant)
                    logger.info("hot-loaded tenant %s (kfid=%s) from %s",
                                tenant.tenant_id, open_kfid, path)
                    return tenant
    except Exception as e:
        logger.warning("_try_hot_load_tenant file check failed for kfid=%s: %s", open_kfid, e)

    # 2) 查 Redis tenant_cfg:* 持久化键（dashboard 添加时 SET 的完整配置）
    try:
        from app.services import redis_client as redis_mod
        if not redis_mod.available():
            return None

        cursor = "0"
        for _ in range(20):
            result = redis_mod.execute("SCAN", cursor, "MATCH", "tenant_cfg:*", "COUNT", "50")
            if not result or not isinstance(result, list) or len(result) < 2:
                break
            cursor = str(result[0])
            keys = result[1] if isinstance(result[1], list) else []
            for key in keys:
                tid = key.replace("tenant_cfg:", "", 1) if isinstance(key, str) else ""
                if not tid:
                    continue
                raw = redis_mod.execute("GET", f"tenant_cfg:{tid}")
                if not raw:
                    continue
                try:
                    tc = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if tc.get("wecom_kf_open_kfid") == open_kfid:
                    resolved = _resolve_env(tc)
                    tenant = TenantConfig(**resolved)
                    tenant_registry.register(tenant)
                    logger.info("hot-loaded tenant %s (kfid=%s) from Redis tenant_cfg",
                                tenant.tenant_id, open_kfid)
                    return tenant
            if cursor == "0":
                break
    except Exception as e:
        logger.warning("_try_hot_load_tenant Redis check failed for kfid=%s: %s", open_kfid, e)

    return None


async def _dispatch_kf_message(msg: dict) -> None:
    """处理单条客服消息"""
    msgid = msg.get("msgid", "")
    origin = msg.get("origin", 0)  # 3=客户, 4=系统事件, 5=客服人员
    external_userid = msg.get("external_userid", "")
    msgtype = msg.get("msgtype", "")

    # ── 去重（对所有消息类型生效，包括事件）──
    # sync_msg 在并发 callback 中可能返回相同消息，必须全局去重
    if _dedup.is_duplicate(msgid):
        return

    # 系统事件 (origin=4) 中的 event 类型需要处理（如 enter_session）
    # external_userid 可能在顶层为空，但 event 子对象里有
    if origin == 4 and msgtype == "event":
        event_data = msg.get("event", {})
        event_type = event_data.get("event_type", "")
        eu = external_userid or event_data.get("external_userid", "")
        if not eu:
            logger.warning("wecom_kf: event %s has no external_userid, skipping", event_type)
            return
        await _handle_event(eu, event_type, msg)
        return

    # 只处理客户发来的消息 (origin=3)
    if origin != 3:
        logger.debug("wecom_kf: skip non-customer msg origin=%s msgtype=%s", origin, msgtype)
        return

    if not external_userid:
        return

    # 跳过过期消息：服务重启后 sync_msg 会重放历史消息，
    # 对这些过期消息回复会因 48h 窗口过期而 95001，还会引起自我介绍等奇怪行为
    send_time = msg.get("send_time", 0)
    if send_time:
        age = time.time() - send_time
        if age > _STALE_MSG_THRESHOLD:
            logger.info("wecom_kf: skip stale msg (%.0fs old) msgid=%s user=%s type=%s",
                        age, msgid[:12], external_userid[:12], msgtype)
            return

    if msgtype == "text":
        text_content = msg.get("text", {}).get("content", "")
        if text_content:
            await _handle_text(external_userid, text_content)

    elif msgtype == "image":
        media_id = msg.get("image", {}).get("media_id", "")
        if media_id:
            await _handle_media_image(external_userid, media_id)
        else:
            await _process_and_reply(external_userid, "[用户发送了一张图片]")

    elif msgtype == "voice":
        media_id = msg.get("voice", {}).get("media_id", "")
        if media_id:
            await _handle_voice(external_userid, media_id)
        else:
            await wecom_kf_client.reply_text(external_userid, "语音接收失败，请重新发送~")

    elif msgtype == "video":
        media_id = msg.get("video", {}).get("media_id", "")
        if media_id:
            await _handle_video(external_userid, media_id)
        else:
            await wecom_kf_client.reply_text(external_userid, "视频接收失败，请重新发送~")

    elif msgtype == "file":
        media_id = msg.get("file", {}).get("media_id", "")
        file_name = msg.get("file", {}).get("file_name", "unknown")
        if media_id:
            await _handle_file(external_userid, media_id, file_name)
        else:
            await wecom_kf_client.reply_text(external_userid, "文件接收失败，请重新发送~")

    elif msgtype == "event":
        event_type = msg.get("event", {}).get("event_type", "")
        await _handle_event(external_userid, event_type, msg)

    elif msgtype in ("location", "link", "business_card", "miniprogram"):
        await wecom_kf_client.reply_text(
            external_userid,
            f"收到你的{_MSG_TYPE_LABELS.get(msgtype, msgtype)}，但我暂时无法处理这类内容，请用文字描述你的需求~"
        )

    else:
        await wecom_kf_client.reply_text(
            external_userid,
            "收到你的消息了，但这个类型暂时无法处理，请用文字告诉我你的需求~"
        )


async def _handle_media_image(external_userid: str, media_id: str) -> None:
    """下载图片/表情包 → 入队合并处理"""
    data_url = await wecom_kf_client.download_media_as_data_url(media_id, "image/png")
    if data_url:
        await _enqueue_message(external_userid, "请查看这张图片", image_urls=[data_url])
    else:
        await wecom_kf_client.reply_text(external_userid, "图片下载失败，请重新发送~")


async def _handle_voice(external_userid: str, media_id: str) -> None:
    """下载语音 → Gemini 原生理解 / ffmpeg 转 WAV → Whisper 转写 → 送 LLM"""
    from app.tenant.context import get_current_tenant

    content_bytes, _ = await wecom_kf_client.download_media(media_id)
    if not content_bytes:
        await wecom_kf_client.reply_text(external_userid, "语音下载失败，请重新发送~")
        return

    tenant = get_current_tenant()

    # ── Gemini：原生音频理解，跳过 ffmpeg + Whisper ──
    if tenant.llm_provider == "gemini":
        from app.services.media_processor import media_to_data_url, detect_media_mime
        mime = detect_media_mime(content_bytes, "audio/amr")
        data_url = media_to_data_url(content_bytes, mime)
        logger.info("wecom_kf voice native passthrough (gemini): %dKB mime=%s",
                     len(content_bytes) // 1024, mime)
        await _enqueue_message(
            external_userid,
            "[语音消息] 请听取并理解这段语音，然后回复用户",
            image_urls=[data_url],
        )
        return

    # ── 非 Gemini：ffmpeg 转 WAV → Whisper STT ──
    from app.services.media_processor import convert_voice_to_wav, transcribe_audio
    from app.config import settings

    wav_bytes = await convert_voice_to_wav(content_bytes)
    if not wav_bytes:
        await wecom_kf_client.reply_text(
            external_userid,
            "语音格式转换失败，请用文字发送你的需求~"
        )
        return

    api_key = tenant.stt_api_key or settings.stt.api_key
    base_url = tenant.stt_base_url or settings.stt.base_url
    model = tenant.stt_model or settings.stt.model
    transcript = await transcribe_audio(wav_bytes, api_key, base_url, model)

    if transcript:
        await _enqueue_message(external_userid, f"[语音转文字] {transcript}")
    else:
        await wecom_kf_client.reply_text(
            external_userid,
            "收到你的语音消息~\n当前语音转写服务不可用，请用文字发送你的需求！"
        )


async def _handle_video(external_userid: str, media_id: str) -> None:
    """下载视频 → Gemini 原生理解 / ffmpeg 提取首帧 → 视觉模型分析"""
    from app.tenant.context import get_current_tenant

    content_bytes, _ = await wecom_kf_client.download_media(media_id)
    if not content_bytes:
        await wecom_kf_client.reply_text(external_userid, "视频下载失败，请重新发送~")
        return

    tenant = get_current_tenant()

    # ── Gemini：原生视频理解（画面+声音）──
    if tenant.llm_provider == "gemini":
        from app.services.gemini_provider import MAX_INLINE_VIDEO_SIZE
        if len(content_bytes) <= MAX_INLINE_VIDEO_SIZE:
            from app.services.media_processor import media_to_data_url, detect_media_mime
            mime = detect_media_mime(content_bytes, "video/mp4")
            data_url = media_to_data_url(content_bytes, mime)
            logger.info("wecom_kf video native passthrough (gemini): %dKB mime=%s",
                         len(content_bytes) // 1024, mime)
            await _enqueue_message(
                external_userid,
                "用户发送了一段视频，请观看并描述你看到和听到的内容",
                image_urls=[data_url],
            )
            return
        else:
            logger.info("wecom_kf video too large for inline (%dMB), falling back to frame",
                         len(content_bytes) // (1024 * 1024))

    # ── 非 Gemini / 大视频回退：ffmpeg 提取首帧 ──
    from app.services.media_processor import extract_video_frame, frame_to_data_url

    frame_bytes = await extract_video_frame(content_bytes)
    if not frame_bytes:
        await wecom_kf_client.reply_text(
            external_userid,
            "视频帧提取失败，请用文字描述视频内容或截图发给我~"
        )
        return

    data_url = frame_to_data_url(frame_bytes)
    await _enqueue_message(
        external_userid,
        "用户发送了一段视频，这是视频的第一帧画面，请描述你看到的内容",
        image_urls=[data_url],
    )


# MIME → 扩展名反向映射（用于文件名缺失时从 Content-Type 推断）
_MIME_TO_EXT: dict[str, str] = {}
for _ext, _mime in _RICH_FILE_EXTS.items():
    _MIME_TO_EXT[_mime] = _ext
# 常见 MIME 补充
_MIME_TO_EXT.update({
    "application/octet-stream": "",  # 通用二进制，无法推断
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/json": ".json",
})


async def _handle_file(external_userid: str, media_id: str, file_name: str) -> None:
    """下载文件 → 文本文件读内容 / 富文档走 Gemini 多模态"""
    ext = ""
    if "." in file_name:
        ext = "." + file_name.rsplit(".", 1)[-1].lower()

    # ── 文件名/扩展名缺失时，先下载再从 Content-Type 推断 ──
    # 企微 KF API 有时不返回 file_name，导致 ext="" → 所有格式检查失败
    content_bytes: bytes = b""
    content_type: str = ""
    if not ext:
        content_bytes, content_type = await wecom_kf_client.download_media(media_id)
        if not content_bytes:
            await wecom_kf_client.reply_text(external_userid, f"文件 {file_name} 下载失败，请重试。")
            return
        # 从 Content-Type 推断扩展名
        if content_type:
            guessed_ext = _MIME_TO_EXT.get(content_type, "")
            if guessed_ext:
                ext = guessed_ext
                if file_name == "unknown":
                    file_name = f"file{ext}"
                logger.info("file ext inferred from content-type: %s → %s", content_type, ext)
        # Content-Type 也推断不出来时，用 magic bytes 检测
        if not ext and content_bytes:
            from app.services.media_processor import detect_media_mime
            detected = detect_media_mime(content_bytes, "application/octet-stream")
            guessed_ext = _MIME_TO_EXT.get(detected, "")
            if guessed_ext:
                ext = guessed_ext
                if file_name == "unknown":
                    file_name = f"file{ext}"
                logger.info("file ext inferred from magic bytes: %s → %s", detected, ext)

    is_text = ext in _TEXT_FILE_EXTS
    rich_mime = _RICH_FILE_EXTS.get(ext)

    if not is_text and not rich_mime:
        await wecom_kf_client.reply_text(
            external_userid,
            f"收到文件 {file_name}，但暂不支持此格式。支持的格式：文本/代码文件、PDF、Word、Excel、PPT、音频。"
        )
        return

    # 如果还没下载过（有扩展名的正常路径），这里下载
    if not content_bytes:
        content_bytes, _ = await wecom_kf_client.download_media(media_id)
    if not content_bytes:
        await wecom_kf_client.reply_text(external_userid, f"文件 {file_name} 下载失败，请重试。")
        return

    # 富文档/音频：转 data URL 传给 Gemini 多模态理解
    if rich_mime:
        from app.services.media_processor import media_to_data_url, detect_media_mime
        # 用文件头检测真实 MIME（比扩展名可靠）
        actual_mime = detect_media_mime(content_bytes, rich_mime)
        data_url = media_to_data_url(content_bytes, actual_mime)
        size_kb = len(content_bytes) // 1024
        logger.info("wecom_kf file→multimodal: %s (%dKB, mime=%s)", file_name, size_kb, actual_mime)
        await _enqueue_message(
            external_userid,
            f"用户发送了文件「{file_name}」({size_kb}KB)，请阅读并理解文件内容，然后回复用户。",
            image_urls=[data_url],
        )
        return

    # 文本文件：解码后送 LLM
    try:
        file_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            file_text = content_bytes.decode("gbk")
        except UnicodeDecodeError:
            await wecom_kf_client.reply_text(external_userid, f"文件 {file_name} 编码无法识别。")
            return

    if len(file_text) > 30000:
        file_text = file_text[:30000] + f"\n\n... (文件被截断，总共 {len(content_bytes)} 字节)"

    user_text = f"用户发送了文件 {file_name}，内容如下：\n\n```\n{file_text}\n```"
    await _enqueue_message(external_userid, user_text)


async def _enqueue_message(
    external_userid: str, text: str, image_urls: list[str] | None = None,
) -> None:
    """消息入队：支持合并等待和 inbox 注入。

    - 如果 bot 正在为该用户处理消息，新消息直接注入 inbox（agent loop 会在工具调用间检查）
    - 否则进入合并窗口（1.5 秒内连发的消息攒在一起处理）
    """
    # ── 如果 bot 正在处理该用户的消息，直接注入信箱 ──
    if _state.is_active(external_userid):
        inbox = _state.get_inbox(external_userid)
        if inbox is not None:
            await inbox.put({"text": text, "images": image_urls})
            logger.info("wecom_kf: injected message into active session for %s: %s",
                        external_userid[:12], text[:50])
            return

    # ── 否则走正常的合并等待流程 ──
    uk = tuk(external_userid)
    if uk not in _user_pending:
        _user_pending[uk] = []

    _user_pending[uk].append({
        "text": text,
        "images": image_urls,
        "external_userid": external_userid,
    })

    # 取消之前的定时器，重新计时
    old = _batch_timers.pop(uk, None)
    if old:
        old.cancel()

    _batch_timers[uk] = asyncio.create_task(
        _flush_after_wait(external_userid)
    )


async def _flush_after_wait(external_userid: str) -> None:
    """等待窗口期结束，合并文本，逐个处理媒体。"""
    await asyncio.sleep(_BATCH_WAIT)

    uk = tuk(external_userid)
    batch = _user_pending.pop(uk, [])
    _batch_timers.pop(uk, None)

    if not batch:
        return

    # 合并文本
    texts = [m["text"] for m in batch if m.get("text")]
    all_images: list[str] = []
    for m in batch:
        if m.get("images"):
            all_images.extend(m["images"])

    user_text = "\n".join(texts) if texts else ""
    if not user_text and not all_images:
        return

    logger.info("wecom_kf: batched %d messages from %s (text merged, %d images)",
                len(batch), external_userid[:12], len(all_images))

    # 第一批图片随文本一起发
    first_images = all_images[:1] if all_images else None
    remaining_images = all_images[1:]

    await _process_and_reply(
        external_userid,
        user_text or "[用户发送了图片]",
        image_urls=first_images,
        remaining_images=remaining_images,
    )


async def _try_intercept_sms_code(external_userid: str, text: str) -> bool:
    """检查是否有小红书 SMS 登录等待用户输入，拦截手机号或验证码。

    返回 True 表示消息已被拦截处理，不需要走正常流程。
    """
    import re
    try:
        from app.tenant.context import get_current_tenant
        from app.services.redis_client import execute as redis_execute

        tenant = get_current_tenant()
        # 构造与 xhs_ops 相同的 session_key
        session_key = f"{tenant.tenant_id}:{external_userid}"
        pending_key = f"xhs:sms_pending:{session_key}"
        code_key = f"xhs:sms_code:{session_key}"

        pending = redis_execute("GET", pending_key)
        if not pending:
            return False

        clean = text.strip()

        if pending == "phone":
            # 等待手机号：匹配 11 位数字（中国手机号）
            phone_match = re.search(r'1[3-9]\d{9}', clean)
            if phone_match:
                redis_execute("SET", code_key, phone_match.group(), "EX", 120)
                logger.info("wecom_kf: intercepted phone number for XHS SMS login: %s***",
                            phone_match.group()[:3])
                return True
        elif pending == "code":
            # 等待验证码：匹配 4-6 位数字
            code_match = re.match(r'^\d{4,6}$', clean)
            if code_match:
                redis_execute("SET", code_key, clean, "EX", 120)
                logger.info("wecom_kf: intercepted SMS code for XHS login")
                return True

        # 不匹配预期格式 → 不拦截，走正常流程
        return False

    except Exception as e:
        logger.debug("wecom_kf: SMS intercept check failed: %s", e)
        return False


async def _handle_text(external_userid: str, text: str) -> None:
    """处理客户文本消息"""
    text = text.strip()
    if not text:
        return

    async def _reply(msg: str) -> None:
        await wecom_kf_client.reply_text(external_userid, msg)

    # 斜杠命令（不走合并）
    if await handle_mode_command(text, external_userid, _state, _reply):
        return

    if text.strip().lower() == "/status":
        await handle_status_command(external_userid, _state, "微信客服", _reply)
        return

    # ── 小红书 SMS 验证码拦截 ──
    # 如果 xhs_ops 正在等待用户输入手机号或验证码，拦截短数字消息
    if await _try_intercept_sms_code(external_userid, text):
        return

    # 正常消息 → 入队合并处理
    await _enqueue_message(external_userid, text)


async def _handle_event(external_userid: str, event_type: str, msg: dict) -> None:
    """处理客服事件"""
    if event_type == "enter_session":
        # ── stale 检查：跳过过期的 enter_session 事件 ──
        send_time = msg.get("send_time", 0)
        if send_time:
            age = time.time() - send_time
            if age > _STALE_MSG_THRESHOLD:
                logger.info("wecom_kf: skip stale enter_session (%.0fs old) user=%s",
                            age, external_userid[:12])
                return

        # ── 去重：同一用户短时间内只处理一次 enter_session ──
        tkey = tuk(external_userid)
        now = time.time()
        last = _enter_session_last.get(tkey, 0)
        if now - last < _ENTER_SESSION_COOLDOWN:
            logger.info("wecom_kf: skip duplicate enter_session for user=%s (%.0fs since last)",
                        external_userid[:12], now - last)
            return
        _enter_session_last[tkey] = now

        # 尝试接入为智能助手 (state=1)，这样后续 send_msg 才能正常工作
        accept = await wecom_kf_client.trans_service_state(external_userid, service_state=1)
        if accept.get("errcode", -1) != 0:
            state_info = await wecom_kf_client.get_service_state(external_userid)
            cur_state = state_info.get("service_state", "?")
            if cur_state == 3:
                logger.error(
                    "wecom_kf: session stuck in state=3 (servicer=%s) for user=%s. "
                    "【管理员操作】请到企业微信后台→微信客服→接待方式，"
                    "改为「智能助手接待」而非「人工接待」，否则 bot 无法回复消息。",
                    state_info.get("servicer_userid", "?"), external_userid[:12])

        # ── 欢迎语：解决试用 bot 扫码进入后空白"闪退"问题 ──
        # 如果租户配置了 greeting_message，发送欢迎语让用户知道 bot 在线。
        # 没配置则不打招呼（等用户先开口）。
        tenant = get_current_tenant()
        greeting = getattr(tenant, "greeting_message", "") if tenant else ""
        if greeting:
            try:
                await _safe_send(external_userid, greeting)
                logger.info("wecom_kf: enter_session accepted for user=%s (state→1, greeting sent)",
                            external_userid[:12])
            except Exception as exc:
                logger.warning("wecom_kf: enter_session greeting failed for user=%s: %s",
                               external_userid[:12], exc)
        else:
            logger.info("wecom_kf: enter_session accepted for user=%s (state→1, no greeting)",
                        external_userid[:12])

    elif event_type == "msg_send_fail":
        logger.warning("wecom_kf: msg send failed for user=%s, event=%s",
                        external_userid, msg.get("event", {}))
    else:
        logger.debug("wecom_kf: unhandled event type=%s", event_type)


async def _safe_send(external_userid: str, text: str, _hit_limit: list[bool] | None = None) -> None:
    """安全发送消息：检测到 95001（发送限额）后停止重试"""
    if _hit_limit and _hit_limit[0]:
        return  # 已触发限额，不再尝试发送
    logger.info("wecom_kf: reply to %s: %s", external_userid[:12], text)
    result = await wecom_kf_client.reply_text(external_userid, text)
    if isinstance(result, dict) and result.get("errcode") == 95001:
        logger.warning("wecom_kf: 95001 send limit hit for user=%s, stopping sends",
                        external_userid[:12])
        if _hit_limit is not None:
            _hit_limit[0] = True


async def _process_and_reply(
    external_userid: str,
    text: str,
    image_urls: list[str] | None = None,
    remaining_images: list[str] | None = None,
) -> None:
    """agent 处理 + 发送回复（支持 inbox 消息注入）"""
    if len(text) > _MAX_USER_TEXT_LEN:
        text = text[:_MAX_USER_TEXT_LEN] + "\n(消息过长已截断)"

    # 归档用户消息（用于 chat_history TTL 过期后的上下文回填）
    _archive_kf_msg(external_userid, "user", text)

    # 共享标志：一旦某次发送触发 95001，后续发送全部跳过
    hit_send_limit: list[bool] = [False]

    # 趁机清理不活跃的用户状态
    _state.cleanup_idle()

    async with _state.get_lock(external_userid):
        # 创建信箱，让 agent 循环能收到实时插入的消息
        inbox = _state.activate(external_userid)

        # 将剩余图片通过 inbox 逐个注入（避免一次性发太多 inline_data）
        if remaining_images:
            async def _inject_remaining() -> None:
                await asyncio.sleep(0.5)  # 等 agent loop 启动
                for i, img_url in enumerate(remaining_images):
                    if not _state.is_active(external_userid):
                        break
                    ib = _state.get_inbox(external_userid)
                    if ib is None:
                        break
                    await ib.put({"text": "", "images": [img_url]})
                    logger.info("wecom_kf: queued image %d/%d into inbox for %s",
                                i + 1, len(remaining_images), external_userid[:12])
            asyncio.create_task(_inject_remaining())

        try:
            reply = await asyncio.wait_for(
                _do_agent_work(external_userid, text, image_urls=image_urls,
                               _hit_send_limit=hit_send_limit, inbox=inbox),
                timeout=_PROCESS_TIMEOUT,
            )
            # 归档 bot 回复
            if reply:
                _archive_kf_msg(external_userid, "assistant", reply)

            display_reply = strip_markdown(reply) if reply else reply
            for chunk in split_reply(display_reply, _MAX_REPLY_LEN, max_bytes=_MAX_REPLY_BYTES):
                await _safe_send(external_userid, chunk, hit_send_limit)

        except asyncio.TimeoutError:
            logger.error("wecom_kf: processing timeout for user=%s", external_userid)
            record_error("timeout", f"wecom_kf timeout user={external_userid}")
            from app.services.base_agent import build_timeout_message
            await _safe_send(external_userid, build_timeout_message(), hit_send_limit)
        except Exception as exc:
            logger.exception("wecom_kf: process error for user=%s", external_userid)
            record_error("unhandled", f"wecom_kf process error user={external_userid}", exc=exc)
            await _safe_send(external_userid, "不好意思出了点小状况~ 你再发一遍试试？", hit_send_limit)
        finally:
            _state.deactivate(external_userid)


async def _do_agent_work(
    external_userid: str,
    text: str,
    image_urls: list[str] | None = None,
    _hit_send_limit: list[bool] | None = None,
    inbox: asyncio.Queue | None = None,
) -> str:
    """调用 LLM agent 处理消息"""
    sender_name = await wecom_kf_client.get_customer_name(external_userid)
    if not sender_name:
        sender_name = f"微信用户({external_userid[:8]})"
    # 无论名字来自 API 还是 fallback，都注册到 user_registry（持久化到 Redis）
    from app.services.user_registry import register as register_user
    register_user(external_userid, sender_name)
    sender_id = external_userid

    # 设置 _current_user_open_id，让工具层（如 xhs_ops 发送二维码）能获取当前用户 ID
    from app.tools.feishu_api import _current_user_open_id
    _current_user_open_id.set(sender_id)

    mode = _state.get_mode(external_userid)

    async def _send_progress(msg: str) -> None:
        msg = strip_markdown(msg)
        for chunk in split_reply(msg, _MAX_REPLY_LEN, max_bytes=_MAX_REPLY_BYTES):
            await _safe_send(external_userid, chunk, _hit_send_limit)

    from app.router.intent import route_message
    return await route_message(
        user_text=text,
        sender_id=sender_id,
        sender_name=sender_name,
        on_progress=_send_progress,
        image_urls=image_urls,
        mode=mode,
        inbox=inbox,
    )


def _xml_text(root: ET.Element, tag: str) -> str:
    """从 XML element 中安全提取文本"""
    elem = root.find(tag)
    return elem.text.strip() if elem is not None and elem.text else ""
