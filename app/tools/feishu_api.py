"""飞书 API 同步客户端

供工具层使用的同步 HTTP 封装（与 github_api.py 同级）。
Token 按租户自动获取和缓存。

支持两种身份：
- tenant_access_token（默认，bot 身份）
- user_access_token（用户授权后，用户身份 — 用于日历等）
"""

from __future__ import annotations

import logging
import time
import contextvars

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_URL = f"{_API_BASE}/auth/v3/tenant_access_token/internal"

# 按 app_id 缓存 tenant token: {app_id: (token, expire_time)}
_token_cache: dict[str, tuple[str, float]] = {}

# 当前请求的用户 open_id（由 agent 在执行工具前设置）
_current_user_open_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_user_open_id", default=""
)


def set_current_user(open_id: str) -> None:
    """设置当前用户 open_id（agent 层调用）"""
    _current_user_open_id.set(open_id)


def has_user_token() -> bool:
    """当前用户是否已有 OAuth user_access_token"""
    open_id = _current_user_open_id.get("")
    if not open_id:
        return False
    from app.services.oauth_store import get_user_token_sync
    return bool(get_user_token_sync(open_id))


def _get_credentials() -> tuple[str, str]:
    """从当前 tenant 上下文获取凭证"""
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    return tenant.app_id, tenant.app_secret


def _get_token() -> str:
    app_id, app_secret = _get_credentials()

    if not app_id or not app_secret:
        logger.debug("skip feishu token: tenant has no feishu app_id/app_secret (platform may be wecom)")
        return ""

    cached = _token_cache.get(app_id)
    if cached and time.time() < cached[1]:
        return cached[0]

    with httpx.Client(timeout=10, trust_env=False) as client:
        resp = client.post(
            _TOKEN_URL,
            json={"app_id": app_id, "app_secret": app_secret},
        )
        data = resp.json()

    if data.get("code") != 0:
        logger.error("failed to get feishu token for app=%s: %s", app_id, data)
        return ""

    token = data["tenant_access_token"]
    expire = time.time() + data.get("expire", 7200) - 300
    _token_cache[app_id] = (token, expire)
    return token


def _headers(use_user_token: bool = False) -> tuple[dict, str]:
    """获取请求头。use_user_token=True 时优先使用用户 token。
    返回 (headers, token_type) 方便调试日志。
    """
    if use_user_token:
        open_id = _current_user_open_id.get("")
        if open_id:
            from app.services.oauth_store import get_user_token_sync
            user_token = get_user_token_sync(open_id)
            if user_token:
                return {"Authorization": f"Bearer {user_token}"}, "user"
            logger.warning("user %s requested user_token but got none, falling back to tenant_token", open_id[:15])
    token = _get_token()
    if not token:
        return {}, "none"
    return {"Authorization": f"Bearer {token}"}, "tenant"


def _sanitize_error(code: int, msg: str, *, path: str = "", token_type: str = "") -> str:
    """将飞书 API 错误转为对 LLM 有行动指引的消息。

    格式: [错误类型] 描述。动作建议。
    LLM 看到这个就知道该做什么，不需要额外的 system prompt 规则。
    """
    logger.warning("feishu API error: path=%s token=%s code=%d msg=%s",
                    path, token_type, code, msg[:500])

    # 权限/授权不足
    auth_codes = {99991668, 99991672, 99991663, 99991664, 99991679}
    if code in auth_codes or any(kw in msg.lower() for kw in ("token", "login", "auth", "identity", "scope")):
        if token_type == "tenant" and code == 99991672:
            return (
                f"[ERROR] 应用权限不足(code={code})。"
                "请联系管理员在飞书后台为应用开通相关权限（如日历、妙记、通讯录等）。"
                "这不是代码bug。"
            )
        return (
            f"[ERROR] 权限不足(code={code})。"
            "用户未授权此操作，此功能暂不可用。"
            "如果用户明确要求使用此功能，建议发 /auth 重新授权。"
            "不要主动提醒授权问题，继续回答用户的其他问题。"
            "这不是代码bug，不需要自我修复。"
        )
    # 资源不存在
    if code in (2091002, 2091004) or "not found" in msg.lower():
        return f"[ERROR] 资源不存在(code={code})。确认ID是否正确，不要重试。不是代码bug。"
    # 访问被拒（bot 没被加入群/妙记/知识库等）
    if code in (2091005, 230002, 1770032):
        return f"[ERROR] 无权访问(code={code})。Bot没有访问此资源的权限（如未被加入群组或知识库），请将 Bot 添加为协作者。不是代码bug。"
    # 日历特有
    if code == 191002:
        return "[ERROR] 没有日历访问权限(code=191002)，已通知管理员处理。"
    # 参数校验失败
    if code == 99992402 or "validation" in msg.lower():
        return f"[ERROR] 参数错误(code={code})。检查传入的ID/参数格式，修正后重试一次。"
    # 通用
    return f"[ERROR] code={code}: {msg}"


def _parse_response(resp: httpx.Response, *, path: str, tok_type: str) -> dict | str:
    """统一解析飞书 API 响应，处理非 JSON 和错误码"""
    # 某些接口（如妙记转录）HTTP 200 直接返回纯文本，不是 JSON
    content_type = resp.headers.get("content-type", "")
    if resp.status_code == 200 and "application/json" not in content_type.lower():
        return resp.text
    try:
        data = resp.json()
    except Exception:
        # 即使没标 content-type，200 的纯文本也应视为成功
        if resp.status_code == 200:
            return resp.text
        logger.warning("feishu non-JSON response: path=%s status=%d body=%s",
                       path, resp.status_code, resp.text[:300])
        return f"[ERROR] 飞书API返回异常(HTTP {resp.status_code})"
    if data.get("code") != 0:
        return _sanitize_error(data.get("code", 0), data.get("msg", resp.text[:300]),
                               path=path, token_type=tok_type)
    return data


def feishu_get(
    path: str, params: dict | None = None, use_user_token: bool = False,
) -> dict | str:
    """同步 GET"""
    hdrs, tok_type = _headers(use_user_token)
    if tok_type == "none":
        return "[ERROR] 飞书 token 不可用（app_id/app_secret 未配置或获取失败）"
    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.get(
            f"{_API_BASE}{path}",
            headers=hdrs,
            params=params,
        )
    return _parse_response(resp, path=path, tok_type=tok_type)


def feishu_post(
    path: str, json: dict, params: dict | None = None,
    use_user_token: bool = False,
) -> dict | str:
    """同步 POST"""
    hdrs, tok_type = _headers(use_user_token)
    if tok_type == "none":
        return "[ERROR] 飞书 token 不可用（app_id/app_secret 未配置或获取失败）"
    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.post(
            f"{_API_BASE}{path}",
            headers=hdrs,
            json=json,
            params=params,
        )
    return _parse_response(resp, path=path, tok_type=tok_type)


def feishu_patch(
    path: str, json: dict, params: dict | None = None,
    use_user_token: bool = False,
) -> dict | str:
    """同步 PATCH"""
    hdrs, tok_type = _headers(use_user_token)
    if tok_type == "none":
        return "[ERROR] 飞书 token 不可用（app_id/app_secret 未配置或获取失败）"
    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.patch(
            f"{_API_BASE}{path}",
            headers=hdrs,
            json=json,
            params=params,
        )
    return _parse_response(resp, path=path, tok_type=tok_type)


def feishu_put(
    path: str, json: dict, params: dict | None = None,
    use_user_token: bool = False,
) -> dict | str:
    """同步 PUT"""
    hdrs, tok_type = _headers(use_user_token)
    if tok_type == "none":
        return "[ERROR] 飞书 token 不可用（app_id/app_secret 未配置或获取失败）"
    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.put(
            f"{_API_BASE}{path}",
            headers=hdrs,
            json=json,
            params=params,
        )
    return _parse_response(resp, path=path, tok_type=tok_type)


def feishu_delete(
    path: str, json: dict | None = None, params: dict | None = None,
    use_user_token: bool = False,
) -> dict | str:
    """同步 DELETE（支持可选 request body，如 batch_delete 接口需要）"""
    hdrs, tok_type = _headers(use_user_token)
    if tok_type == "none":
        return "[ERROR] 飞书 token 不可用（app_id/app_secret 未配置或获取失败）"
    with httpx.Client(timeout=30, trust_env=False) as client:
        resp = client.request(
            "DELETE",
            f"{_API_BASE}{path}",
            headers=hdrs,
            json=json,
            params=params,
        )
    return _parse_response(resp, path=path, tok_type=tok_type)
