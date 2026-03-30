"""Shared context-pipeline helpers for agent runtimes."""

from __future__ import annotations

import logging
from typing import Any


def normalize_inbox_item(inbox_item: Any) -> tuple[str, list[str]]:
    """Normalize an inbox payload into text plus image URLs."""
    if isinstance(inbox_item, dict):
        text = inbox_item.get("text", "") or ""
        images = inbox_item.get("images") or []
    else:
        text = str(inbox_item)
        images = []
    normalized_images = [url for url in images if isinstance(url, str) and url]
    return text, normalized_images


def append_openai_inbox_messages(
    messages: list[dict[str, Any]],
    pending: list[Any],
    *,
    logger: logging.Logger | None = None,
    log_label: str = "inbox inject",
) -> int:
    """Append pending inbox items as OpenAI-style user messages.

    Returns the number of user messages appended.
    """
    appended = 0
    for inbox_item in pending:
        msg_text, msg_images = normalize_inbox_item(inbox_item)
        if logger:
            logger.info("%s: %s (images=%d)", log_label, msg_text[:60], len(msg_images))
        if not msg_text and not msg_images:
            continue
        if msg_images:
            parts: list[dict[str, Any]] = []
            if msg_text:
                parts.append({"type": "text", "text": msg_text})
            for url in msg_images:
                parts.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": msg_text})
        appended += 1
    return appended
