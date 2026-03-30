"""Shared context-compaction helpers for agent runtimes.

This is the first phase of the harness split: provider-specific runtimes keep
owning their loop, while token-pressure policy moves into a shared module.
"""

from __future__ import annotations

import logging
from typing import Any

DEFAULT_COMPACTION_KEEP_RECENT = 8
DEFAULT_COMPACTION_AFTER_ROUND = 6
_COMPRESSED_SUFFIX = "\n...[已压缩]"
_MAX_RESULT_CHARS = 200


def _truncate_text(value: str, max_chars: int = _MAX_RESULT_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + _COMPRESSED_SUFFIX


def compress_openai_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = DEFAULT_COMPACTION_KEEP_RECENT,
    logger: logging.Logger | None = None,
) -> int:
    """Trim older OpenAI-style tool messages in-place.

    Returns the number of messages that were compacted.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_recent:
        return 0

    compressed = 0
    for idx in tool_indices[:-keep_recent]:
        content = messages[idx].get("content", "")
        if isinstance(content, str):
            compacted = _truncate_text(content)
            if compacted != content:
                messages[idx]["content"] = compacted
                compressed += 1

    if compressed and logger:
        logger.debug("compressed %d old tool results", compressed)
    return compressed


def compress_gemini_function_results(
    contents: list[Any],
    *,
    keep_recent: int = DEFAULT_COMPACTION_KEEP_RECENT,
    logger: logging.Logger | None = None,
) -> int:
    """Trim older Gemini function_response payloads in-place."""
    fr_locations: list[tuple[int, int]] = []
    for ci, content in enumerate(contents):
        parts = getattr(content, "parts", None) or []
        for pi, part in enumerate(parts):
            if getattr(part, "function_response", None):
                fr_locations.append((ci, pi))

    if len(fr_locations) <= keep_recent:
        return 0

    compressed = 0
    for ci, pi in fr_locations[:-keep_recent]:
        try:
            response = contents[ci].parts[pi].function_response.response
        except Exception:
            continue
        if not isinstance(response, dict):
            continue
        result = response.get("result", "")
        if isinstance(result, str):
            compacted = _truncate_text(result)
            if compacted != result:
                response["result"] = compacted
                compressed += 1

    if compressed and logger:
        logger.debug("compressed %d old gemini function results", compressed)
    return compressed
