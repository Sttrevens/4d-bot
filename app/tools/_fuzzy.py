"""模糊搜索工具函数

供各工具模块共享的模糊匹配能力。
"""

from __future__ import annotations

import re

# 去除干扰字符的正则
_STRIP_RE = re.compile(r"[\s\-_./·•，。：；！？、（）【】]")


def fuzzy_match(text: str, keyword: str) -> bool:
    """模糊匹配：判断 text 是否匹配 keyword

    匹配策略（任一命中即为匹配）：
    1. 子串匹配 — keyword 整体出现在 text 中
    2. 多词匹配 — keyword 按空格拆分后每个词都出现在 text 中
    3. 紧凑匹配 — 去除标点/空格/连字符后做子串匹配（"FL8" 匹配 "F-L8"）
    """
    if not keyword:
        return True
    text_lower = text.lower()
    kw_lower = keyword.lower().strip()
    if not kw_lower:
        return True

    # 1. 直接子串
    if kw_lower in text_lower:
        return True

    # 2. 多词：所有 token 都命中
    tokens = kw_lower.split()
    if len(tokens) > 1 and all(t in text_lower for t in tokens):
        return True

    # 3. 紧凑匹配：去除标点/空格后比较
    text_clean = _STRIP_RE.sub("", text_lower)
    kw_clean = _STRIP_RE.sub("", kw_lower)
    if kw_clean and kw_clean in text_clean:
        return True

    return False


def fuzzy_filter(items: list[dict], keyword: str, fields: list[str]) -> list[dict]:
    """对字典列表做模糊过滤

    items:   [{"summary": "...", "name": "..."}, ...]
    keyword: 用户输入的关键词
    fields:  要搜索的字段名列表，如 ["summary", "name"]

    任一字段匹配即保留该条目。
    """
    if not keyword:
        return items
    return [
        item for item in items
        if any(fuzzy_match(str(item.get(f, "")), keyword) for f in fields)
    ]
