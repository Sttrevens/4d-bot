from __future__ import annotations

from google.genai import types

from app.harness.control_plane import ControlEvent
from app.services.gemini_provider import _append_gemini_control_event


def test_gemini_control_adapter_does_not_append_failed_candidate_reply():
    contents: list[types.Content] = []
    failed_candidate = types.Content(
        role="model",
        parts=[types.Part(text="证据账本 grounding 中立裁判 CONTACT_ENRICHMENT_GATE")],
    )
    event = ControlEvent(
        kind="public_output_scrub",
        reason_code="internal_control_leak",
        action="rewrite_public_reply",
        audit_summary="证据账本 grounding 中立裁判 CONTACT_ENRICHMENT_GATE",
    )

    _append_gemini_control_event(
        contents,
        event,
        channel_platform="qq",
        available_tools={"web_search"},
        failed_content=failed_candidate,
    )

    assert contents == [contents[0]]
    assert contents[0] is not failed_candidate
    assert contents[0].role == "user"
    text = contents[0].parts[0].text
    assert "证据账本" not in text
    assert "grounding" not in text.lower()
    assert "中立裁判" not in text
    assert "CONTACT_ENRICHMENT_GATE" not in text
