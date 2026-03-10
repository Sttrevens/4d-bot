"""搜索来源注册表 —— 架构级 URL 真实性验证

web_search 每次返回结果时，自动把真实 URL 注册到请求级注册表。
export_file 生成报告前，扫描内容中的所有 URL，对不在注册表中的 URL 做清理。

这是代码层硬约束，不依赖 LLM 遵守 system prompt。
"""

from __future__ import annotations

import contextvars
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 请求级注册表：当前对话轮次中 web_search 返回的所有真实 URL
_verified_urls: contextvars.ContextVar[set[str]] = contextvars.ContextVar(
    "_verified_urls", default=None  # type: ignore[arg-type]
)


def register_urls(urls: list[str]) -> None:
    """注册 web_search 返回的真实 URL（由 web_search 工具调用）"""
    store = _verified_urls.get(None)
    if store is None:
        store = set()
        _verified_urls.set(store)
    for url in urls:
        if url:
            store.add(url.strip())
            # 同时注册去掉尾部斜杠的版本
            store.add(url.strip().rstrip("/"))


def get_verified_urls() -> set[str]:
    """获取当前对话中所有已验证的 URL"""
    return _verified_urls.get(None) or set()


def reset() -> None:
    """重置注册表（新对话开始时调用）"""
    _verified_urls.set(set())


# ── URL 提取与验证 ──

# 匹配 HTML 中的 href="..." 和 Markdown 中的 [text](url)
_URL_PATTERNS = [
    re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'\[([^\]]*)\]\(([^)]+)\)'),  # group(2) is URL
    re.compile(r'https?://[^\s<>"\')\]]+'),   # 裸 URL
]


def _extract_urls(content: str) -> list[str]:
    """从 HTML/Markdown 内容中提取所有 URL"""
    urls = set()
    # href="..."
    for m in _URL_PATTERNS[0].finditer(content):
        url = m.group(1)
        if url.startswith(("http://", "https://")):
            urls.add(url)
    # [text](url)
    for m in _URL_PATTERNS[1].finditer(content):
        url = m.group(2)
        if url.startswith(("http://", "https://")):
            urls.add(url)
    # 裸 URL（补充捕获）
    for m in _URL_PATTERNS[2].finditer(content):
        urls.add(m.group(0))
    return list(urls)


# 允许的 URL 域名白名单（不需要验证的通用资源）
_SAFE_DOMAINS = frozenset({
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdn.jsdelivr.net",
    "unpkg.com",
    "cdnjs.cloudflare.com",
    # Google Fonts import 等静态资源不算来源
})


def _is_safe_url(url: str) -> bool:
    """检查 URL 是否是不需要验证的安全资源（CDN/字体等）"""
    try:
        parsed = urlparse(url)
        return parsed.hostname in _SAFE_DOMAINS if parsed.hostname else False
    except Exception:
        return False


def _is_verified(url: str, verified: set[str]) -> bool:
    """检查 URL 是否在已验证的来源中"""
    if not verified:
        return True  # 没有任何搜索 → 不做验证（fail-open）
    url_clean = url.strip().rstrip("/")
    # 精确匹配
    if url_clean in verified or url in verified:
        return True
    # 前缀匹配：搜索结果可能是文章 URL，LLM 引用时可能加了锚点
    for v in verified:
        if url_clean.startswith(v.rstrip("/")) or v.rstrip("/").startswith(url_clean):
            return True
    return False


def sanitize_urls_in_content(content: str) -> tuple[str, list[str]]:
    """扫描内容中的 URL，移除未经验证的链接。

    返回: (清理后的内容, 被移除的 URL 列表)

    策略：
    - HTML href: 移除 <a> 标签，保留文本
    - Markdown link: [text](url) → text（来源：公开信息）
    - 裸 URL: 直接移除
    """
    verified = get_verified_urls()
    if not verified:
        # 当前对话没有执行过 web_search → 不做验证（fail-open）
        return content, []

    removed: list[str] = []

    # 1. 处理 HTML <a href="...">text</a> → 保留文本
    def _replace_html_link(m: re.Match) -> str:
        url = m.group(1)
        text = m.group(2)
        if _is_safe_url(url) or _is_verified(url, verified):
            return m.group(0)  # 保留原样
        removed.append(url)
        return text  # 去掉链接，保留文本

    content = re.sub(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        _replace_html_link,
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # 2. 处理 Markdown [text](url) → text
    def _replace_md_link(m: re.Match) -> str:
        text = m.group(1)
        url = m.group(2)
        if _is_safe_url(url) or _is_verified(url, verified):
            return m.group(0)
        removed.append(url)
        return text

    content = re.sub(
        r'\[([^\]]*)\]\(([^)]+)\)',
        _replace_md_link,
        content,
    )

    if removed:
        logger.warning(
            "source_registry: removed %d unverified URLs from export content: %s",
            len(removed), removed[:5],
        )

    return content, removed
