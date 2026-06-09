from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

RuntimeName = Literal[
    "legacy",
    "legacy_shadow_hermes",
    "legacy_shadow_codex_cli",
    "legacy_shadow_claude_cli",
    "legacy_shadow_custom_command",
    "hermes_sidecar",
    "codex_cli",
    "claude_cli",
    "custom_command",
]


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _dataclass_kwargs(cls, data: Any, *, aliases: dict[str, str] | None = None) -> dict[str, Any]:
    source = _dict(data)
    aliases = aliases or {}
    names = {item.name for item in fields(cls)}
    kwargs = {name: source[name] for name in names if name in source}
    for source_name, target_name in aliases.items():
        if target_name in names and target_name not in kwargs and source_name in source:
            kwargs[target_name] = source[source_name]
    return kwargs


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
    provider: str = "hermes_sidecar"
    profile: str = "default"
    max_rounds: int = 12
    max_tool_result_chars: int = 20000
    context_strategy: str = "hermes_compressor"
    memory_enabled: bool = True
    skills_enabled: bool = True
    mcp_enabled: bool = False
    code_execution_backend: str = "none"
    command: str = ""
    args: list[str] = field(default_factory=list)
    workspace: str = ""
    sandbox: str = "read-only"
    permission_mode: str = "plan"
    auto_execute: bool = False
    model: str = ""


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
    def failed(
        cls,
        run_id: str,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        runtime: str = "hermes_sidecar",
    ) -> "RuntimeResponse":
        return cls(
            run_id=run_id,
            runtime=runtime,
            status="failed",
            error=RuntimeErrorInfo(code=code, message=message, retryable=retryable),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeResponse":
        payload = _dict(data)
        usage = RuntimeUsage(**_dataclass_kwargs(RuntimeUsage, payload.get("usage")))
        error_raw = _dict(payload.get("error"))
        return cls(
            run_id=str(payload.get("run_id", "")),
            runtime=str(payload.get("runtime") or "hermes_sidecar"),
            status=str(payload.get("status") or "completed"),
            final_text=str(payload.get("final_text") or ""),
            artifacts=[
                RuntimeArtifact(**_dataclass_kwargs(RuntimeArtifact, item))
                for item in _list(payload.get("artifacts"))
                if isinstance(item, dict)
            ],
            tool_calls=[
                RuntimeToolCallRecord(
                    **_dataclass_kwargs(RuntimeToolCallRecord, item, aliases={"name": "tool_name"})
                )
                for item in _list(payload.get("tool_calls"))
                if isinstance(item, dict)
            ],
            usage=usage,
            events=[item for item in _list(payload.get("events")) if isinstance(item, dict)],
            resume=_dict(payload.get("resume")) or {"resumable": False, "resume_token": ""},
            error=RuntimeErrorInfo(**_dataclass_kwargs(RuntimeErrorInfo, error_raw)) if error_raw else None,
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
        payload = _dict(data)
        return cls(
            run_id=str(payload.get("run_id", "")),
            tenant_id=str(payload.get("tenant_id", "")),
            channel_id=str(payload.get("channel_id", "")),
            platform=str(payload.get("platform", "")),
            sender=RuntimeSender(**_dataclass_kwargs(RuntimeSender, payload.get("sender"))),
            conversation=RuntimeConversation(**_dataclass_kwargs(RuntimeConversation, payload.get("conversation"))),
            input=RuntimeInput(**_dataclass_kwargs(RuntimeInput, payload.get("input"))),
            policy=RuntimePolicy(**_dataclass_kwargs(RuntimePolicy, payload.get("policy"))),
            runtime=RuntimeOptions(**_dataclass_kwargs(RuntimeOptions, payload.get("runtime"))),
        )
