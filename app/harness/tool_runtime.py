"""Runtime helpers for safe tool invocation."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable


async def invoke_tool_handler(handler: Callable[[dict], Any], args: dict) -> Any:
    """Invoke a tool handler and await nested awaitables when needed.

    Some tools are registered as synchronous wrappers that *return* coroutine
    objects (e.g. ``lambda args: async_impl(...)``). Relying only on
    ``iscoroutinefunction`` misses this shape and can leak un-awaited coroutines.
    """
    result = handler(args) if not asyncio.iscoroutinefunction(handler) else await handler(args)
    if inspect.isawaitable(result):
        result = await result
    return result
