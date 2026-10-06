from __future__ import annotations

import asyncio
import json
import os
from typing import Awaitable, Callable

import httpx

from app.hermes_runtime.events import make_runtime_event
from app.hermes_runtime.observability import record_runtime_event, record_runtime_health
from app.hermes_runtime.types import (
    RuntimeArtifact,
    RuntimeConversation,
    RuntimeErrorInfo,
    RuntimeInput,
    RuntimeOptions,
    RuntimePolicy,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSender,
    RuntimeToolCallRecord,
    RuntimeUsage,
)


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
    run_id = str(data.get("run_id") or "")
    if not run_id:
        raise RuntimeBadResponseError("sidecar response missing run_id")
    status = str(data.get("status") or "")
    if not status:
        raise RuntimeBadResponseError("sidecar response missing status")
    try:
        artifacts = [
            RuntimeArtifact(**item)
            for item in data.get("artifacts", []) or []
            if isinstance(item, dict)
        ]
        tool_calls = [
            RuntimeToolCallRecord(**item)
            for item in data.get("tool_calls", []) or []
            if isinstance(item, dict)
        ]
        usage_raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        error_raw = data.get("error") if isinstance(data.get("error"), dict) else None
        return RuntimeResponse(
            run_id=run_id,
            runtime=str(data.get("runtime") or "hermes_sidecar"),
            status=status,
            final_text=str(data.get("final_text") or ""),
            artifacts=artifacts,
            tool_calls=tool_calls,
            usage=RuntimeUsage(**usage_raw),
            events=list(data.get("events", []) or []),
            resume=dict(data.get("resume") or {"resumable": False, "resume_token": ""}),
            error=RuntimeErrorInfo(**error_raw) if error_raw else None,
        )
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


def _agent_runtime_provider(tenant) -> str:
    raw_provider = getattr(tenant, "agent_runtime_provider", "")
    provider = raw_provider.strip() if isinstance(raw_provider, str) else ""
    if provider:
        return provider
    raw_legacy = getattr(tenant, "agent_runtime", "legacy")
    return raw_legacy if isinstance(raw_legacy, str) and raw_legacy else "legacy"


def _safe_runtime_sandbox(tenant) -> str:
    raw_sandbox = getattr(tenant, "agent_runtime_sandbox", "read-only")
    sandbox = raw_sandbox.strip() if isinstance(raw_sandbox, str) and raw_sandbox.strip() else "read-only"
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        return "read-only"
    auto_execute = getattr(tenant, "agent_runtime_auto_execute", False)
    if sandbox != "read-only" and auto_execute is not True:
        return "read-only"
    return sandbox


def _str_runtime_attr(tenant, name: str, default: str = "") -> str:
    value = getattr(tenant, name, default)
    return value if isinstance(value, str) else default


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
            provider=_agent_runtime_provider(tenant),
            profile=str(getattr(tenant, "hermes_runtime_profile", "default") or "default"),
            memory_enabled=bool(getattr(tenant, "memory_context_enabled", True)),
            skills_enabled=True,
            mcp_enabled=bool(getattr(tenant, "mcp_enabled", False)),
            code_execution_backend=str(getattr(tenant, "hermes_code_execution_backend", "none") or "none"),
            command=_str_runtime_attr(tenant, "agent_runtime_command", ""),
            args=_as_string_list(getattr(tenant, "agent_runtime_args", [])),
            workspace=_str_runtime_attr(tenant, "agent_runtime_workspace", ""),
            sandbox=_safe_runtime_sandbox(tenant),
            permission_mode=_str_runtime_attr(tenant, "agent_runtime_permission_mode", "plan") or "plan",
            auto_execute=getattr(tenant, "agent_runtime_auto_execute", False) is True,
            model=_str_runtime_attr(tenant, "agent_runtime_model", ""),
        ),
    )


def store_shadow_response(tenant_id: str, run_id: str, response: RuntimeResponse) -> None:
    from app.hermes_runtime import observability

    try:
        observability._redis_execute(
            "SET",
            observability.shadow_result_key(tenant_id, run_id),
            json.dumps(response.to_dict(), ensure_ascii=False),
            "EX",
            "604800",
        )
        observability.record_runtime_run_summary({
            "tenant_id": tenant_id,
            "run_id": run_id,
            "runtime": "legacy_shadow_hermes",
            "status": response.status,
            "shadow": True,
            "shadow_mode": True,
            "error": response.error.code if response.error else "",
            "cost": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "api_calls": response.usage.api_calls,
                "tool_calls": response.usage.tool_calls,
            },
            "tool_calls": len(response.tool_calls),
        })
    except Exception:
        pass


def _request_session_id(request: RuntimeRequest) -> str:
    sender_identity = request.sender.identity_id or request.sender.sender_id
    chat_type = request.conversation.chat_type or "dm"
    chat_id = request.conversation.chat_id or sender_identity
    return f"hr:{request.tenant_id}:{request.channel_id}:{chat_type}:{chat_id}:{sender_identity}"
