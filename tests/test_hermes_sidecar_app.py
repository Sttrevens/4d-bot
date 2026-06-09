from fastapi.testclient import TestClient


def test_sidecar_app_exposes_health_with_pinned_source():
    from app.hermes_runtime.sidecar_app import create_app
    from app.hermes_runtime.worker import UPSTREAM_HERMES_SHA

    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["source_sha"] == UPSTREAM_HERMES_SHA


def test_sidecar_app_runs_serialized_turn_through_worker():
    from app.hermes_runtime.sidecar_app import create_app
    from app.hermes_runtime.types import RuntimeResponse

    async def fake_worker(request):
        return RuntimeResponse(
            run_id=request.run_id,
            status="completed",
            final_text=f"echo:{request.input.text}",
        )

    client = TestClient(create_app(worker=fake_worker))

    response = client.post(
        "/v1/runtime/turn",
        json={
            "run_id": "run-sidecar-app",
            "tenant_id": "pm-bot",
            "channel_id": "pm-bot-feishu",
            "platform": "feishu",
            "sender": {"sender_id": "u1"},
            "conversation": {"history_key": "u1"},
            "input": {"text": "hello"},
        },
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-sidecar-app"
    assert response.json()["status"] == "completed"
    assert response.json()["final_text"] == "echo:hello"
