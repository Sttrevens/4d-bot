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


def test_load_shadow_response_reads_tenant_scoped_result(monkeypatch):
    from app.hermes_runtime.observability import load_shadow_response

    calls = []

    def fake_execute(*args):
        calls.append(args)
        return '{"run_id": "run-1", "status": "completed", "final_text": "shadow answer"}'

    monkeypatch.setattr("app.hermes_runtime.observability._redis_execute", fake_execute)

    response = load_shadow_response("pm-bot", "run-1")

    assert calls == [("GET", "pm-bot:runtime:hermes:shadow:run-1")]
    assert response == {"run_id": "run-1", "status": "completed", "final_text": "shadow answer"}


def test_runtime_event_includes_rollout_debug_dimensions():
    from app.hermes_runtime.events import make_runtime_event

    event = make_runtime_event(
        run_id="run-1",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        session_id="hr:pm-bot:pm-bot-feishu:dm:u1:u1",
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
    assert data["session_id"] == "hr:pm-bot:pm-bot-feishu:dm:u1:u1"
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
        channel_id="pm-bot-feishu",
        session_id="hr:pm-bot:pm-bot-feishu:dm:u1:u1",
        platform="feishu",
        event="runtime.failed",
        error="runtime_unavailable",
    )

    record_runtime_event(event)

    assert any(call[:2] == ("RPUSH", event_stream_key("pm-bot", "run-1")) for call in calls)
    assert any(call[:2] == ("SET", run_summary_key("pm-bot", "run-1")) for call in calls)
    assert health_state()["last_error"] == "runtime_unavailable"


def test_record_runtime_event_indexes_run_summary(monkeypatch):
    from app.hermes_runtime.events import make_runtime_event
    from app.hermes_runtime.observability import (
        RUNTIME_RUN_INDEX_KEY,
        record_runtime_event,
        tenant_run_index_key,
    )

    calls = []
    monkeypatch.setattr(
        "app.hermes_runtime.observability._redis_execute",
        lambda *args: calls.append(args) or "OK",
    )

    record_runtime_event(
        make_runtime_event(
            run_id="run-indexed",
            tenant_id="pm-bot",
            event="runtime.completed",
        )
    )

    assert any(call[:2] == ("ZADD", RUNTIME_RUN_INDEX_KEY) for call in calls)
    assert any(call[:2] == ("ZADD", tenant_run_index_key("pm-bot")) for call in calls)
    assert any(call[:2] == ("EXPIRE", RUNTIME_RUN_INDEX_KEY) for call in calls)


def test_list_runtime_run_summaries_reads_global_and_tenant_indexes(monkeypatch):
    import json

    from app.hermes_runtime import observability

    strings = {
        observability.run_summary_key("pm-bot", "run-1"): json.dumps({"tenant_id": "pm-bot", "run_id": "run-1"}),
        observability.run_summary_key("code-bot", "run-2"): json.dumps({"tenant_id": "code-bot", "run_id": "run-2"}),
    }

    def fake_execute(command, *args):
        if command == "ZREVRANGE" and args[0] == observability.RUNTIME_RUN_INDEX_KEY:
            return ["pm-bot:run-1", b"code-bot:run-2"]
        if command == "ZREVRANGE" and args[0] == observability.tenant_run_index_key("pm-bot"):
            return ["run-1"]
        if command == "GET":
            return strings.get(str(args[0]))
        raise AssertionError(f"unexpected redis command {command}")

    monkeypatch.setattr(observability, "_redis_execute", fake_execute)

    assert [run["run_id"] for run in observability.list_runtime_run_summaries(limit=10)] == ["run-1", "run-2"]
    assert observability.list_runtime_run_summaries("pm-bot", limit=10) == [
        {"tenant_id": "pm-bot", "run_id": "run-1"}
    ]


def test_runtime_run_detail_combines_summary_events_and_shadow(monkeypatch):
    import json

    from app.hermes_runtime import observability
    from app.hermes_runtime.events import make_runtime_event
    from app.hermes_runtime.types import RuntimeResponse

    summary = {"run_id": "run-1", "tenant_id": "pm-bot", "runtime": "hermes_sidecar", "status": "completed"}
    event = make_runtime_event(run_id="run-1", tenant_id="pm-bot", event="runtime.completed").to_dict()
    shadow = RuntimeResponse(run_id="run-1", final_text="shadow answer").to_dict()
    strings = {
        observability.run_summary_key("pm-bot", "run-1"): json.dumps(summary),
        observability.shadow_result_key("pm-bot", "run-1"): json.dumps(shadow),
    }
    lists = {observability.event_stream_key("pm-bot", "run-1"): [json.dumps(event), "not-json"]}

    def fake_execute(command, *args):
        if command == "GET":
            return strings.get(str(args[0]))
        if command == "LRANGE":
            return lists.get(str(args[0]), [])
        raise AssertionError(f"unexpected redis command {command}")

    monkeypatch.setattr(observability, "_redis_execute", fake_execute)

    detail = observability.load_runtime_run_detail("pm-bot", "run-1")

    assert detail["summary"] == summary
    assert detail["events"] == [event]
    assert detail["shadow_response"] == shadow


def test_shadow_qa_report_scores_shadow_runs(monkeypatch):
    from app.hermes_runtime import observability

    summaries_by_tenant = {
        "pm-bot": [
            {"run_id": "run-pass", "tenant_id": "pm-bot", "status": "completed", "shadow_mode": True},
            {"run_id": "run-review", "tenant_id": "pm-bot", "status": "completed", "shadow_mode": True},
            {"run_id": "run-fail", "tenant_id": "pm-bot", "status": "failed", "shadow_mode": True},
            {"run_id": "visible-run", "tenant_id": "pm-bot", "status": "completed", "shadow_mode": False},
        ]
    }
    shadows = {
        ("pm-bot", "run-pass"): {"status": "completed", "final_text": "shadow ok"},
        ("pm-bot", "run-review"): {"status": "completed", "final_text": ""},
        ("pm-bot", "run-fail"): {"status": "failed", "final_text": ""},
    }

    monkeypatch.setattr(
        observability,
        "list_runtime_run_summaries",
        lambda tenant_id, limit=100: summaries_by_tenant.get(tenant_id, []),
    )
    monkeypatch.setattr(
        observability,
        "load_runtime_run_detail",
        lambda tenant_id, run_id: {"shadow_response": shadows.get((tenant_id, run_id))},
    )

    report = observability.build_shadow_qa_report(["pm-bot"])

    assert report["total_shadow_runs"] == 3
    assert report["status_counts"] == {"pass": 1, "review": 1, "fail": 1}
    assert report["tenants"]["pm-bot"]["severe_regression_count"] == 1
    assert [run["qa_status"] for run in report["tenants"]["pm-bot"]["recent_runs"]] == [
        "pass",
        "review",
        "fail",
    ]
