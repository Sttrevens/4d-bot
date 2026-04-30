from app.harness.runtime_errors import (
    classify_runtime_error,
    classify_runtime_error_batch,
)
from app.services.error_log import ErrorRecord


def _record(
    *,
    category: str = "api_error",
    summary: str,
    detail: str = "",
    tool_name: str = "",
) -> ErrorRecord:
    return ErrorRecord(
        time="2026-04-30T14:00:00",
        category=category,
        summary=summary,
        detail=detail,
        tool_name=tool_name,
    )


def test_httpx_read_error_is_transient_upstream_not_autofixable():
    decision = classify_runtime_error(
        _record(
            summary="Gemini API call failed (round 3)",
            detail="httpcore.ReadError\n\nThe above exception was the direct cause of:\nhttpx.ReadError",
            tool_name="gemini",
        )
    )

    assert decision.kind == "transient_upstream"
    assert not decision.autofix_allowed
    assert "network" in decision.labels


def test_github_self_get_connection_error_is_transient_upstream():
    decision = classify_runtime_error(
        _record(
            summary="_self_get connection error: https://api.github.com/repos/Sttrevens/4d-bot/contents/app/app/services/gemini_provider.py",
            detail="httpcore.ConnectTimeout: _ssl.c:993: The handshake operation timed out\nhttpx.ConnectTimeout",
            tool_name="self_read_file",
        )
    )

    assert decision.kind == "transient_upstream"
    assert not decision.autofix_allowed
    assert "github" in decision.labels


def test_plain_timeout_category_is_transient_upstream():
    decision = classify_runtime_error(
        _record(
            category="timeout",
            summary="QQ 消息处理超时 sender=123 text=hello",
        )
    )

    assert decision.kind == "transient_upstream"
    assert not decision.autofix_allowed
    assert "timeout" in decision.labels


def test_allowed_tool_stack_trace_is_autofixable_code_bug():
    decision = classify_runtime_error(
        _record(
            category="tool_exception",
            summary="web_search 异常",
            detail='Traceback (most recent call last):\n  File "/app/app/tools/web_search.py", line 88, in web_search\n    raise ValueError("bad parser")',
            tool_name="web_search",
        )
    )

    assert decision.kind == "code_bug"
    assert decision.autofix_allowed
    assert "allowed_path" in decision.labels


def test_core_service_stack_trace_is_manual_diagnostic():
    decision = classify_runtime_error(
        _record(
            category="unhandled",
            summary="Gemini provider crashed",
            detail='Traceback (most recent call last):\n  File "/app/app/services/gemini_provider.py", line 2261, in handle_message\n    raise RuntimeError("bad state")',
            tool_name="gemini",
        )
    )

    assert decision.kind == "manual_diagnostic"
    assert not decision.autofix_allowed
    assert decision.diagnostic_only


def test_batch_allows_only_autofixable_code_errors_to_trigger():
    network = _record(
        summary="Gemini API call failed (round 3)",
        detail="httpx.ReadError",
        tool_name="gemini",
    )
    tool_bug = _record(
        category="tool_exception",
        summary="web_search 异常",
        detail='File "/app/app/tools/web_search.py", line 88, in web_search\nValueError: bad parser',
        tool_name="web_search",
    )

    batch = classify_runtime_error_batch([network, tool_bug])

    assert batch.should_autofix
    assert batch.autofixable_errors == [tool_bug]
    assert batch.transient_count == 1
