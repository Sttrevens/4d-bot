from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.hermes_runtime.types import RuntimeRequest, RuntimeResponse, RuntimeToolCallRecord, RuntimeUsage


def upstream_api_url() -> str:
    return os.getenv("HERMES_UPSTREAM_API_URL", "").strip().rstrip("/")


def _upstream_model(provider_config: dict[str, Any] | None = None) -> str:
    override = os.getenv("HERMES_UPSTREAM_MODEL", "").strip()
    if override:
        return override
    config = provider_config or {}
    return str(config.get("base_model") or "hermes-agent").strip() or "hermes-agent"


def _chat_path() -> str:
    path = os.getenv("HERMES_UPSTREAM_CHAT_PATH", "/v1/chat/completions").strip() or "/v1/chat/completions"
    return path if path.startswith("/") else f"/{path}"


def _headers(request: RuntimeRequest, provider_config: dict[str, Any] | None = None) -> dict[str, str]:
    headers = {
        "X-Hermes-Session-Id": request.conversation.history_key,
        "X-Hermes-Session-Key": request.sender.identity_id or request.sender.sender_id,
    }
    api_key = os.getenv("HERMES_UPSTREAM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if provider_config and int(provider_config.get("credential_pool_size") or 0) > 1:
        credential_ref = str(provider_config.get("credential_ref") or "").strip()
        if credential_ref:
            headers["X-Hermes-Credential-Ref"] = credential_ref
    return headers


def _user_content(request: RuntimeRequest) -> str | list[dict[str, Any]]:
    if not request.input.image_urls:
        return request.input.text
    parts: list[dict[str, Any]] = [{"type": "text", "text": request.input.text}]
    for url in request.input.image_urls:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _messages(request: RuntimeRequest) -> list[dict[str, Any]]:
    system_parts = [
        "You are the upstream Hermes Agent runtime behind a production bot adapter.",
        f"tenant_id={request.tenant_id}",
        f"channel_id={request.channel_id}",
        f"platform={request.platform}",
    ]
    if request.input.chat_context:
        system_parts.append(request.input.chat_context)
    return [
        {"role": "system", "content": "\n".join(system_parts)},
        {"role": "user", "content": _user_content(request)},
    ]


def _payload(
    request: RuntimeRequest,
    *,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    provider_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": _upstream_model(provider_config),
        "stream": False,
        "messages": messages if messages is not None else _messages(request),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _extract_message(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("invalid choice")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ValueError("missing message")
    return message


def _extract_final_text(data: dict[str, Any]) -> str:
    message = _extract_message(data)
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        text_parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and str(part.get("text") or "").strip()
        ]
        if text_parts:
            return "\n".join(text_parts)
    raise ValueError("missing assistant content")


def _merge_usage(total: RuntimeUsage, item: RuntimeUsage) -> RuntimeUsage:
    return RuntimeUsage(
        input_tokens=total.input_tokens + item.input_tokens,
        output_tokens=total.output_tokens + item.output_tokens,
        api_calls=total.api_calls + item.api_calls,
        tool_calls=total.tool_calls + item.tool_calls,
    )


def _load_tenant(tenant_id: str):
    from app.tenant.registry import tenant_registry

    return tenant_registry.get(tenant_id) or tenant_registry.get_default()


def _provider_config_for_request(request: RuntimeRequest) -> dict[str, Any]:
    from app.hermes_runtime.provider_router import build_provider_config

    return build_provider_config(_load_tenant(request.tenant_id), run_id=request.run_id)


def _model_selected_event(provider_config: dict[str, Any]) -> dict[str, Any]:
    event = {
        "event": "runtime.model.selected",
        "provider": str(provider_config.get("provider") or ""),
        "model": _upstream_model(provider_config),
        "credential_ref": str(provider_config.get("credential_ref") or ""),
    }
    if int(provider_config.get("credential_pool_size") or 0) > 1:
        event["credential_id"] = str(provider_config.get("credential_id") or "")
    return event


def _provider_exhausted_event(provider_config: dict[str, Any], *, status_code: int) -> dict[str, Any]:
    event = {
        "event": "runtime.provider.exhausted",
        "provider": str(provider_config.get("provider") or ""),
        "model": _upstream_model(provider_config),
        "credential_ref": str(provider_config.get("credential_ref") or ""),
        "status_code": status_code,
    }
    if int(provider_config.get("credential_pool_size") or 0) > 1:
        event["credential_id"] = str(provider_config.get("credential_id") or "")
    return event


def _provider_credential_candidates(provider_config: dict[str, Any]) -> list[dict[str, str]]:
    candidates = provider_config.get("credential_candidates")
    if not isinstance(candidates, list) or not candidates:
        return [
            {
                "tenant_id": str(provider_config.get("tenant_id") or ""),
                "provider": str(provider_config.get("provider") or ""),
                "credential_id": "primary",
                "secret_ref": str(provider_config.get("credential_ref") or ""),
            }
        ]
    filtered: list[dict[str, str]] = []
    tenant_id = str(provider_config.get("tenant_id") or "")
    provider = str(provider_config.get("provider") or "")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("tenant_id") or "") != tenant_id:
            continue
        if str(candidate.get("provider") or "") != provider:
            continue
        secret_ref = str(candidate.get("secret_ref") or "")
        credential_id = str(candidate.get("credential_id") or "")
        if not secret_ref or not credential_id:
            continue
        filtered.append(
            {
                "tenant_id": tenant_id,
                "provider": provider,
                "credential_id": credential_id,
                "secret_ref": secret_ref,
            }
        )
    return filtered or [
        {
            "tenant_id": tenant_id,
            "provider": provider,
            "credential_id": "primary",
            "secret_ref": str(provider_config.get("credential_ref") or ""),
        }
    ]


def _provider_attempt_configs(provider_config: dict[str, Any]) -> list[dict[str, Any]]:
    models = [
        (
            str(provider_config.get("provider") or ""),
            _upstream_model(provider_config),
        )
    ]
    seen_models = {_upstream_model(provider_config)}
    for fallback in provider_config.get("fallbacks") or []:
        if not isinstance(fallback, dict):
            continue
        model = str(fallback.get("model") or "").strip()
        if not model or model in seen_models:
            continue
        models.append((str(fallback.get("provider") or provider_config.get("provider") or ""), model))
        seen_models.add(model)

    credentials = _provider_credential_candidates(provider_config)
    attempts: list[dict[str, Any]] = []
    seen_attempts: set[tuple[str, str, str]] = set()
    for provider, model in models:
        for credential in credentials:
            key = (provider, model, credential["secret_ref"])
            if key in seen_attempts:
                continue
            attempt = dict(provider_config)
            attempt["provider"] = provider
            attempt["base_model"] = model
            attempt["credential_ref"] = credential["secret_ref"]
            attempt["credential_id"] = credential["credential_id"]
            attempt["credential_pool_size"] = len(credentials)
            attempts.append(attempt)
            seen_attempts.add(key)
    return attempts


def _is_provider_exhausted_response(response: httpx.Response) -> bool:
    return response.status_code == 429


def _openai_tools_from_request(request: RuntimeRequest):
    if not request.policy.allowed_tool_names:
        return None, []

    from app.hermes_runtime.tool_bridge import RuntimeToolset, build_runtime_toolset

    toolset = build_runtime_toolset(_load_tenant(request.tenant_id), user_text=request.input.text)
    allowed = set(request.policy.allowed_tool_names)
    schemas = [tool.schema for tool in toolset.tools if tool.name in allowed]
    handlers = {name: handler for name, handler in toolset.handlers.items() if name in allowed}
    return RuntimeToolset(tools=toolset.tools, handlers=handlers, tenant_id=toolset.tenant_id), schemas


def _decode_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _extract_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    parsed = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        if not name:
            continue
        parsed.append(
            {
                "id": str(call.get("id") or f"call_{len(parsed) + 1}"),
                "tool_name": name,
                "args": _decode_tool_args(function.get("arguments")),
                "raw": call,
            }
        )
    return parsed


def _usage(data: dict[str, Any]) -> RuntimeUsage:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return RuntimeUsage(api_calls=1)
    return RuntimeUsage(
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        api_calls=1,
    )


def _append_chat_context(request: RuntimeRequest, context: str) -> RuntimeRequest:
    if not context.strip():
        return request
    existing = request.input.chat_context.strip()
    if context in existing:
        return request
    updated = RuntimeRequest.from_dict(request.to_dict())
    updated.input.chat_context = f"{existing}\n\n{context}" if existing else context
    return updated


def _memory_bridge_for_request(request: RuntimeRequest):
    if not request.runtime.memory_enabled:
        return None
    identity_id = request.sender.identity_id or request.sender.sender_id
    if not identity_id:
        return None
    from app.hermes_runtime.memory_bridge import HermesMemoryBridge
    from app.hermes_runtime.memory_scope import MemoryScope, build_session_id

    scope_name = request.conversation.chat_type or "dm"
    session_id = build_session_id(
        tenant_id=request.tenant_id,
        channel_id=request.channel_id,
        scope=scope_name,
        identity_id=identity_id,
        chat_id=request.conversation.chat_id,
    )
    return HermesMemoryBridge(
        MemoryScope(
            tenant_id=request.tenant_id,
            session_id=session_id,
            identity_id=identity_id,
            sender_name=request.sender.sender_name,
            chat_id=request.conversation.chat_id,
            channel_id=request.channel_id,
        ),
        run_id=request.run_id,
    )


async def run_upstream_turn(request: RuntimeRequest, *, timeout_seconds: int = 180) -> RuntimeResponse:
    base_url = upstream_api_url()
    if not base_url:
        return RuntimeResponse.failed(
            request.run_id,
            "runtime_unavailable",
            "HERMES_UPSTREAM_API_URL is not configured.",
            retryable=True,
        )
    from app.hermes_runtime.skills_bridge import append_skill_activation_context

    request = append_skill_activation_context(request)
    memory_bridge = _memory_bridge_for_request(request)
    if memory_bridge is not None:
        memory_context = await memory_bridge.prefetch(request.input.text)
        request = _append_chat_context(request, memory_context)
    try:
        from app.hermes_runtime.tool_bridge import execute_tool_calls

        timeout = httpx.Timeout(float(timeout_seconds))
        messages = _messages(request)
        provider_attempts = _provider_attempt_configs(_provider_config_for_request(request))
        events: list[dict[str, Any]] = []
        toolset, tools = _openai_tools_from_request(request)
        usage = RuntimeUsage()
        tool_records: list[RuntimeToolCallRecord] = []

        async with httpx.AsyncClient(base_url=base_url, timeout=timeout, headers=_headers(request)) as client:
            for attempt_index, provider_config in enumerate(provider_attempts):
                events.append(_model_selected_event(provider_config))
                for _round in range(max(1, request.runtime.max_rounds)):
                    request_headers = _headers(request, provider_config)
                    post_kwargs: dict[str, Any] = {
                        "json": _payload(request, messages=messages, tools=tools, provider_config=provider_config)
                    }
                    if "X-Hermes-Credential-Ref" in request_headers:
                        post_kwargs["headers"] = request_headers
                    response = await client.post(_chat_path(), **post_kwargs)
                    if _is_provider_exhausted_response(response):
                        events.append(
                            _provider_exhausted_event(provider_config, status_code=response.status_code)
                        )
                        if attempt_index < len(provider_attempts) - 1:
                            break
                        return RuntimeResponse.failed(
                            request.run_id,
                            "provider_exhausted",
                            "Upstream Hermes provider credentials were exhausted.",
                            retryable=True,
                        )
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError("upstream response is not a JSON object")
                    usage = _merge_usage(usage, _usage(data))
                    message = _extract_message(data)
                    tool_calls = _extract_tool_calls(message)
                    if not tool_calls:
                        final_text = _extract_final_text(data)
                        if memory_bridge is not None:
                            await memory_bridge.sync_turn(
                                request.input.text,
                                final_text,
                                tool_names=[record.tool_name for record in tool_records],
                            )
                        return RuntimeResponse(
                            run_id=request.run_id,
                            status="completed",
                            final_text=final_text,
                            tool_calls=tool_records,
                            usage=usage,
                            events=events,
                        )
                    if toolset is None:
                        return RuntimeResponse.failed(
                            request.run_id,
                            "policy_denied",
                            "Upstream Hermes requested a tool, but no tools are allowed for this runtime request.",
                            retryable=False,
                        )

                    messages.append(
                        {
                            "role": "assistant",
                            "content": message.get("content") or "",
                            "tool_calls": [call["raw"] for call in tool_calls],
                        }
                    )
                    results = await execute_tool_calls(
                        toolset,
                        request.policy,
                        [{"tool_name": call["tool_name"], "args": call["args"]} for call in tool_calls],
                        run_id=request.run_id,
                        tenant_id=request.tenant_id,
                    )
                    usage = _merge_usage(usage, RuntimeUsage(tool_calls=len(results)))
                    for call, result in zip(tool_calls, results, strict=False):
                        tool_records.append(
                            RuntimeToolCallRecord(
                                tool_name=call["tool_name"],
                                status="success" if result.ok else "failed",
                                duration_ms=result.duration_ms,
                                side_effect=result.side_effect,
                                code=result.code,
                            )
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": result.content,
                            }
                        )
                else:
                    return RuntimeResponse.failed(
                        request.run_id,
                        "context_overflow",
                        "Upstream Hermes runtime exceeded the configured tool round limit.",
                        retryable=True,
                    )
                continue

        return RuntimeResponse.failed(
            request.run_id,
            "provider_exhausted",
            "Upstream Hermes provider credentials were exhausted.",
            retryable=True,
        )
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        return RuntimeResponse.failed(
            request.run_id,
            "runtime_unavailable",
            f"Upstream Hermes runtime unavailable: {exc}",
            retryable=True,
        )
    except (ValueError, TypeError) as exc:
        return RuntimeResponse.failed(
            request.run_id,
            "runtime_bad_response",
            f"Upstream Hermes runtime returned an invalid response: {exc}",
            retryable=True,
        )
