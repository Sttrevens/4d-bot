from __future__ import annotations

from types import SimpleNamespace

from app.harness.tool_output_ledger import ToolOutputLedger, format_tool_output_for_model
from app.tools import tool_output_ops


class FakeRedis:
    def __init__(self, *, enabled: bool = True, fail_set: bool = False) -> None:
        self.enabled = enabled
        self.fail_set = fail_set
        self.calls: list[tuple[str, ...]] = []
        self.store: dict[str, str] = {}

    def available(self) -> bool:
        return self.enabled

    def execute(self, *args: str | int):
        parts = tuple(str(arg) for arg in args)
        self.calls.append(parts)
        command = parts[0].upper()
        if command == "SET":
            if self.fail_set:
                return None
            self.store[parts[1]] = parts[2]
            return "OK"
        if command == "GET":
            return self.store.get(parts[1])
        if command == "DEL":
            self.store.pop(parts[1], None)
            return 1
        raise AssertionError(f"unexpected redis command: {parts}")


def test_records_tool_output_with_preview_and_tenant_scope() -> None:
    now = [1000.0]
    ledger = ToolOutputLedger(ttl_seconds=60, preview_chars=24, clock=lambda: now[0])

    record = ledger.record(
        tenant_id="tenant-a",
        tool_name="search",
        content="First line\nsecond line with more content",
    )

    assert record.tenant_id == "tenant-a"
    assert record.tool_name == "search"
    assert record.preview == "First line second line..."
    assert record.size == len("First line\nsecond line with more content")
    assert ledger.read("tenant-a", record.output_id).content == record.content
    assert ledger.read("tenant-b", record.output_id) is None


def test_memory_ledger_expires_records_after_ttl() -> None:
    now = [1000.0]
    ledger = ToolOutputLedger(ttl_seconds=10, clock=lambda: now[0])
    record = ledger.record(tenant_id="tenant-a", tool_name="slow_tool", content="payload")

    now[0] = 1009.0
    assert ledger.read("tenant-a", record.output_id) is not None

    now[0] = 1011.0
    assert ledger.read("tenant-a", record.output_id) is None


def test_redis_backend_stores_json_with_expiry_and_reads_it_back() -> None:
    redis = FakeRedis()
    ledger = ToolOutputLedger(ttl_seconds=30, redis_client=redis, clock=lambda: 2000.0)

    record = ledger.record(tenant_id="tenant-a", tool_name="search", content="redis payload")
    ledger.clear_memory()

    assert ("SET", f"tool_output:tenant-a:{record.output_id}", redis.store[f"tool_output:tenant-a:{record.output_id}"], "EX", "30") in redis.calls
    loaded = ledger.read("tenant-a", record.output_id)
    assert loaded is not None
    assert loaded.content == "redis payload"
    assert loaded.preview == "redis payload"


def test_redis_write_failure_falls_back_to_memory() -> None:
    redis = FakeRedis(fail_set=True)
    ledger = ToolOutputLedger(ttl_seconds=30, redis_client=redis, clock=lambda: 3000.0)

    record = ledger.record(tenant_id="tenant-a", tool_name="search", content="memory payload")

    assert ledger.read("tenant-a", record.output_id).content == "memory payload"


def test_read_tool_output_uses_current_tenant(monkeypatch) -> None:
    ledger = ToolOutputLedger(ttl_seconds=30, clock=lambda: 4000.0)
    record = ledger.record(tenant_id="tenant-a", tool_name="search", content="full output")
    monkeypatch.setattr(tool_output_ops, "get_tool_output_ledger", lambda: ledger)
    monkeypatch.setattr(
        tool_output_ops,
        "get_current_tenant",
        lambda: SimpleNamespace(tenant_id="tenant-a"),
    )

    result = tool_output_ops.read_tool_output({"output_id": record.output_id})

    assert result.ok is True
    assert result.content == "full output"


def test_read_tool_output_rejects_missing_and_cross_tenant_records(monkeypatch) -> None:
    ledger = ToolOutputLedger(ttl_seconds=30, clock=lambda: 5000.0)
    record = ledger.record(tenant_id="tenant-a", tool_name="search", content="secret")
    monkeypatch.setattr(tool_output_ops, "get_tool_output_ledger", lambda: ledger)
    monkeypatch.setattr(
        tool_output_ops,
        "get_current_tenant",
        lambda: SimpleNamespace(tenant_id="tenant-b"),
    )

    missing_param = tool_output_ops.read_tool_output({})
    cross_tenant = tool_output_ops.read_tool_output({"output_id": record.output_id})

    assert missing_param.ok is False
    assert missing_param.code == "invalid_param"
    assert cross_tenant.ok is False
    assert cross_tenant.code == "not_found"
    assert "not found" in cross_tenant.content


def test_tool_registration_exports_read_tool_output() -> None:
    assert tool_output_ops.TOOL_MAP["read_tool_output"] is tool_output_ops.read_tool_output
    definition = tool_output_ops.TOOL_DEFINITIONS[0]
    assert definition["name"] == "read_tool_output"
    assert definition["input_schema"]["required"] == ["output_id"]


def test_format_tool_output_for_model_stores_large_outputs_and_keeps_ledger_id_first() -> None:
    ledger = ToolOutputLedger(ttl_seconds=30, preview_chars=12, clock=lambda: 6000.0)

    rendered = format_tool_output_for_model(
        tenant_id="tenant-a",
        tool_name="search",
        content="alpha beta gamma delta epsilon",
        max_inline_chars=12,
        ledger=ledger,
    )

    first_line = rendered.splitlines()[0]
    assert first_line.startswith("[tool_output_stored output_id=")
    output_id = first_line.split("output_id=", 1)[1].split("]", 1)[0]
    assert ledger.read("tenant-a", output_id).content == "alpha beta gamma delta epsilon"
    assert "read_tool_output" in rendered
    assert "alpha beta..." in rendered


def test_format_tool_output_for_model_leaves_small_outputs_inline() -> None:
    ledger = ToolOutputLedger(ttl_seconds=30)

    rendered = format_tool_output_for_model(
        tenant_id="tenant-a",
        tool_name="search",
        content="short",
        max_inline_chars=12,
        ledger=ledger,
    )

    assert rendered == "short"
