import pytest


def test_memory_scope_uses_hashed_identifiers():
    from app.hermes_runtime.memory_scope import build_session_id

    session_id = build_session_id(
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        scope="dm",
        identity_id="ou_sensitive",
        chat_id="",
    )

    assert session_id.startswith("hr:pm-bot:pm-bot-feishu:dm:")
    assert "ou_sensitive" not in session_id


@pytest.mark.asyncio
async def test_memory_bridge_prefetch_uses_existing_memory_context(monkeypatch):
    from app.hermes_runtime.memory_bridge import HermesMemoryBridge
    from app.hermes_runtime.memory_scope import MemoryScope

    async def fake_build_memory_context(user_id, user_name="", current_text=""):
        return f"memory for {user_id}: {current_text}"

    monkeypatch.setattr("app.services.memory.build_memory_context", fake_build_memory_context)
    bridge = HermesMemoryBridge(
        MemoryScope(
            tenant_id="pm-bot",
            session_id="hr:pm-bot:ch:dm:abc:self",
            identity_id="id-1",
            sender_name="Steven",
        )
    )

    context = await bridge.prefetch("继续刚才的 PPT")

    assert "id-1" in context
    assert "继续刚才的 PPT" in context


@pytest.mark.asyncio
async def test_memory_bridge_emits_prefetch_and_write_events(monkeypatch):
    from app.hermes_runtime.memory_bridge import HermesMemoryBridge
    from app.hermes_runtime.memory_scope import MemoryScope

    recorded = []

    async def fake_build_memory_context(user_id, user_name="", current_text=""):
        return f"context for {user_id}"

    async def fake_write_diary(user_id, user_name, user_content, assistant_content, *, tool_names_called=None):
        return None

    monkeypatch.setattr("app.services.memory.build_memory_context", fake_build_memory_context)
    monkeypatch.setattr("app.services.memory.write_diary", fake_write_diary)
    monkeypatch.setattr("app.hermes_runtime.observability.record_runtime_event", recorded.append)

    bridge = HermesMemoryBridge(
        MemoryScope(
            tenant_id="pm-bot",
            session_id="hr:pm-bot:ch:dm:abc:self",
            identity_id="id-1",
            sender_name="Steven",
            channel_id="ch",
        ),
        run_id="run-1",
    )

    await bridge.prefetch("继续刚才的 PPT")
    await bridge.sync_turn("user text", "assistant text", tool_names=["read_agent_skill_file"])

    assert [event.event for event in recorded] == ["runtime.memory.prefetched", "runtime.memory.write"]
    assert {event.run_id for event in recorded} == {"run-1"}
    assert {event.tenant_id for event in recorded} == {"pm-bot"}
    assert recorded[0].payload == {
        "session_id": "hr:pm-bot:ch:dm:abc:self",
        "identity_present": True,
        "context_chars": len("context for id-1"),
    }
    assert recorded[1].payload == {
        "session_id": "hr:pm-bot:ch:dm:abc:self",
        "identity_present": True,
        "tool_count": 1,
    }
