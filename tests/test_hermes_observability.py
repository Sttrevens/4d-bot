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
