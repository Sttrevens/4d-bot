from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

RuntimeName = Literal["legacy", "legacy_shadow_hermes", "hermes_sidecar"]


@dataclass(slots=True)
class RuntimeSender:
    sender_id: str
    sender_name: str = ""
    identity_id: str = ""


@dataclass(slots=True)
class RuntimeConversation:
    history_key: str
    chat_id: str = ""
    chat_type: str = ""
    mode: str = "safe"
    fresh_start: bool = False


@dataclass(slots=True)
class RuntimeInput:
    text: str
    image_urls: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    chat_context: str = ""


@dataclass(slots=True)
class RuntimePolicy:
    allowed_tool_names: list[str] = field(default_factory=list)
    allowed_tool_groups: list[str] = field(default_factory=list)
    admin: bool = False
    self_iteration_enabled: bool = False
    side_effect_budget: str = "normal"
    requires_confirmation_for: list[str] = field(default_factory=list)
    shadow_mode: bool = False


@dataclass(slots=True)
class RuntimeOptions:
    profile: str = "default"
    max_rounds: int = 12
    max_tool_result_chars: int = 20000
    context_strategy: str = "hermes_compressor"
    memory_enabled: bool = True
    skills_enabled: bool = True
    mcp_enabled: bool = False
    code_execution_backend: str = "none"


@dataclass(slots=True)
class RuntimeArtifact:
    artifact_id: str
    kind: str
    filename: str = ""
    delivery_hint: str = ""


@dataclass(slots=True)
class RuntimeToolCallRecord:
    tool_name: str
    status: str
    duration_ms: int = 0
    side_effect: bool = False
    code: str = ""


@dataclass(slots=True)
class RuntimeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    tool_calls: int = 0


@dataclass(slots=True)
class RuntimeErrorInfo:
    code: str
    message: str
    retryable: bool = False


@dataclass(slots=True)
class RuntimeResponse:
    run_id: str
    runtime: str = "hermes_sidecar"
    status: str = "completed"
    final_text: str = ""
    artifacts: list[RuntimeArtifact] = field(default_factory=list)
    tool_calls: list[RuntimeToolCallRecord] = field(default_factory=list)
    usage: RuntimeUsage = field(default_factory=RuntimeUsage)
    events: list[dict[str, Any]] = field(default_factory=list)
    resume: dict[str, Any] = field(default_factory=lambda: {"resumable": False, "resume_token": ""})
    error: RuntimeErrorInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def failed(cls, run_id: str, code: str, message: str, *, retryable: bool = False) -> "RuntimeResponse":
        return cls(
            run_id=run_id,
            status="failed",
            error=RuntimeErrorInfo(code=code, message=message, retryable=retryable),
        )


@dataclass(slots=True)
class RuntimeRequest:
    run_id: str
    tenant_id: str
    channel_id: str
    platform: str
    sender: RuntimeSender
    conversation: RuntimeConversation
    input: RuntimeInput
    policy: RuntimePolicy = field(default_factory=RuntimePolicy)
    runtime: RuntimeOptions = field(default_factory=RuntimeOptions)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeRequest":
        return cls(
            run_id=str(data.get("run_id", "")),
            tenant_id=str(data.get("tenant_id", "")),
            channel_id=str(data.get("channel_id", "")),
            platform=str(data.get("platform", "")),
            sender=RuntimeSender(**dict(data.get("sender") or {})),
            conversation=RuntimeConversation(**dict(data.get("conversation") or {})),
            input=RuntimeInput(**dict(data.get("input") or {})),
            policy=RuntimePolicy(**dict(data.get("policy") or {})),
            runtime=RuntimeOptions(**dict(data.get("runtime") or {})),
        )
