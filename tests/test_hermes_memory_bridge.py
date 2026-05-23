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
