"""GitHub API 公共客户端

所有 GitHub 操作共用的 HTTP 请求封装。
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

# GitHub API 需要代理（中国大陆），但 connect timeout 要短，
# 代理挂了时快速失败（5s），不要 hang 30s 堆积连接
_GH_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"


def _get_github_config() -> tuple[str, str, str]:
    """从 tenant 上下文获取 GitHub 配置，回退到全局 settings"""
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    token = tenant.github_token or settings.github.token
    owner = tenant.github_repo_owner or settings.github.repo_owner
    name = tenant.github_repo_name or settings.github.repo_name
    return token, owner, name


def _headers() -> dict:
    token, _, _ = _get_github_config()
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def _repo_url(path: str = "") -> str:
    _, owner, name = _get_github_config()
    return f"{_API_BASE}/repos/{owner}/{name}{path}"


def gh_get(path: str, params: dict | None = None) -> dict | list | str:
    """同步 GET 请求"""
    with httpx.Client(timeout=_GH_TIMEOUT) as client:
        resp = client.get(_repo_url(path), headers=_headers(), params=params)
    if resp.status_code >= 400:
        return f"[ERROR] {resp.status_code}: {resp.text[:500]}"
    return resp.json()


def gh_post(path: str, json: dict) -> dict | str:
    """同步 POST 请求"""
    with httpx.Client(timeout=_GH_TIMEOUT) as client:
        resp = client.post(_repo_url(path), headers=_headers(), json=json)
    if resp.status_code >= 400:
        return f"[ERROR] {resp.status_code}: {resp.text[:500]}"
    return resp.json()


def gh_put(path: str, json: dict) -> dict | str:
    """同步 PUT 请求"""
    with httpx.Client(timeout=_GH_TIMEOUT) as client:
        resp = client.put(_repo_url(path), headers=_headers(), json=json)
    if resp.status_code >= 400:
        return f"[ERROR] {resp.status_code}: {resp.text[:500]}"
    return resp.json()


def gh_patch(path: str, json: dict) -> dict | str:
    """同步 PATCH 请求"""
    with httpx.Client(timeout=_GH_TIMEOUT) as client:
        resp = client.patch(_repo_url(path), headers=_headers(), json=json)
    if resp.status_code >= 400:
        return f"[ERROR] {resp.status_code}: {resp.text[:500]}"
    return resp.json()


def gh_delete(path: str) -> str:
    """同步 DELETE 请求"""
    with httpx.Client(timeout=_GH_TIMEOUT) as client:
        resp = client.delete(_repo_url(path), headers=_headers())
    if resp.status_code >= 400:
        return f"[ERROR] {resp.status_code}: {resp.text[:500]}"
    return "ok"
