import pytest


@pytest.mark.asyncio
async def test_admin_hermes_runtime_health_reports_pinned_source(monkeypatch):
    from app.admin.routes import api_hermes_runtime_health
    from app.hermes_runtime.worker import UPSTREAM_HERMES_SHA

    monkeypatch.setenv("HERMES_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("HERMES_RUNTIME_SOURCE_SHA", UPSTREAM_HERMES_SHA)

    response = await api_hermes_runtime_health(_token="test-token")

    assert response["source_sha"] == UPSTREAM_HERMES_SHA
    assert response["enabled"] is True


@pytest.mark.asyncio
async def test_admin_hermes_runtime_health_uses_configured_sidecar(monkeypatch):
    import httpx

    from app.admin.routes import api_hermes_runtime_health

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def get(self, path):
            assert path == "/health"
            return httpx.Response(
                200,
                json={"status": "ok", "source_sha": "sidecar-sha"},
                request=httpx.Request("GET", "http://hermes.test/health"),
            )

    monkeypatch.setenv("HERMES_RUNTIME_URL", "http://hermes.test")
    monkeypatch.setattr(httpx, "Client", FakeClient)

    response = await api_hermes_runtime_health(_token="test-token")

    assert response["enabled"] is True
    assert response["sidecar_status"] == "ok"
    assert response["sidecar_url"] == "http://hermes.test"
    assert response["source_sha"] == "sidecar-sha"


@pytest.mark.asyncio
async def test_admin_hermes_runtime_health_reports_last_sidecar_error(monkeypatch):
    from app.admin.routes import api_hermes_runtime_health
    from app.hermes_runtime.observability import record_runtime_health

    monkeypatch.delenv("HERMES_RUNTIME_EXECUTE_LOCAL", raising=False)
    monkeypatch.setenv("HERMES_RUNTIME_ENABLED", "1")
    record_runtime_health(success=False, error="runtime_unavailable")

    response = await api_hermes_runtime_health(_token="test-token")

    assert response["sidecar_status"] == "error"
    assert response["last_error"] == "runtime_unavailable"
