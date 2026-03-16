"""Markdown-aware 消息分块

借鉴 OpenClaw 的 outbound message chunking：
  不同 channel 有不同的消息长度限制，
  简单按字符数 hard cut 会打断代码块、表格、列表。

OpenClaw 每个 channel 可以配置 chunkerMode（text/markdown）
和自定义 chunker 函数。我们实现一个通用的 markdown-aware chunker。

分块策略（优先级从高到低）：
  1. 尽量在段落边界分块（空行）
  2. 其次在代码块结束处分块（```）
  3. 再次在列表项边界分块
  4. 最后在句号/感叹号处分块
  5. 万不得已在字符数处硬切
"""

from __future__ import annotations

import re


def chunk_markdown(text: str, limit: int = 4000) -> list[str]:
    """将文本按 markdown 结构分块。

    Args:
        text: 原始文本（可能含 markdown）
        limit: 每块最大字符数

    Returns:
        分块后的文本列表。如果 text 不超过 limit，返回单元素列表。
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # 在 limit 范围内找最佳分割点
        cut = _find_best_break(remaining, limit)
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n")

    return chunks or [text]


def _find_best_break(text: str, limit: int) -> int:
    """在 text[:limit] 中找最佳分割点。"""
    window = text[:limit]

    # 策略 1: 段落边界（空行）
    pos = _rfind_paragraph_break(window)
    if pos > limit * 0.3:  # 至少用掉 30% 空间
        return pos

    # 策略 2: 代码块结束（```）
    pos = _rfind_code_block_end(window)
    if pos > limit * 0.3:
        return pos

    # 策略 3: 列表项边界
    pos = _rfind_list_break(window)
    if pos > limit * 0.3:
        return pos

    # 策略 4: 句末标点
    pos = _rfind_sentence_end(window)
    if pos > limit * 0.3:
        return pos

    # 策略 5: 硬切
    return limit


def _rfind_paragraph_break(text: str) -> int:
    """从右向左找段落边界（连续空行）。"""
    # 找最后一个 \n\n
    pos = text.rfind("\n\n")
    return pos + 2 if pos > 0 else 0


def _rfind_code_block_end(text: str) -> int:
    """从右向左找代码块结束标记。"""
    # 找最后一个 ``` 后跟换行
    pattern = re.compile(r"```\s*\n")
    matches = list(pattern.finditer(text))
    if matches:
        last = matches[-1]
        return last.end()
    return 0


def _rfind_list_break(text: str) -> int:
    """从右向左找列表项边界。"""
    # 找最后一个 \n 后跟 - 或数字列表
    pattern = re.compile(r"\n(?=[-*\d]+[\.\)] )")
    matches = list(pattern.finditer(text))
    if matches:
        return matches[-1].start() + 1  # 包含 \n
    return 0


def _rfind_sentence_end(text: str) -> int:
    """从右向左找句末。"""
    # 中英文句末标点
    pattern = re.compile(r"[。！？.!?]\s*\n?")
    matches = list(pattern.finditer(text))
    if matches:
        return matches[-1].end()
    return 0
