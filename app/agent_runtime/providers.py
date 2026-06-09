from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeProviderManifest:
    name: str
    label: str
    description: str
    default_command: str = ""
    default_sandbox: str = "read-only"
    supports_streaming: bool = False
    supports_file_read: bool = False
    supports_file_write: bool = False
    supports_shell: bool = False
    supports_tools: bool = False
    supports_mcp: bool = False
    supports_sessions: bool = False
    requires_explicit_enable: bool = True
    safety_note: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "default_command": self.default_command,
            "default_sandbox": self.default_sandbox,
            "supports_streaming": self.supports_streaming,
            "supports_file_read": self.supports_file_read,
            "supports_file_write": self.supports_file_write,
            "supports_shell": self.supports_shell,
            "supports_tools": self.supports_tools,
            "supports_mcp": self.supports_mcp,
            "supports_sessions": self.supports_sessions,
            "requires_explicit_enable": self.requires_explicit_enable,
            "safety_note": self.safety_note,
        }


_PROVIDERS = {
    "legacy": RuntimeProviderManifest(
        name="legacy",
        label="Legacy built-in provider",
        description="Use the existing in-process Gemini/OpenAI-compatible bot path.",
        requires_explicit_enable=False,
    ),
    "hermes_sidecar": RuntimeProviderManifest(
        name="hermes_sidecar",
        label="Hermes sidecar",
        description="Use the Hermes-compatible runtime contract over an in-process worker or HTTP sidecar.",
        supports_streaming=False,
        supports_file_read=True,
        supports_file_write=True,
        supports_shell=True,
        supports_tools=True,
        supports_mcp=True,
        supports_sessions=True,
        safety_note="All tool execution should continue through the 4d-bot bridge and tenant allowlists.",
    ),
    "codex_cli": RuntimeProviderManifest(
        name="codex_cli",
        label="Codex CLI",
        description="Run OpenAI Codex through non-interactive `codex exec --json`.",
        default_command="codex",
        supports_streaming=True,
        supports_file_read=True,
        supports_file_write=True,
        supports_shell=True,
        supports_tools=True,
        supports_mcp=True,
        supports_sessions=True,
        safety_note="Defaults to read-only sandbox; write or full-access modes require explicit opt-in.",
    ),
    "claude_cli": RuntimeProviderManifest(
        name="claude_cli",
        label="Claude CLI",
        description="Run Claude through print mode with stream-json output.",
        default_command="claude",
        supports_streaming=True,
        supports_file_read=True,
        supports_file_write=True,
        supports_shell=True,
        supports_tools=True,
        supports_mcp=True,
        supports_sessions=True,
        safety_note="Defaults to plan permission mode; stronger modes require explicit tenant configuration.",
    ),
    "custom_command": RuntimeProviderManifest(
        name="custom_command",
        label="Custom local command",
        description="Run an operator-supplied command that returns a final response on stdout.",
        supports_streaming=False,
        supports_file_read=True,
        supports_file_write=True,
        supports_shell=True,
        safety_note="Only use commands from a trusted local environment.",
    ),
}


def list_provider_manifests() -> list[RuntimeProviderManifest]:
    return list(_PROVIDERS.values())


def get_provider_manifest(name: str) -> RuntimeProviderManifest | None:
    return _PROVIDERS.get(name)
