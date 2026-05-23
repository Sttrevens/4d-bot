def test_runtime_event_redacts_secrets():
    from app.hermes_runtime.events import make_runtime_event

    event = make_runtime_event(
        run_id="run-1",
        tenant_id="pm-bot",
        event="runtime.model.selected",
        payload={"api_key": "secret-key", "model": "gemini"},
    )

    assert event.payload["api_key"] == "[REDACTED]"
    assert "secret-key" not in str(event.to_dict())


def test_observability_run_summary_is_tenant_scoped():
    from app.hermes_runtime.observability import run_summary_key

    assert run_summary_key("pm-bot", "run-1") == "pm-bot:runtime:hermes:run:run-1"


def test_runtime_event_includes_rollout_debug_dimensions():
    from app.hermes_runtime.events import make_runtime_event

    event = make_runtime_event(
        run_id="run-1",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        session_id="hr:pm-bot:pm-bot-feishu:dm:u1:self",
        platform="feishu",
        event="runtime.tool.completed",
        tool="export_file",
        provider="gemini",
        error="",
        cost={"input_tokens": 10, "output_tokens": 3},
    )

    data = event.to_dict()

    assert data["run_id"] == "run-1"
    assert data["tenant_id"] == "pm-bot"
    assert data["channel_id"] == "pm-bot-feishu"
    assert data["session_id"] == "hr:pm-bot:pm-bot-feishu:dm:u1:self"
    assert data["platform"] == "feishu"
    assert data["tool"] == "export_file"
    assert data["provider"] == "gemini"
    assert data["error"] == ""
    assert data["cost"] == {"input_tokens": 10, "output_tokens": 3}


def test_record_runtime_event_updates_health_and_run_summary(monkeypatch):
    from app.hermes_runtime.events import make_runtime_event
    from app.hermes_runtime.observability import (
        event_stream_key,
        health_state,
        record_runtime_event,
        run_summary_key,
    )

    calls = []
    monkeypatch.setattr(
        "app.hermes_runtime.observability._redis_execute",
        lambda *args: calls.append(args) or "OK",
    )

    event = make_runtime_event(
        run_id="run-1",
        tenant_id="pm-bot",
        platform="feishu",
        event="runtime.failed",
        error="runtime_unavailable",
    )

    record_runtime_event(event)

    assert any(call[:2] == ("RPUSH", event_stream_key("pm-bot", "run-1")) for call in calls)
    assert any(call[:2] == ("SET", run_summary_key("pm-bot", "run-1")) for call in calls)
    state = health_state()
    assert state["last_error"] == "runtime_unavailable"
