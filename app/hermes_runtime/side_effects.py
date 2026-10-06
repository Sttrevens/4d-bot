from __future__ import annotations

from dataclasses import dataclass

from app.hermes_runtime.tool_policy import side_effect_class_for_tool


@dataclass(frozen=True, slots=True)
class SideEffect:
    side_effect_class: str
    side_effect: bool
    requires_checkpoint: bool = False
    rollback_available: bool = False
    user_visible: bool = False


def classify_side_effect(tool_name: str, args: dict | None = None) -> SideEffect:
    _ = args or {}
    cls = side_effect_class_for_tool(tool_name)
    if cls == "read_only":
        return SideEffect(cls, side_effect=False)
    if cls == "message_send":
        return SideEffect(cls, side_effect=True, rollback_available=False, user_visible=True)
    if cls == "code_mutation":
        return SideEffect(cls, side_effect=True, requires_checkpoint=True, rollback_available=True)
    if cls == "infrastructure":
        return SideEffect(cls, side_effect=True, requires_checkpoint=True, rollback_available=True)
    if cls == "platform_write":
        return SideEffect(cls, side_effect=True, rollback_available=True)
    return SideEffect("unknown", side_effect=False)
