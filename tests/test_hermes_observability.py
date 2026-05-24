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


def test_load_runtime_events_reads_tenant_scoped_stream(monkeypatch):
    from app.hermes_runtime.observability import load_runtime_events

    calls = []

    def fake_execute(*args):
        calls.append(args)
        return [
            '{"run_id": "run-1", "tenant_id": "pm-bot", "event": "runtime.started"}',
            '{"run_id": "run-1", "tenant_id": "pm-bot", "event": "runtime.completed"}',
        ]

    monkeypatch.setattr("app.services.redis_client.execute", fake_execute)

    events = load_runtime_events("pm-bot", "run-1", limit=2)

    assert calls == [("LRANGE", "pm-bot:runtime:hermes:events:run-1", 0, 1)]
    assert [event["event"] for event in events] == ["runtime.started", "runtime.completed"]
