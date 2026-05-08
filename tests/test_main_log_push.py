import asyncio
import json

import pytest


@pytest.fixture(autouse=True)
def _local_bot_log(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_LOG_FILE", str(tmp_path / "bot.log"))


def test_log_cache_targets_only_physical_tenants(monkeypatch, tmp_path):
    from app import main

    tenants_file = tmp_path / "tenants.json"
    tenants_file.write_text(
        json.dumps({"tenants": [{"tenant_id": "local-bot"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TENANTS_CONFIG_PATH", str(tenants_file))

    hot_loaded = ["local-bot"] + [f"remote-{i}" for i in range(20)]
    local_tids = main._local_log_cache_tenant_ids(hot_loaded)
    commands = main._build_log_cache_commands(
        [
            "2026-05-03 [INFO] system startup",
            "2026-05-03 [INFO] tenant=remote-1 should not create its own cache key",
            "2026-05-03 [INFO] tenant=local-bot hello",
        ],
        local_tids,
        now=1777815000.0,
    )

    assert local_tids == ["local-bot"]
    assert [cmd[1] for cmd in commands] == ["logs:local-bot"]


def test_log_cache_payload_is_cropped_to_limit_and_keeps_newest_lines():
    from app import main

    lines = [f"old-{i} " + ("x" * 220) for i in range(20)]
    lines.append("newest sentinel")

    commands = main._build_log_cache_commands(
        lines,
        ["local-bot"],
        now=1777815000.0,
        max_lines=100,
        max_payload_bytes=850,
    )
    payload = json.loads(commands[0][2])

    assert len(commands[0][2].encode("utf-8")) <= 850
    assert payload["lines"][-1] == "newest sentinel"
    assert payload["truncated"] is True
    assert payload["count"] == len(payload["lines"])


def test_log_cache_commands_are_chunked_for_small_redis_pipelines():
    from app import main

    commands = [["SET", f"logs:t{i}", "{}", "EX", "120"] for i in range(5)]

    assert [len(batch) for batch in main._chunk_log_cache_commands(commands, batch_size=2)] == [2, 2, 1]


def test_health_diagnostics_uses_redis_client_module(monkeypatch):
    from app import main
    from app.services import redis_client

    monkeypatch.setattr(redis_client, "available", lambda: True)
    monkeypatch.setattr(redis_client, "execute", lambda *args: "PONG" if args == ("PING",) else None)

    result = asyncio.run(main.health_diagnostics())

    assert result["checks"]["redis"] == {"status": "ok", "response": "PONG"}
