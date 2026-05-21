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
