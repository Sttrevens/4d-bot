import asyncio
import json



def test_memory_list_api_filters_and_serializes_entries(monkeypatch):
    from app.admin import routes

    entries = [
        {
            "type": "numeric_fact",
            "user_id": "ou_743c3f5d",
            "user_name": "吴天骄",
            "action": "数字事实: Outbound 首周销量预测 40-60w",
            "tags": ["预测", "数字"],
            "time": "2026-04-18T00:00:00+00:00",
        },
        {
            "user_id": "ou_other",
            "user_name": "Other",
            "action": "普通聊天",
            "tags": ["其他"],
            "time": "2026-05-01T00:00:00+00:00",
        },
    ]

    monkeypatch.setattr(routes.redis, "available", lambda: True)
    monkeypatch.setattr(routes.memory_store, "read_journal_all", lambda: entries)
    monkeypatch.setattr(routes.memory_store, "read_json", lambda key: {"name": "吴天骄"} if key.startswith("users/") else None)

    response = asyncio.run(routes.api_tenant_memory(
        "pm-bot",
        q="outbound",
        user_id="ou_743c3f5d",
        tag="预测",
        type="numeric_fact",
        _token="test-token",
    ))
    data = json.loads(response.body)

    assert data["ok"] is True
    assert data["stats"]["total_entries"] == 2
    assert data["stats"]["matched_entries"] == 1
    assert data["entries"][0]["summary"] == "数字事实: Outbound 首周销量预测 40-60w"
    assert data["profile"]["name"] == "吴天骄"


def test_memory_list_api_returns_clear_unavailable_state(monkeypatch):
    from app.admin import routes

    monkeypatch.setattr(routes.redis, "available", lambda: False)

    response = asyncio.run(routes.api_tenant_memory("pm-bot", _token="test-token"))
    data = json.loads(response.body)

    assert data["ok"] is False
    assert data["error"] == "Redis unavailable"
    assert data["entries"] == []


def test_memory_recall_preview_sets_tenant_context_and_returns_entries(monkeypatch):
    from app.admin import routes

    captured = {}
    entry = {
        "type": "numeric_fact",
        "user_id": "ou_743c3f5d",
        "user_name": "吴天骄",
        "action": "数字事实: Outbound 首周销量预测 40-60w",
        "tags": ["预测", "数字"],
        "time": "2026-04-18T00:00:00+00:00",
    }

    def fake_recall(*, user_id="", keyword="", limit=10, query_text=""):
        captured.update({
            "user_id": user_id,
            "keyword": keyword,
            "limit": limit,
            "query_text": query_text,
        })
        return [entry]

    monkeypatch.setattr(routes.redis, "available", lambda: True)
    monkeypatch.setattr(routes.memory, "recall", fake_recall)
    monkeypatch.setattr(routes.memory, "recall_text", lambda **_kwargs: "formatted recall")

    response = asyncio.run(routes.api_tenant_memory_recall_preview(
        "pm-bot",
        user_id="ou_743c3f5d",
        query_text="你之前猜outbound是40-60w？",
        keyword="outbound",
        _token="test-token",
    ))
    data = json.loads(response.body)

    assert captured["user_id"] == "ou_743c3f5d"
    assert captured["keyword"] == "outbound"
    assert data["formatted"] == "formatted recall"
    assert data["entries"][0]["summary"] == "数字事实: Outbound 首周销量预测 40-60w"


def test_instance_logs_uses_full_source_when_redis_snapshot_is_short(monkeypatch):
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

    response = asyncio.run(routes.api_instance_logs("kf-steven-ai", lines=20, _token="test-token"))
    data = json.loads(response.body)

    assert provisioner_calls == [("kf-steven-ai", 20)]
    assert data["log_source"] == "http:8103"
    assert data["total_lines"] == 20
    assert data["logs"].splitlines() == full_lines


def test_instance_logs_do_not_treat_synced_tenant_registry_as_local(monkeypatch):
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

    response = asyncio.run(routes.api_instance_logs("kf-steven-ai", lines=20, _token="test-token"))
    data = json.loads(response.body)

    assert data["log_source"] == "http:8103"
    assert data["logs"] == "remote-kf-line"


def test_instance_logs_for_co_tenant_use_host_instance_not_self(monkeypatch):
    from types import SimpleNamespace

    from app.admin import routes
    from app.tenant.registry import tenant_registry
    from app.services import provisioner

    async def fail_if_self_logs_used(**_kwargs):
        raise AssertionError("co-hosted tenant logs must be resolved through the host instance")

    def fake_get_instance_logs(tenant_id: str, lines: int = 200, **_kwargs):
        assert tenant_id == "kf-heng"
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "host_tenant_id": "kf-steven-ai",
            "is_co_tenant": True,
            "container": "bot-kf-steven-ai",
            "log_source": "http:8103",
            "buffer_size": 20000,
            "total_lines": 1,
            "logs": "cohost-line",
            "error_count": 0,
            "recent_errors": "",
        }

    monkeypatch.setattr(tenant_registry, "get", lambda _tenant_id: SimpleNamespace(tenant_id="kf-heng"))
    monkeypatch.setattr(provisioner._registry, "get", lambda _tenant_id: None)
    monkeypatch.setattr(provisioner, "find_log_host_instance_id", lambda _tenant_id: "kf-steven-ai")
    monkeypatch.setattr(routes, "api_self_logs", fail_if_self_logs_used)
    monkeypatch.setattr(routes.redis, "available", lambda: False)
    monkeypatch.setattr(provisioner, "get_instance_logs", fake_get_instance_logs)

    response = asyncio.run(routes.api_instance_logs("kf-heng", lines=20, _token="test-token"))
    data = json.loads(response.body)

    assert data["tenant_id"] == "kf-heng"
    assert data["host_tenant_id"] == "kf-steven-ai"
    assert data["logs"] == "cohost-line"
