from __future__ import annotations

from app.hermes_runtime.memory_scope import MemoryScope


class HermesMemoryBridge:
    def __init__(self, scope: MemoryScope) -> None:
        self.scope = scope

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        _ = session_id
        if not self.scope.identity_id:
            return ""
        from app.services import memory

        return await memory.build_memory_context(
            self.scope.identity_id,
            user_name=self.scope.sender_name,
            current_text=query,
        )

    async def sync_turn(self, user_content: str, assistant_content: str, *, tool_names: list[str] | None = None) -> None:
        if not self.scope.identity_id:
            return
        from app.services import memory

        await memory.write_diary(
            self.scope.identity_id,
            self.scope.sender_name,
            user_content,
            assistant_content,
            tool_names_called=tool_names or [],
        )
