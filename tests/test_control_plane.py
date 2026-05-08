from __future__ import annotations

from app.harness.control_plane import (
    ControlEvent,
    append_openai_control_event,
    render_control_event,
    sanitize_public_reply,
)


UNSAFE_TERMS = (
    "证据账本",
    "grounding",
    "中立裁判",
    "CONTACT_ENRICHMENT_GATE",
    "<tools_used>",
)


def _rendered_text(message: dict) -> str:
    return str(message.get("content") or message.get("text") or "")


def test_control_event_renderer_hides_internal_audit_terms_for_providers():
    event = ControlEvent(
        kind="exit_governor",
        reason_code="deterministic.grounding",
        action="verify_evidence",
        payload={
            "missing": ["报告文件"],
            "allowed_entities": ["张三"],
            "unsafe_debug": "证据账本 grounding 中立裁判 CONTACT_ENRICHMENT_GATE",
        },
        audit_summary="证据账本 grounding 中立裁判 CONTACT_ENRICHMENT_GATE",
    )

    for provider in ("openai", "gemini"):
        message = render_control_event(
            event,
            provider=provider,
            channel_platform="qq",
            available_tools={"web_search", "fetch_url"},
        )
        text = _rendered_text(message)
        assert message["role"] == "user"
        assert "请" in text
        for term in UNSAFE_TERMS:
            assert term not in text


def test_append_openai_control_event_adds_only_safe_user_message():
    messages: list[dict] = []
    event = ControlEvent(
        kind="unmatched_read",
        reason_code="read_without_write",
        action="write_after_read",
        audit_summary="read-without-write grounding 中立裁判",
    )

    append_openai_control_event(
        messages,
        event,
        channel_platform="feishu",
        available_tools={"write_file"},
    )

    assert messages == [
        {
            "role": "user",
            "content": messages[0]["content"],
        }
    ]
    for term in UNSAFE_TERMS:
        assert term not in messages[0]["content"]


def test_public_output_policy_requests_retry_and_scrubs_internal_terms():
    decision = sanitize_public_reply(
        "收到。\n<tools_used>web_search</tools_used>\n"
        "证据账本 grounding 中立裁判 CONTACT_ENRICHMENT_GATE\n"
        "真正回答：我在，直接说需求就行。",
        channel_platform="qq",
    )

    assert decision.should_retry is True
    assert decision.was_sanitized is True
    assert "真正回答" in decision.text
    for term in UNSAFE_TERMS:
        assert term not in decision.text
