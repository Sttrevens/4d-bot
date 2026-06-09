from __future__ import annotations

from app.agent_runtime.cli_adapters import (
    ClaudeCliRuntimeClient,
    CodexCliRuntimeClient,
    CustomCommandRuntimeClient,
)
from app.hermes_runtime.types import RuntimeRequest, RuntimeResponse


class AgentRuntimeClient:
    def __init__(self, *, provider: str = "", timeout_seconds: int = 180) -> None:
        self.provider = provider
        self.timeout_seconds = timeout_seconds

    async def run_turn(self, request: RuntimeRequest) -> RuntimeResponse:
        provider = self.provider or request.runtime.provider or "hermes_sidecar"
        if provider == "hermes_sidecar":
            from app.hermes_runtime.client import HermesRuntimeClient

            return await HermesRuntimeClient(timeout_seconds=self.timeout_seconds).run_turn(request)
        if provider == "codex_cli":
            return await CodexCliRuntimeClient(timeout_seconds=self.timeout_seconds).run_turn(request)
        if provider == "claude_cli":
            return await ClaudeCliRuntimeClient(timeout_seconds=self.timeout_seconds).run_turn(request)
        if provider == "custom_command":
            return await CustomCommandRuntimeClient(timeout_seconds=self.timeout_seconds).run_turn(request)
        return RuntimeResponse.failed(
            request.run_id,
            "runtime_bad_request",
            f"Unsupported agent runtime provider: {provider}",
            retryable=False,
            runtime=provider,
        )
