from __future__ import annotations

import asyncio
import json
import os
from typing import Awaitable, Callable

import httpx

from app.hermes_runtime.types import (
    RuntimeConversation,
    RuntimeInput,
    RuntimeOptions,
    RuntimePolicy,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSender,
)
from app.services import redis_client as redis


RuntimeWorker = Callable[[RuntimeRequest], Awaitable[RuntimeResponse]]


class RuntimeBadResponseError(ValueError):
    pass


def _sidecar_base_url() -> str:
    return os.getenv("HERMES_RUNTIME_URL", "").strip().rstrip("/")


def _sidecar_turn_path() -> str:
    path = os.getenv("HERMES_RUNTIME_TURN_PATH", "/v1/runtime/turn").strip() or "/v1/runtime/turn"
    return path if path.startswith("/") else f"/{path}"


def _runtime_response_from_dict(data: dict) -> RuntimeResponse:
    if not isinstance(data, dict):
        raise RuntimeBadResponseError("sidecar response must be a JSON object")
    if not str(data.get("run_id") or ""):
        raise RuntimeBadResponseError("sidecar response missing run_id")
    if not str(data.get("status") or ""):
        raise RuntimeBadResponseError("sidecar response missing status")
    try:
        return RuntimeResponse.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise RuntimeBadResponseError(str(exc)) from exc


class HermesRuntimeClient:
    def __init__(
        self,
        *,
        timeout_seconds: int = 180,
        worker: RuntimeWorker | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._worker = worker

    async def run_turn(self, request: RuntimeRequest) -> RuntimeResponse:
        sidecar_url = _sidecar_base_url()
        if self._worker is None and sidecar_url:
            return await self._run_sidecar_turn(sidecar_url, request)

        worker = self._worker
        if worker is None:
            from app.hermes_runtime.worker import run_runtime_turn
            worker = run_runtime_turn
        try:
            return await asyncio.wait_for(worker(request), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_timeout",
                f"Hermes runtime exceeded {self.timeout_seconds}s",
                retryable=True,
            )
        except Exception as exc:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_unavailable",
                f"Hermes runtime unavailable: {exc}",
                retryable=True,
            )

    async def _run_sidecar_turn(self, sidecar_url: str, request: RuntimeRequest) -> RuntimeResponse:
        try:
            timeout = httpx.Timeout(float(self.timeout_seconds))
            async with httpx.AsyncClient(base_url=sidecar_url, timeout=timeout) as client:
                response = await client.post(_sidecar_turn_path(), json=request.to_dict())
                response.raise_for_status()
                return _runtime_response_from_dict(response.json())
        except RuntimeBadResponseError as exc:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_bad_response",
                f"Hermes runtime returned an invalid response: {exc}",
                retryable=True,
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_unavailable",
                f"Hermes runtime sidecar unavailable: {exc}",
                retryable=True,
            )
        except ValueError as exc:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_bad_response",
                f"Hermes runtime returned invalid JSON: {exc}",
                retryable=True,
            )
        except Exception as exc:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_unavailable",
                f"Hermes runtime unavailable: {exc}",
                retryable=True,
            )

    def health(self) -> dict:
        from app.hermes_runtime.worker import health
        return health()


def _as_string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str) and v.strip()]


def build_runtime_request(
    tenant,
    *,
    run_id: str,
    user_text: str,
    sender_id: str,
    sender_name: str = "",
    identity_id: str = "",
    history_key: str,
    chat_id: str = "",
    chat_type: str = "",
    mode: str = "safe",
    chat_context: str = "",
    image_urls: list[str] | None = None,
    channel_id: str = "",
    platform: str = "",
    shadow_mode: bool = False,
) -> RuntimeRequest:
    tenant_id = str(getattr(tenant, "tenant_id", "") or "")
    platform = platform or str(getattr(tenant, "platform", "") or "")
    if not channel_id:
        try:
            channel_id = tenant._build_primary_channel().channel_id
        except Exception:
            channel_id = f"{tenant_id}-{platform}" if tenant_id and platform else tenant_id

    return RuntimeRequest(
        run_id=run_id,
        tenant_id=tenant_id,
        channel_id=channel_id,
        platform=platform,
        sender=RuntimeSender(
            sender_id=sender_id,
            sender_name=sender_name,
            identity_id=identity_id or sender_id,
        ),
        conversation=RuntimeConversation(
            history_key=history_key,
            chat_id=chat_id,
            chat_type=chat_type,
            mode=mode,
        ),
        input=RuntimeInput(
            text=user_text,
            image_urls=list(image_urls or []),
            chat_context=chat_context,
        ),
        policy=RuntimePolicy(
            allowed_tool_names=_as_string_list(getattr(tenant, "tools_enabled", [])),
            allowed_tool_groups=_as_string_list(getattr(tenant, "plugin_groups_enabled", [])),
            admin=False,
            self_iteration_enabled=bool(getattr(tenant, "self_iteration_enabled", False)),
            shadow_mode=shadow_mode,
        ),
        runtime=RuntimeOptions(
            profile=str(getattr(tenant, "hermes_runtime_profile", "default") or "default"),
            memory_enabled=bool(getattr(tenant, "memory_context_enabled", True)),
            skills_enabled=True,
            mcp_enabled=bool(getattr(tenant, "mcp_enabled", False)),
            code_execution_backend=str(getattr(tenant, "hermes_code_execution_backend", "none") or "none"),
        ),
    )


def store_shadow_response(tenant_id: str, run_id: str, response: RuntimeResponse) -> None:
    from app.hermes_runtime.observability import shadow_result_key

    try:
        redis.execute(
            "SET",
            shadow_result_key(tenant_id, run_id),
            json.dumps(response.to_dict(), ensure_ascii=False),
            "EX",
            "604800",
        )
    except Exception:
        pass
