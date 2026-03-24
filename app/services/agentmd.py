"""AGENT.md 项目上下文加载器

类似 Claude Code 的 CLAUDE.md 机制：从目标 GitHub 仓库的根目录加载 AGENT.md，
作为 bot 理解项目结构、编码规范和架构约定的基础。

AGENT.md 是 bot 对项目的"第一印象"——
好的 AGENT.md 让 bot 知道项目的全貌，修改代码前自然会考虑影响范围。

为什么叫 AGENT.md：
  CLAUDE.md 是给 Claude Code 用的；AGENT.md 是给所有 AI agent 用的通用约定。
  任何关联了代码仓库的 bot，启动时都会加载 AGENT.md 作为项目理解的基础。

缓存策略：Redis 缓存 30 分钟，避免每次请求都调 GitHub API。
"""

from __future__ import annotations

import base64
import logging

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "agentmd"
_CACHE_TTL = 1800  # 30 分钟
_EMPTY_SENTINEL = "__AGENTMD_EMPTY__"


def _cache_key(tenant_id: str) -> str:
    return f"{_CACHE_PREFIX}:{tenant_id}"


def _load_from_github(repo_owner: str, repo_name: str, path: str) -> str | None:
    """从 GitHub API 加载 AGENT.md 内容。"""
    try:
        from app.tools.github_api import gh_get
        result = gh_get(f"/repos/{repo_owner}/{repo_name}/contents/{path}")
        if isinstance(result, dict) and "content" in result:
            content = base64.b64decode(result["content"]).decode("utf-8")
            return content
        logger.debug("AGENT.md not found: %s/%s/%s", repo_owner, repo_name, path)
        return None
    except Exception:
        logger.debug("AGENT.md load from GitHub failed", exc_info=True)
        return None


def _load_from_cache(tenant_id: str) -> str | None:
    try:
        from app.services import redis_client as redis
        if not redis.available():
            return None
        raw = redis.execute("GET", _cache_key(tenant_id))
        if raw is None:
            return None
        return raw if isinstance(raw, str) else raw.decode("utf-8")
    except Exception:
        return None


def _save_to_cache(tenant_id: str, content: str) -> None:
    try:
        from app.services import redis_client as redis
        if redis.available():
            redis.execute("SET", _cache_key(tenant_id), content, "EX", str(_CACHE_TTL))
    except Exception:
        logger.debug("AGENT.md cache write failed", exc_info=True)


def get_agentmd_content() -> str | None:
    """获取当前租户关联仓库的 AGENT.md 内容。

    优先从 Redis 缓存读取，缓存未命中则从 GitHub API 加载。
    返回 None 表示未配置 GitHub 仓库或 AGENT.md 不存在。
    """
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
    except Exception:
        return None

    if not getattr(tenant, "agentmd_enabled", True):
        return None

    tid = tenant.tenant_id

    cached = _load_from_cache(tid)
    if cached is not None:
        return None if cached == _EMPTY_SENTINEL else cached

    repo_owner = tenant.github_repo_owner
    repo_name = tenant.github_repo_name

    if not repo_owner or not repo_name:
        _save_to_cache(tid, _EMPTY_SENTINEL)
        return None

    path = getattr(tenant, "agentmd_path", "AGENT.md") or "AGENT.md"
    content = _load_from_github(repo_owner, repo_name, path)

    if content:
        _save_to_cache(tid, content)
        logger.info("AGENT.md loaded: %s/%s/%s (%d chars)",
                     repo_owner, repo_name, path, len(content))
        return content
    else:
        _save_to_cache(tid, _EMPTY_SENTINEL)
        return None


def build_agentmd_prompt(content: str, max_chars: int = 4000) -> str:
    """将 AGENT.md 内容格式化为 system prompt 注入片段。

    AGENT.md 在 system prompt 中的地位：
    - 比记忆更重要（记忆是过去的经验，AGENT.md 是项目的真相）
    - 比通用指令更具体（通用指令说"写好代码"，AGENT.md 说"这个项目怎么写"）
    - bot 的每一次代码修改都应该参照 AGENT.md 的描述
    """
    if not content:
        return ""

    truncated = content[:max_chars]
    if len(content) > max_chars:
        truncated += "\n... (AGENT.md 内容过长，已截断)"

    return (
        "\n\n── AGENT.md（项目上下文 · 你最重要的参考）──\n"
        f"{truncated}\n"
        "── AGENT.md 结束 ──\n"
        "\n"
        "上面的 AGENT.md 是你理解这个项目的基础。\n"
        "修改代码时，确保你的改动符合 AGENT.md 中描述的架构和约定。\n"
        "如果 AGENT.md 描述了模块之间的关系，修改某个模块前请先了解它的上下游。"
    )


def invalidate_cache(tenant_id: str = "") -> bool:
    """手动清除 AGENT.md 缓存。仓库中 AGENT.md 更新后可调用此函数刷新。"""
    try:
        if not tenant_id:
            from app.tenant.context import get_current_tenant
            tenant_id = get_current_tenant().tenant_id
        from app.services import redis_client as redis
        if redis.available():
            redis.execute("DEL", _cache_key(tenant_id))
            return True
    except Exception:
        pass
    return False
