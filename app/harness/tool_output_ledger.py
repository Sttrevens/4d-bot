"""Tenant-scoped ledger for full tool outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import threading
import time
import uuid
from typing import Any, Callable


DEFAULT_TTL_SECONDS = 60 * 60 * 24
DEFAULT_PREVIEW_CHARS = 240
_KEY_PREFIX = "tool_output"
_GLOBAL_LEDGER: ToolOutputLedger | None = None


@dataclass(frozen=True)
class ToolOutputRecord:
    output_id: str
    tenant_id: str
    tool_name: str
    content: str
    preview: str
    size: int
    created_at: float
    expires_at: float
    metadata: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def from_json(raw: str) -> ToolOutputRecord | None:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return ToolOutputRecord(
                output_id=str(data["output_id"]),
                tenant_id=str(data["tenant_id"]),
                tool_name=str(data["tool_name"]),
                content=str(data["content"]),
                preview=str(data.get("preview", "")),
                size=int(data.get("size", len(str(data["content"])))),
                created_at=float(data.get("created_at", 0.0)),
                expires_at=float(data.get("expires_at", 0.0)),
                metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
            )
        except (KeyError, TypeError, ValueError):
            return None


class ToolOutputLedger:
    """Store full tool outputs with Redis persistence and memory fallback."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
        redis_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.preview_chars = max(1, int(preview_chars))
        self._redis = redis_client if redis_client is not None else _default_redis_client()
        self._clock = clock
        self._lock = threading.Lock()
        self._memory: dict[tuple[str, str], ToolOutputRecord] = {}

    def record(
        self,
        *,
        tenant_id: str,
        tool_name: str,
        content: Any,
        metadata: dict[str, Any] | None = None,
    ) -> ToolOutputRecord:
        tenant = str(tenant_id or "").strip()
        output = content if isinstance(content, str) else str(content)
        now = self._clock()
        record = ToolOutputRecord(
            output_id=uuid.uuid4().hex,
            tenant_id=tenant,
            tool_name=str(tool_name or ""),
            content=output,
            preview=make_preview(output, self.preview_chars),
            size=len(output),
            created_at=now,
            expires_at=now + self.ttl_seconds,
            metadata=dict(metadata or {}),
        )
        self._save_memory(record)
        self._save_redis(record)
        return record

    def read(self, tenant_id: str, output_id: str) -> ToolOutputRecord | None:
        tenant = str(tenant_id or "").strip()
        oid = str(output_id or "").strip()
        if not tenant or not oid:
            return None

        record = self._read_memory(tenant, oid)
        if record is not None:
            return record

        record = self._read_redis(tenant, oid)
        if record is None:
            return None
        if self._is_expired(record):
            self._delete_redis(tenant, oid)
            return None
        self._save_memory(record)
        return record

    def clear_memory(self) -> None:
        with self._lock:
            self._memory.clear()

    def _save_memory(self, record: ToolOutputRecord) -> None:
        with self._lock:
            self._memory[(record.tenant_id, record.output_id)] = record

    def _read_memory(self, tenant_id: str, output_id: str) -> ToolOutputRecord | None:
        key = (tenant_id, output_id)
        with self._lock:
            record = self._memory.get(key)
            if record is None:
                return None
            if self._is_expired(record):
                self._memory.pop(key, None)
                return None
            return record

    def _save_redis(self, record: ToolOutputRecord) -> bool:
        if not _redis_available(self._redis):
            return False
        result = self._redis.execute(
            "SET",
            _redis_key(record.tenant_id, record.output_id),
            record.to_json(),
            "EX",
            self.ttl_seconds,
        )
        return result == "OK"

    def _read_redis(self, tenant_id: str, output_id: str) -> ToolOutputRecord | None:
        if not _redis_available(self._redis):
            return None
        raw = self._redis.execute("GET", _redis_key(tenant_id, output_id))
        if not raw:
            return None
        return ToolOutputRecord.from_json(str(raw))

    def _delete_redis(self, tenant_id: str, output_id: str) -> None:
        if _redis_available(self._redis):
            self._redis.execute("DEL", _redis_key(tenant_id, output_id))

    def _is_expired(self, record: ToolOutputRecord) -> bool:
        return record.expires_at <= self._clock()


def make_preview(content: str, max_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    text = " ".join(str(content or "").split())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rstrip()
    word_boundary = cut.rfind(" ")
    if word_boundary > 0:
        cut = cut[:word_boundary]
    return f"{cut}..."


def get_tool_output_ledger() -> ToolOutputLedger:
    global _GLOBAL_LEDGER
    if _GLOBAL_LEDGER is None:
        _GLOBAL_LEDGER = ToolOutputLedger()
    return _GLOBAL_LEDGER


def format_tool_output_for_model(
    *,
    tenant_id: str,
    tool_name: str,
    content: Any,
    max_inline_chars: int,
    ledger: ToolOutputLedger | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Return inline content, or store full output and return a ledger preview."""
    output = content if isinstance(content, str) else str(content)
    if len(output) <= max_inline_chars:
        return output

    tenant = str(tenant_id or "").strip()
    if not tenant:
        return (
            output[:max_inline_chars]
            + f"\n\n... (truncated, original {len(output)} chars; tenant unavailable)"
        )

    active_ledger = ledger or get_tool_output_ledger()
    record = active_ledger.record(
        tenant_id=tenant,
        tool_name=tool_name,
        content=output,
        metadata=metadata,
    )
    return (
        f"[tool_output_stored output_id={record.output_id}]\n"
        f"tool={record.tool_name} original_size={record.size} chars\n"
        "Use read_tool_output with this output_id if the full result is needed.\n\n"
        f"Preview:\n{record.preview}"
    )


def _redis_key(tenant_id: str, output_id: str) -> str:
    return f"{_KEY_PREFIX}:{tenant_id}:{output_id}"


def _redis_available(redis_client: Any | None) -> bool:
    if redis_client is None:
        return False
    available = getattr(redis_client, "available", None)
    if not callable(available):
        return False
    try:
        return bool(available())
    except Exception:
        return False


def _default_redis_client() -> Any | None:
    try:
        from app.services import redis_client
    except Exception:
        return None
    return redis_client
