import json

import pytest


@pytest.mark.asyncio
async def test_instance_logs_uses_full_source_when_redis_snapshot_is_short(monkeypatch):
    from app.admin import routes
    from app.tenant.registry import tenant_registry
    from app.services import provisioner

    cached_payload = json.dumps({
        "lines": ["redis-1", "redis-2", "redis-3"],
        "count": 3,
    })
    full_lines = [f"full-{i}" for i in range(20)]
    provisioner_calls: list[tuple[str, int]] = []

    monkeypatch.setattr(tenant_registry, "get", lambda _tenant_id: None)
    monkeypatch.setattr(routes.redis, "available", lambda: True)
    monkeypatch.setattr(routes.redis, "execute", lambda *args: cached_payload if args[:2] == ("GET", "logs:kf-steven-ai") else None)

    def fake_get_instance_logs(tenant_id: str, lines: int = 200, **_kwargs):
        provisioner_calls.append((tenant_id, lines))
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "container": "bot-kf-steven-ai",
            "log_source": "http:8103",
            "buffer_size": 20000,
            "total_lines": len(full_lines),
            "logs": "\n".join(full_lines),
            "error_count": 0,
            "recent_errors": "",
        }

    monkeypatch.setattr(provisioner, "get_instance_logs", fake_get_instance_logs)

    response = await routes.api_instance_logs("kf-steven-ai", lines=20, _token="test-token")
    data = json.loads(response.body)

    assert provisioner_calls == [("kf-steven-ai", 20)]
    assert data["log_source"] == "http:8103"
    assert data["total_lines"] == 20
    assert data["logs"].splitlines() == full_lines


@pytest.mark.asyncio
async def test_instance_logs_do_not_treat_synced_tenant_registry_as_local(monkeypatch):
    from types import SimpleNamespace

    from app.admin import routes
    from app.tenant.registry import tenant_registry
    from app.services import provisioner

    full_lines = ["remote-kf-line"]

    async def fail_if_self_logs_used(**_kwargs):
        raise AssertionError("synced tenant registry entry must not be treated as local logs")

    def fake_get_instance_logs(tenant_id: str, lines: int = 200, **_kwargs):
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "container": "bot-kf-steven-ai",
            "log_source": "http:8103",
            "buffer_size": 20000,
            "total_lines": len(full_lines),
            "logs": "\n".join(full_lines),
            "error_count": 0,
            "recent_errors": "",
        }

    monkeypatch.setattr(tenant_registry, "get", lambda _tenant_id: SimpleNamespace(tenant_id="kf-steven-ai"))
    monkeypatch.setattr(provisioner._registry, "get", lambda _tenant_id: SimpleNamespace(port=8103))
    monkeypatch.setattr(routes, "api_self_logs", fail_if_self_logs_used)
    monkeypatch.setattr(routes.redis, "available", lambda: False)
    monkeypatch.setattr(provisioner, "get_instance_logs", fake_get_instance_logs)

    response = await routes.api_instance_logs("kf-steven-ai", lines=20, _token="test-token")
    data = json.loads(response.body)

    assert data["log_source"] == "http:8103"
    assert data["logs"] == "remote-kf-line"
