from __future__ import annotations

import os
from typing import Any

import httpx

from app.hermes_runtime.types import RuntimeRequest, RuntimeResponse, RuntimeUsage


def upstream_api_url() -> str:
    return os.getenv("HERMES_UPSTREAM_API_URL", "").strip().rstrip("/")


def _upstream_model() -> str:
    return os.getenv("HERMES_UPSTREAM_MODEL", "hermes-agent").strip() or "hermes-agent"


def _chat_path() -> str:
    path = os.getenv("HERMES_UPSTREAM_CHAT_PATH", "/v1/chat/completions").strip() or "/v1/chat/completions"
    return path if path.startswith("/") else f"/{path}"


def _headers(request: RuntimeRequest) -> dict[str, str]:
    headers = {
        "X-Hermes-Session-Id": request.conversation.history_key,
        "X-Hermes-Session-Key": request.sender.identity_id or request.sender.sender_id,
    }
    api_key = os.getenv("HERMES_UPSTREAM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
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


def _payload(request: RuntimeRequest) -> dict[str, Any]:
    return {
        "model": _upstream_model(),
        "stream": False,
        "messages": _messages(request),
    }


def _extract_final_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("invalid choice")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ValueError("missing message")
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


def _usage(data: dict[str, Any]) -> RuntimeUsage:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return RuntimeUsage(api_calls=1)
    return RuntimeUsage(
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        api_calls=1,
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
    try:
        timeout = httpx.Timeout(float(timeout_seconds))
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout, headers=_headers(request)) as client:
            response = await client.post(_chat_path(), json=_payload(request))
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("upstream response is not a JSON object")
        return RuntimeResponse(
            run_id=request.run_id,
            status="completed",
            final_text=_extract_final_text(data),
            usage=_usage(data),
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
