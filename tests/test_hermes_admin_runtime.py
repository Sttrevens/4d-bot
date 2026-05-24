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
async def test_admin_hermes_runtime_health_reports_last_sidecar_error(monkeypatch):
    from app.admin.routes import api_hermes_runtime_health
    from app.hermes_runtime.observability import record_runtime_health

    monkeypatch.delenv("HERMES_RUNTIME_EXECUTE_LOCAL", raising=False)
    monkeypatch.setenv("HERMES_RUNTIME_ENABLED", "1")
    record_runtime_health(success=False, error="runtime_unavailable")

    response = await api_hermes_runtime_health(_token="test-token")

    assert response["sidecar_status"] == "error"
    assert response["last_error"] == "runtime_unavailable"


@pytest.mark.asyncio
async def test_admin_hermes_runtime_health_uses_configured_sidecar_url(monkeypatch):
    from app.admin.routes import api_hermes_runtime_health

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return (
                b'{"enabled": true, "source_sha": "sidecar-sha", '
                b'"sidecar_status": "ok", "last_success_at": "2026-05-24T01:00:00Z"}'
            )

    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return FakeResponse()

    monkeypatch.setenv("HERMES_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("HERMES_RUNTIME_URL", "http://127.0.0.1:8765")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    response = await api_hermes_runtime_health(_token="test-token")

    assert calls == [("http://127.0.0.1:8765/health", 2)]
    assert response["sidecar_status"] == "ok"
    assert response["source_sha"] == "sidecar-sha"
    assert response["sidecar_url"] == "http://127.0.0.1:8765"
