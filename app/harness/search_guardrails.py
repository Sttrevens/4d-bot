"""Guardrails for query-topic alignment in follow-up turns."""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import unquote

_SPACE_RE = re.compile(r"\s+")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

_FOCUS_STOPWORDS = {
    "the",
    "this",
    "that",
    "these",
    "those",
    "and",
    "for",
    "with",
    "about",
    "from",
    "into",
    "continue",
    "search",
    "result",
    "results",
    "keyword",
    "site",
    "https",
    "http",
    "www",
    "com",
    "cn",
    "org",
    "net",
    "你",
    "我",
    "他",
    "她",
    "它",
    "我们",
    "你们",
    "他们",
    "这个",
    "那个",
    "这些",
    "那些",
    "这里",
    "那里",
    "刚才",
    "继续",
    "再试试",
    "搜索",
    "一下",
    "一下子",
    "问题",
    "内容",
    "信息",
    "资料",
    "看看",
    "查查",
}

_FOCUS_FILLER_WORDS = {
    "game",
    "games",
    "info",
    "steam",
    "游戏",
    "信息",
    "资料",
    "介绍",
    "官网",
}


def _extract_focus_tokens(text: str) -> list[str]:
    source = unquote((text or "").lower())
    if not source:
        return []
    source = _SPACE_RE.sub(" ", source)
    ascii_tokens = [
        t for t in _ASCII_TOKEN_RE.findall(source)
        if len(t) >= 3 and t not in _FOCUS_FILLER_WORDS and t not in _FOCUS_STOPWORDS
    ]
    cjk_tokens = [
        t for t in _CJK_TOKEN_RE.findall(source)
        if t not in _FOCUS_FILLER_WORDS and t not in _FOCUS_STOPWORDS
    ]
    return ascii_tokens + cjk_tokens


def extract_focus_terms(*texts: str, max_terms: int = 24) -> tuple[str, ...]:
    """Extract stable topic terms from user/context text."""
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(_extract_focus_tokens(text))
    if not counter:
        return ()
    ranked = sorted(
        counter.items(),
        key=lambda item: (-item[1], -len(item[0]), item[0]),
    )
    return tuple(term for term, _ in ranked[:max_terms])


def is_query_off_topic(query: str, focus_terms: list[str] | tuple[str, ...]) -> bool:
    """Return True when a search query has zero lexical overlap with focus terms."""
    if not query or not focus_terms:
        return False
    q_tokens = set(_extract_focus_tokens(query))
    if not q_tokens:
        return False
    focus_set = set(focus_terms)
    if q_tokens & focus_set:
        return False

    q_text = unquote(query).lower()
    for term in focus_set:
        if len(term) >= 4 and term in q_text:
            return False
    return True
