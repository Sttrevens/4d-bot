"""飞书 OAuth 用户 Token 存储

存储每个用户的 user_access_token + refresh_token。
Token 过期时自动刷新。

持久化：Upstash Redis，按租户隔离。
Key 格式: {tenant_id}:oauth_tokens → JSON { open_id: {...token_data} }
"""

from __future__ import annotations

import base64
import json as _json
import logging
import os
import secrets
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlencode

import threading

import httpx

from app.config import settings
from app.services import redis_client as redis

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

# OAuth 用户授权 scope
# 注意：只放飞书权限列表中确认存在的 scope 名称。
# 无效 scope 可能被飞书静默忽略（token 里没有该权限但不报错），
# 导致授权看似成功但 API 调用全部 99991679。
# docx/bitable/drive/wiki/contact 等工具用 tenant_token，不需要 OAuth scope。
_SCOPES = " ".join([
    # 日历 — 已确认
    "calendar:calendar",
    # 任务 — 已确认
    "task:task",
    "task:task:write",
    "task:tasklist:write",
    # 消息 — 已确认
    "im:message:readonly",
    # 妙记 — 已确认
    "minutes:minutes",
    "minutes:minutes.transcript:export",
    # 邮箱 — 以下 scope 全部在飞书开发者后台确认存在（2026-03-03）
    "mail:user_mailbox.folder:read",           # 查询邮箱文件夹
    "mail:user_mailbox.folder:write",          # 创建/修改/删除文件夹
    "mail:user_mailbox.message:readonly",      # 列出邮件
    "mail:user_mailbox.message:send",          # 发送邮件
    "mail:user_mailbox.message.address:read",  # 获取邮件地址字段
    "mail:user_mailbox.message.body:read",     # 获取邮件正文
    "mail:user_mailbox.message.subject:read",  # 获取邮件主题
    # 系统
    "offline_access",
])

# 文件 fallback 路径（仅 Redis 不可用时使用）
_TOKEN_FILE = Path(os.getenv("OAUTH_TOKEN_FILE", "/data/oauth_tokens.json"))

# 旧版 Redis key（用于迁移）
_LEGACY_REDIS_KEY = "feishu_oauth_tokens"


def _get_tenant_oauth_credentials(tenant_id: str) -> tuple[str, str, str]:
    """获取指定租户的 OAuth 凭证 (app_id, app_secret, redirect_uri)。

    优先从 tenant_registry 查找，找不到则回退到全局环境变量。
    """
    if tenant_id:
        from app.tenant.registry import tenant_registry
        tenant = tenant_registry.get(tenant_id)
        if tenant:
            return (
                tenant.app_id or settings.feishu.app_id,
                tenant.app_secret or settings.feishu.app_secret,
                tenant.oauth_redirect_uri or settings.feishu.oauth_redirect_uri,
            )
    return settings.feishu.app_id, settings.feishu.app_secret, settings.feishu.oauth_redirect_uri


@dataclass
class UserToken:
    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp
    open_id: str = ""
    name: str = ""
    tenant_id: str = ""  # 所属租户
    scope: str = ""  # OAuth 授权时获得的 scope


# open_id → UserToken
_tokens: dict[str, UserToken] = {}

# 防止并发刷新同一个 refresh_token（一次性，用两次就废）
_refresh_lock = threading.Lock()


# ── 持久化（Redis 按租户隔离）──

def _save_tokens() -> None:
    """保存 tokens：按 tenant_id 分组存到各自的 Redis key"""
    if not redis.available():
        _save_tokens_file()
        return

    # 按 tenant 分组
    by_tenant: dict[str, dict] = {}
    for oid, tok in _tokens.items():
        tid = tok.tenant_id or "default"
        by_tenant.setdefault(tid, {})[oid] = asdict(tok)

    for tid, data in by_tenant.items():
        json_str = _json.dumps(data, ensure_ascii=False)
        result = redis.execute("SET", f"{tid}:oauth_tokens", json_str)
        if result != "OK":
            logger.warning("oauth save failed for tenant %s", tid)

    logger.debug("tokens saved to redis: %d users across %d tenants",
                 len(_tokens), len(by_tenant))


def _save_tokens_file() -> None:
    """文件 fallback（Redis 不可用时）"""
    data = {oid: asdict(tok) for oid, tok in _tokens.items()}
    json_str = _json.dumps(data, ensure_ascii=False)
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(json_str)
        tmp.replace(_TOKEN_FILE)
        logger.debug("tokens saved to file: %d users", len(data))
    except Exception:
        logger.warning("failed to save tokens to %s", _TOKEN_FILE, exc_info=True)


def _load_tokens() -> None:
    """启动时恢复 tokens：从 Redis 按租户加载 + 迁移旧 key"""
    if not redis.available():
        logger.warning(
            "Redis NOT configured. Tokens will NOT persist across deploys! "
            "Set UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN."
        )
        _load_tokens_file()
        return

    if not redis.ping():
        logger.error("Redis PING failed, falling back to file")
        _load_tokens_file()
        return

    logger.info("Redis connected OK")
    loaded = 0

    # 1. 迁移旧版单 key（feishu_oauth_tokens → default:oauth_tokens）
    legacy_raw = redis.execute("GET", _LEGACY_REDIS_KEY)
    if legacy_raw:
        try:
            legacy_data = _json.loads(legacy_raw)
            for oid, tok_data in legacy_data.items():
                tok_data.setdefault("tenant_id", "default")
                _tokens[oid] = UserToken(**tok_data)
                loaded += 1
            logger.info("migrated %d tokens from legacy key", len(legacy_data))
            # 迁移完成后删除旧 key，避免重复迁移
            redis.execute("DEL", _LEGACY_REDIS_KEY)
        except Exception:
            logger.warning("failed to parse legacy tokens", exc_info=True)

    # 2. 从所有已注册租户加载
    from app.tenant.registry import tenant_registry
    for tid in tenant_registry.all_tenants():
        raw = redis.execute("GET", f"{tid}:oauth_tokens")
        if not raw:
            continue
        try:
            data = _json.loads(raw)
            for oid, tok_data in data.items():
                tok_data.setdefault("tenant_id", tid)
                _tokens[oid] = UserToken(**tok_data)
                loaded += 1
                # 诊断：检查 scope 字段
                scope_val = tok_data.get("scope", "MISSING")
                if not scope_val:
                    logger.warning("loaded token for %s has empty scope", oid[:15])
            logger.info("loaded %d tokens for tenant %s", len(data), tid)
        except Exception:
            logger.warning("failed to parse tokens for tenant %s", tid, exc_info=True)

    # 3. 也试 "default" key（如果不在 all_tenants 中）
    if "default" not in tenant_registry.all_tenants():
        raw = redis.execute("GET", "default:oauth_tokens")
        if raw:
            try:
                data = _json.loads(raw)
                for oid, tok_data in data.items():
                    if oid not in _tokens:
                        tok_data.setdefault("tenant_id", "default")
                        _tokens[oid] = UserToken(**tok_data)
                        loaded += 1
                logger.info("loaded %d tokens from default key", len(data))
            except Exception:
                logger.warning("failed to parse default tokens", exc_info=True)

    if loaded:
        # 迁移后立即保存（确保按新格式持久化）
        _save_tokens()

    logger.info("total authorized users: %d", len(_tokens))


def _load_tokens_file() -> None:
    """文件 fallback 加载"""
    if not _TOKEN_FILE.exists():
        logger.info("no saved tokens found, starting fresh")
        return
    try:
        raw = _json.loads(_TOKEN_FILE.read_text())
        for oid, tok_data in raw.items():
            tok_data.setdefault("tenant_id", "default")
            _tokens[oid] = UserToken(**tok_data)
        logger.info("loaded %d user tokens from file %s", len(raw), _TOKEN_FILE)
    except Exception:
        logger.warning("failed to load tokens from %s", _TOKEN_FILE, exc_info=True)


_startup_done = False


def init_tokens() -> None:
    """由 FastAPI startup 调用，此时 logging 已初始化"""
    global _startup_done
    if _startup_done:
        return
    _startup_done = True
    _load_tokens()


# ── State 编解码 ──

def _encode_state(open_id: str, tenant_id: str = "") -> str:
    """把 open_id + tenant_id 编码到 state 里，即使进程重启也不丢"""
    nonce = secrets.token_urlsafe(8)
    payload = _json.dumps({"oid": open_id, "tid": tenant_id, "n": nonce})
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_state(state: str) -> tuple[str, str]:
    """从 state 解码 (open_id, tenant_id)"""
    try:
        # 补齐 base64 padding
        padding = 4 - len(state) % 4
        if padding != 4:
            state += "=" * padding
        payload = base64.urlsafe_b64decode(state).decode()
        data = _json.loads(payload)
        return data.get("oid", ""), data.get("tid", "")
    except Exception:
        logger.warning("failed to decode state: %s", state[:30])
        return "", ""


def build_auth_url(sender_open_id: str) -> str:
    """为某个用户生成 OAuth 授权链接（使用当前租户的凭证）

    redirect_uri 自动转换为 per-tenant 路径：
      /oauth/callback → /oauth/{tenant_id}/callback
    确保 nginx 能路由到该租户的容器。
    """
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()

    state = _encode_state(sender_open_id, tenant.tenant_id)

    app_id = tenant.app_id or settings.feishu.app_id
    redirect_uri = tenant.oauth_redirect_uri or settings.feishu.oauth_redirect_uri

    # 自动转换为 per-tenant callback 路径（多租户容器路由需要）
    # /oauth/callback → /oauth/{tenant_id}/callback
    tid = tenant.tenant_id
    if tid and redirect_uri and f"/oauth/{tid}/callback" not in redirect_uri:
        redirect_uri = redirect_uri.replace(
            "/oauth/callback", f"/oauth/{tid}/callback",
        )
        logger.info("OAuth redirect_uri rewritten for tenant %s: %s", tid, redirect_uri)

    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "scope": _SCOPES,
    }
    return f"https://accounts.feishu.cn/open-apis/authen/v1/authorize?{urlencode(params)}"


async def exchange_code(code: str, state: str) -> dict | None:
    """用授权码换取 token，返回 {"name": ..., "scope": ...} 或 None"""
    sender_open_id, tenant_id = _decode_state(state)
    logger.info("OAuth callback: state decoded sender_open_id=%s tenant_id=%s", sender_open_id, tenant_id)

    app_id, app_secret, redirect_uri = _get_tenant_oauth_credentials(tenant_id)

    # redirect_uri 必须和 build_auth_url 中的完全一致，否则飞书拒绝
    if tenant_id and redirect_uri and f"/oauth/{tenant_id}/callback" not in redirect_uri:
        redirect_uri = redirect_uri.replace(
            "/oauth/callback", f"/oauth/{tenant_id}/callback",
        )

    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        resp = await client.post(_TOKEN_URL, json={
            "client_id": app_id,
            "client_secret": app_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
        data = resp.json()

    granted_scope = data.get("scope", "")
    # 飞书可能返回 scope 为空或不包含我们请求的全部权限
    logger.info("OAuth token response: keys=%s scope='%s' full_data=%s", 
                list(data.keys()), granted_scope, str(data)[:500])

    access_token = data.get("access_token", "")
    if not access_token:
        logger.error("OAuth token exchange failed: %s", data)
        return None

    refresh_token = data.get("refresh_token", "")
    expires_in = data.get("expires_in", 7200)

    # 获取用户信息（确认 open_id）
    open_id = sender_open_id
    name = ""
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.get(
                _USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_data = resp.json()

        user_info = user_data.get("data", {})
        if user_info.get("open_id"):
            open_id = user_info["open_id"]
        name = user_info.get("name", "")
        logger.info("OAuth user_info: open_id=%s name=%s", open_id, name)
    except Exception:
        logger.exception("Failed to fetch user info, using sender_open_id=%s", sender_open_id)

    if not open_id:
        logger.error("OAuth: no open_id available (state decode failed and user_info failed)")
        return None

    # tenant_id 已从 state 中解码，无需依赖请求上下文
    if not tenant_id:
        tenant_id = "default"

    token = UserToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + expires_in - 300,  # 提前 5 分钟刷新
        open_id=open_id,
        name=name,
        tenant_id=tenant_id,
        scope=granted_scope,
    )
    _tokens[open_id] = token
    clear_dead(open_id)
    _save_tokens()

    # 清除日历等缓存，确保新 token 立即生效（不会被负缓存挡住）
    try:
        from app.tools.calendar_ops import invalidate_calendar_cache
        invalidate_calendar_cache()
    except Exception:
        pass  # calendar_ops 可能未加载

    # 验证保存的 token 包含 scope
    saved_token = _tokens.get(open_id)
    saved_scope = saved_token.scope if saved_token else "N/A"
    logger.info("OAuth token stored for user %s (%s), tenant=%s, granted_scope='%s', saved_scope='%s', total authorized: %d",
                name, open_id, tenant_id, granted_scope, saved_scope, len(_tokens))
    return {"name": name or open_id, "scope": granted_scope}


async def _refresh_token(token: UserToken) -> bool:
    """刷新过期的 token"""
    if not token.refresh_token:
        return False

    app_id, app_secret, _ = _get_tenant_oauth_credentials(token.tenant_id)

    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.post(_TOKEN_URL, json={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
            })
            data = resp.json()

        new_access = data.get("access_token", "")
        if not new_access:
            logger.error("OAuth refresh failed: %s", data)
            return False

        token.access_token = new_access
        token.refresh_token = data.get("refresh_token", token.refresh_token)
        token.expires_at = time.time() + data.get("expires_in", 7200) - 300
        _save_tokens()
        logger.info("OAuth token refreshed for %s", token.open_id)
        return True
    except Exception:
        logger.exception("OAuth refresh error for %s", token.open_id)
        return False


def get_user_token_sync(open_id: str) -> str:
    """同步获取用户的 access_token（供工具层使用）

    如果 token 过期且有 refresh_token，尝试同步刷新。
    加锁防止并发刷新（飞书 refresh_token 是一次性的）。
    返回空字符串表示用户未授权或 token 已失效。
    """
    token = _tokens.get(open_id)
    if not token:
        return ""

    if time.time() < token.expires_at:
        return token.access_token

    if not token.refresh_token:
        logger.warning("user %s token expired, no refresh_token", open_id[:15])
        return ""

    # 用锁保证同一时刻只有一个线程刷新（避免 refresh_token 被重复消费）
    app_id, app_secret, _ = _get_tenant_oauth_credentials(token.tenant_id)

    with _refresh_lock:
        # 双重检查：可能另一个线程已经刷新成功了
        if time.time() < token.expires_at:
            return token.access_token

        try:
            with httpx.Client(timeout=15, trust_env=False) as client:
                resp = client.post(_TOKEN_URL, json={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                })
                data = resp.json()

            new_access = data.get("access_token", "")
            if new_access:
                token.access_token = new_access
                token.refresh_token = data.get("refresh_token", token.refresh_token)
                token.expires_at = time.time() + data.get("expires_in", 7200) - 300
                _save_tokens()
                logger.info("token refreshed for %s", open_id[:15])
                return token.access_token

            # 刷新失败
            err_code = data.get("code", 0)
            err_desc = data.get("error_description", "")
            logger.error("token refresh failed for %s: code=%s desc=%s",
                         open_id[:15], err_code, err_desc)

            # refresh_token 被吊销 / 已过期 / client_id 不匹配 / 缺少参数 → 清除死 token，用户必须重新 /auth
            if err_code in (20064, 20035, 20024, 20063) or "revoked" in err_desc.lower():
                logger.warning("refresh_token revoked for %s — clearing token, user must /auth",
                               open_id[:15])
                _tokens.pop(open_id, None)
                _mark_dead(open_id)
                _save_tokens()

            return ""
        except Exception:
            logger.exception("token refresh error for %s", open_id[:15])
            return ""


def is_authorized(open_id: str) -> bool:
    """用户是否已授权"""
    return open_id in _tokens


def clear_user_token(open_id: str) -> bool:
    """清除用户的 token（用于重新授权）"""
    global _tokens
    if open_id in _tokens:
        del _tokens[open_id]
        _save_tokens()
        logger.info("cleared token for user %s", open_id[:15])
        return True
    return False


def needs_reauth(open_id: str) -> bool:
    """用户的 token 是否已失效（曾经授权过，但 refresh 失败被清除了）

    与 is_authorized 的区别：
    - is_authorized=False + needs_reauth=True → 曾授权但 token 死了，需要重新 /auth
    - is_authorized=False + needs_reauth=False → 从未授权过
    """
    return open_id in _dead_tokens


# 记录哪些用户的 token 死了（refresh 失败被清除）
_dead_tokens: set[str] = set()


def _mark_dead(open_id: str) -> None:
    """标记用户 token 已死，需要重新授权"""
    _dead_tokens.add(open_id)


def clear_dead(open_id: str) -> None:
    """用户重新授权后清除死亡标记"""
    _dead_tokens.discard(open_id)


def get_token_info(open_id: str) -> dict:
    """获取用户 token 信息（调试用）"""
    tok = _tokens.get(open_id)
    if not tok:
        return {}
    return {
        "open_id": tok.open_id,
        "name": tok.name,
        "has_token": bool(tok.access_token),
        "has_refresh": bool(tok.refresh_token),
        "expires_at": tok.expires_at,
        "tenant_id": tok.tenant_id,
        "scope": tok.scope,
    }


# ── 后台主动刷新（在 access_token 过期前刷新，保持 refresh 链不断）──

_REFRESH_INTERVAL = 1800  # 每 30 分钟检查一次
_REFRESH_BUFFER = 600     # 提前 10 分钟刷新

_bg_thread: threading.Thread | None = None


def _background_refresh_loop() -> None:
    """后台线程：定期检查并刷新即将过期的 token"""
    while True:
        time.sleep(_REFRESH_INTERVAL)
        now = time.time()
        # 快照 token 列表，避免遍历时修改
        for open_id, token in list(_tokens.items()):
            remaining = token.expires_at - now
            if remaining > _REFRESH_BUFFER or not token.refresh_token:
                continue
            # 即将过期，主动刷新
            logger.info("proactive refresh for %s (expires in %.0fs)", open_id[:15], remaining)
            # 复用 get_user_token_sync 的锁逻辑
            result = get_user_token_sync(open_id)
            if result:
                logger.info("proactive refresh succeeded for %s", open_id[:15])
            else:
                logger.warning("proactive refresh failed for %s", open_id[:15])


def start_background_refresh() -> None:
    """启动后台刷新线程（由 main.py startup 调用）"""
    global _bg_thread
    if _bg_thread is not None:
        return
    _bg_thread = threading.Thread(target=_background_refresh_loop, daemon=True, name="token-refresh")
    _bg_thread.start()
    logger.info("background token refresh started (interval=%ds, buffer=%ds)",
                _REFRESH_INTERVAL, _REFRESH_BUFFER)
