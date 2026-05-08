"""Lightweight per-turn latency tracing for bot reply paths."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import json
import logging
import os
import time
from typing import Any, Iterator

logger = logging.getLogger(__name__)


_CURRENT_TRACE: ContextVar["LatencyTrace | None"] = ContextVar(
    "bot_latency_trace",
    default=None,
)


def _env_truthy(key: str, default: str = "1") -> bool:
    value = os.getenv(key, default).strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _env_float(key: str, default: float, *, minimum: float = 0.0, maximum: float = 60.0) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value


def fastpath_enabled() -> bool:
    return _env_truthy("BOT_FASTPATH_ENABLED", "1")


def env_mode(key: str, default: str = "adaptive") -> str:
    return (os.getenv(key, default).strip().lower() or default)


def get_webhook_batch_wait_seconds() -> float:
    return _env_float("WEBHOOK_BATCH_WAIT_S", 0.8, minimum=0.05, maximum=5.0)


def get_intent_classifier_timeout_seconds() -> float:
    return _env_float("BOT_INTENT_CLASSIFIER_TIMEOUT_S", 0.8, minimum=0.05, maximum=5.0)


def get_progress_hint_budget_seconds() -> float:
    return _env_float("BOT_PROGRESS_HINT_BUDGET_S", 0.2, minimum=0.01, maximum=2.0)


@dataclass
class LatencyTrace:
    tenant_id: str
    run_id: str = ""
    request_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    spans: list[dict[str, Any]] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    rounds: int = 0
    _finished: bool = False

    @contextmanager
    def span(self, name: str, **metadata: Any) -> Iterator[None]:
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            item: dict[str, Any] = {"name": name, "ms": elapsed_ms}
            clean_meta = {
                k: v for k, v in metadata.items()
                if v is not None and v != ""
            }
            if clean_meta:
                item["metadata"] = clean_meta
            self.spans.append(item)

    def add_span(self, name: str, ms: int, **metadata: Any) -> None:
        item: dict[str, Any] = {"name": name, "ms": max(0, int(ms))}
        clean_meta = {
            k: v for k, v in metadata.items()
            if v is not None and v != ""
        }
        if clean_meta:
            item["metadata"] = clean_meta
        self.spans.append(item)

    def record_round(self, model: str) -> None:
        self.rounds += 1
        if model:
            self.models.append(model)

    def record_tool(self, tool_name: str) -> None:
        if tool_name:
            self.tools.append(tool_name)

    def finish(self, outcome: str = "unknown", **metadata: Any) -> dict[str, Any]:
        total_ms = int((time.monotonic() - self.started_at) * 1000)
        payload: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "request_key": self.request_key,
            "outcome": outcome,
            "total_ms": total_ms,
            "spans": self.spans,
            "rounds": self.rounds,
            "tools": self.tools,
            "models": self.models,
        }
        merged_meta = dict(self.metadata)
        merged_meta.update({k: v for k, v in metadata.items() if v is not None and v != ""})
        if merged_meta:
            payload["metadata"] = merged_meta
        if not self._finished:
            logger.info("latency_trace: %s", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            self._finished = True
        return payload


def get_current_trace() -> LatencyTrace | None:
    return _CURRENT_TRACE.get()


def set_current_trace(trace: LatencyTrace | None) -> Token:
    return _CURRENT_TRACE.set(trace)


def reset_current_trace(token: Token) -> None:
    _CURRENT_TRACE.reset(token)


@contextmanager
def trace_span(name: str, **metadata: Any) -> Iterator[None]:
    trace = get_current_trace()
    if trace is None:
        with nullcontext():
            yield
        return
    with trace.span(name, **metadata):
        yield


def record_round(model: str) -> None:
    trace = get_current_trace()
    if trace is not None:
        trace.record_round(model)


def record_tool(tool_name: str) -> None:
    trace = get_current_trace()
    if trace is not None:
        trace.record_tool(tool_name)
