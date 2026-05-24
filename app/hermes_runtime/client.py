from __future__ import annotations

import asyncio
import json
import os
from typing import Awaitable, Callable

import httpx

from app.hermes_runtime.events import make_runtime_event
from app.hermes_runtime.observability import record_runtime_event, record_runtime_health
from app.hermes_runtime.types import (
    RuntimeConversation,
    RuntimeInput,
    RuntimeOptions,
    RuntimePolicy,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSender,
)


RuntimeWorker = Callable[[RuntimeRequest], Awaitable[RuntimeResponse]]


class RuntimeBadResponseError(ValueError):
    pass


def _sidecar_base_url() -> str:
    return os.getenv("HERMES_RUNTIME_URL", "").strip().rstrip("/")


def _turn_path() -> str:
    path = os.getenv("HERMES_RUNTIME_TURN_PATH", "/v1/runtime/turn").strip() or "/v1/runtime/turn"
    return path if path.startswith("/") else f"/{path}"


def _health_path() -> str:
    path = os.getenv("HERMES_RUNTIME_HEALTH_PATH", "/health").strip() or "/health"
    return path if path.startswith("/") else f"/{path}"


def _runtime_response_from_dict(data: dict) -> RuntimeResponse:
    if not isinstance(data, dict):
        raise RuntimeBadResponseError("sidecar response must be a JSON object")
    run_id = str(data.get("run_id") or "")
    if not run_id:
        raise RuntimeBadResponseError("sidecar response missing run_id")
    status = str(data.get("status") or "")
    if not status:
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
            response = await self._run_sidecar_turn(sidecar_url, request)
            self._record_response_event(request, response)
            return response

        worker = self._worker
        if worker is None:
            from app.hermes_runtime.worker import run_runtime_turn
            worker = run_runtime_turn
        try:
            response = await asyncio.wait_for(worker(request), timeout=self.timeout_seconds)
            self._record_response_event(request, response)
            return response
        except asyncio.TimeoutError:
            response = RuntimeResponse.failed(
                request.run_id,
                "runtime_timeout",
                f"Hermes runtime exceeded {self.timeout_seconds}s",
                retryable=True,
            )
            self._record_response_event(request, response)
            return response
        except Exception as exc:
            response = RuntimeResponse.failed(
                request.run_id,
                "runtime_unavailable",
                f"Hermes runtime unavailable: {exc}",
                retryable=True,
            )
            self._record_response_event(request, response)
            return response

    async def _run_sidecar_turn(self, sidecar_url: str, request: RuntimeRequest) -> RuntimeResponse:
        try:
            timeout = httpx.Timeout(float(self.timeout_seconds))
            async with httpx.AsyncClient(base_url=sidecar_url, timeout=timeout) as client:
                response = await client.post(_turn_path(), json=request.to_dict())
                response.raise_for_status()
                return _runtime_response_from_dict(response.json())
        except asyncio.TimeoutError:
            return RuntimeResponse.failed(
                request.run_id,
                "runtime_timeout",
                f"Hermes runtime exceeded {self.timeout_seconds}s",
                retryable=True,
            )
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
        sidecar_url = _sidecar_base_url()
        if sidecar_url:
            try:
                timeout = httpx.Timeout(float(self.timeout_seconds))
                with httpx.Client(base_url=sidecar_url, timeout=timeout) as client:
                    response = client.get(_health_path())
                    response.raise_for_status()
                    data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeBadResponseError("health response must be a JSON object")
                return {
                    "enabled": True,
                    "sidecar_status": str(data.get("status") or "ok"),
                    "sidecar_url": sidecar_url,
                    **data,
                }
            except Exception as exc:
                return {
                    "enabled": True,
                    "sidecar_status": "unavailable",
                    "sidecar_url": sidecar_url,
                    "last_error": str(exc),
                }

        from app.hermes_runtime.worker import health
        return health()

    def _record_response_event(self, request: RuntimeRequest, response: RuntimeResponse) -> None:
        error = response.error.code if response.error else ""
        event_name = {
            "completed": "runtime.completed",
            "partial": "runtime.partial",
            "failed": "runtime.failed",
            "timed_out": "runtime.failed",
        }.get(response.status, f"runtime.{response.status}")
        level = "error" if response.status in {"failed", "timed_out"} else "info"
        cost = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "api_calls": response.usage.api_calls,
            "tool_calls": response.usage.tool_calls,
        }
        record_runtime_event(
            make_runtime_event(
                run_id=request.run_id,
                tenant_id=request.tenant_id,
                channel_id=request.channel_id,
                session_id=_request_session_id(request),
                platform=request.platform,
                event=event_name,
                level=level,
                message=response.error.message if response.error else response.status,
                error=error,
                cost=cost,
            )
        )
        record_runtime_health(success=(response.status == "completed"), error=error)


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
    from app.services import redis_client as redis

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


def _request_session_id(request: RuntimeRequest) -> str:
    sender_identity = request.sender.identity_id or request.sender.sender_id
    chat_type = request.conversation.chat_type or "dm"
    chat_id = request.conversation.chat_id or sender_identity
    return f"hr:{request.tenant_id}:{request.channel_id}:{chat_type}:{chat_id}:{sender_identity}"
