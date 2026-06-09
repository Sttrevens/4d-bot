from __future__ import annotations

from app.hermes_runtime.memory_scope import MemoryScope


class HermesMemoryBridge:
    def __init__(self, scope: MemoryScope, *, run_id: str = "") -> None:
        self.scope = scope
        self.run_id = run_id

    def _record_event(self, event_name: str, payload: dict) -> None:
        if not self.run_id:
            return
        from app.hermes_runtime.events import make_runtime_event
        from app.hermes_runtime import observability

        observability.record_runtime_event(
            make_runtime_event(
                run_id=self.run_id,
                tenant_id=self.scope.tenant_id,
                event=event_name,
                payload=payload,
            )
        )

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        _ = session_id
        if not self.scope.identity_id:
            return ""
        from app.services import memory

        context = await memory.build_memory_context(
            self.scope.identity_id,
            user_name=self.scope.sender_name,
            current_text=query,
        )
        self._record_event(
            "runtime.memory.prefetched",
            {
                "session_id": self.scope.session_id,
                "identity_present": True,
                "context_chars": len(context),
            },
        )
        return context

    async def sync_turn(self, user_content: str, assistant_content: str, *, tool_names: list[str] | None = None) -> None:
        if not self.scope.identity_id:
            return
        from app.services import memory

        tools = tool_names or []
        await memory.write_diary(
            self.scope.identity_id,
            self.scope.sender_name,
            user_content,
            assistant_content,
            tool_names_called=tools,
        )
        self._record_event(
            "runtime.memory.write",
            {
                "session_id": self.scope.session_id,
                "identity_present": True,
                "tool_count": len(tools),
            },
        )
