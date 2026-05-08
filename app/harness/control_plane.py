"""Structured control-plane events for agent retry and public-output safety."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import SimpleNamespace
from typing import Any, MutableSequence

try:
    from app.harness.capability_broker import (
        capabilities_for_platform,
        can_run_bitable_flow,
    )
except Exception:  # pragma: no cover - compatibility for sibling repo variants
    def capabilities_for_platform(platform: str):
        _ = platform
        return None

    def can_run_bitable_flow(**kwargs):
        _ = kwargs
        return SimpleNamespace(allowed=True, reason="")

try:
    from app.harness.object_binding import ObjectBindingPolicy
except Exception:  # pragma: no cover - compatibility for sibling repo variants
    ObjectBindingPolicy = None


_BITABLE_DELIVERABLE_TOOLS = frozenset({
    "list_bitable_tables",
    "list_bitable_fields",
    "create_bitable_table",
    "create_bitable_field",
    "create_bitable_record",
    "batch_create_bitable_records",
    "update_bitable_record",
})

_INTERNAL_CONTROL_RE = re.compile(
    r"(证据账本|grounding|中立裁判|CONTACT_ENRICHMENT_GATE|<tools_used>|</tools_used>|"
    r"<execute_tool>|</execute_tool>|verdict=|reason=deterministic|deterministic\.|"
    r"llm\.nudge|sub_agent|run_id|traceback)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ControlEvent:
    kind: str
    reason_code: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    audit_summary: str = ""
    max_retries: int = 1


@dataclass(frozen=True)
class ControlDecision:
    verdict: str
    reason: str
    event: ControlEvent | None = None
    reply_override: str | None = None


@dataclass(frozen=True)
class PublicOutputDecision:
    text: str
    was_sanitized: bool
    should_retry: bool
    audit_reason: str = ""


def _missing_text(missing: Any) -> str:
    if isinstance(missing, (list, tuple, set, frozenset)):
        items = [str(item).strip() for item in missing if str(item).strip()]
        return "、".join(items)
    return str(missing or "").strip()


def _render_deliverable(event: ControlEvent, available_tools: set[str] | frozenset[str] | None) -> str:
    missing = event.payload.get("missing") or event.payload.get("missing_deliverables") or []
    missing_text = _missing_text(missing) or "交付物"
    has_bitable = "多维表格" in missing_text
    if not has_bitable:
        return f"你还没有生成用户要求的{missing_text}。请立即调用 export_file 或对应工具完成文件生成，不要跳过。"

    tools = set(available_tools if available_tools is not None else event.payload.get("available_tools") or set())
    if tools and not (tools & _BITABLE_DELIVERABLE_TOOLS):
        return (
            f"你还没有生成用户要求的{missing_text}。"
            "当前会话未加载可用的 Bitable 工具，无法执行 Bitable 写操作，"
            "请不要继续重试 Bitable API。"
            "请先向用户明确说明限制，并提供替代交付（create_feishu_doc/export_file），"
            "或请用户切到支持 Bitable 的飞书渠道后再继续。"
        )

    platform = str(event.payload.get("channel_platform") or "").strip()
    if not platform and tools:
        platform = "feishu" if tools & _BITABLE_DELIVERABLE_TOOLS else "unknown"
    capabilities = event.payload.get("channel_capabilities")
    if capabilities is None and platform:
        capabilities = capabilities_for_platform(platform)
    decision = can_run_bitable_flow(
        channel_platform=platform or "unknown",
        capabilities=capabilities,
        available_tools=tools if tools else None,
    )
    if not decision.allowed:
        if "toolset" in decision.reason:
            return (
                f"你还没有生成用户要求的{missing_text}。"
                "未加载可用的 Bitable 工具，请不要继续重试 Bitable API。"
                "请先向用户明确说明限制，并提供替代交付（create_feishu_doc/export_file），"
                "或请用户切到支持 Bitable 的飞书渠道后再继续。"
            )
        return (
            f"你还没有生成用户要求的{missing_text}。"
            "未加载可用的 Bitable 工具，当前会话无法执行 Bitable 写操作，请不要继续重试 Bitable API。"
            "请先向用户明确说明限制，并提供替代交付（create_feishu_doc/export_file），"
            "或请用户切到支持 Bitable 的飞书渠道后再继续。"
        )

    return (
        f"你还没有生成用户要求的{missing_text}。"
        "请优先调用 Bitable 工具完成（如 list_bitable_tables / list_bitable_fields / "
        "create_bitable_table / create_bitable_record / batch_create_bitable_records）；"
        "如果缺少 app_token 或多维表格链接，直接向用户索取。"
        "不要改用 create_feishu_doc 或 export_file 兜底。"
    )


def _render_contact_gate(event: ControlEvent) -> str:
    binding = ObjectBindingPolicy.coerce(event.payload.get("binding_context")) if ObjectBindingPolicy else None
    allowed_entities: list[str] = []
    if binding:
        allowed_entities = [
            str(item).strip()
            for item in (binding.allowed_entities or [])
            if str(item).strip()
        ]
    if not allowed_entities:
        allowed_entities = [
            str(item).strip()
            for item in (event.payload.get("allowed_entities") or [])
            if str(item).strip()
        ]
    allowed = "、".join(allowed_entities) if allowed_entities else "用户绑定的对象"
    return (
        f"请回到用户要求的对象：{allowed}。"
        "最终直接给表格：姓名 | 可核验邮箱 | 来源链接 | 状态 | 备注。"
        "只能填公开页面原文出现的邮箱；找不到就写“未找到可核验公开邮箱”。"
        "不要用流程说明、创建触达项目或后台自动挖掘来代替结果。"
    )


def _render_exit_governor(event: ControlEvent, channel_platform: str = "") -> str:
    reason = event.reason_code
    if reason.endswith("qq_persona") or event.action == "rewrite_for_channel":
        return (
            "上一版回复不适合 QQ 玩家社群。请直接重写给用户看的最终回复，不要解释这条指令。"
            "当前身份：耀西，四缔游戏官方社群运营。面向玩家时只说玩家答疑、活动公告、反馈收集、"
            "社群秩序维护、游戏相关沟通。不要主动提内部团队协作、老板安排、办公工具能力或代码能力。"
            "回复要短、自然，像真人社群运营。"
        )
    if "history_assertion" in reason or event.action == "verify_history":
        return "你在引用用户之前说过的内容。请先查看可用聊天记录来确认原话，再给用户最终答复。"
    if "intermediate" in reason:
        return "上一版不是用户可读的最终答复。请继续完成任务；如果已完成，请直接给用户结论。"
    if event.action == "complete_required_action" or "action_claim" in reason:
        return "上一版包含准备去做或已经做了的语义，但还没有足够执行结果。请直接调用对应工具完成；如果不需要工具，请给出直接答案。"
    if event.action == "verify_evidence" or event.reason_code.endswith("grounding"):
        return "上一版包含需要核验的事实信息。请先调用搜索或读取类工具核验，再基于结果回答；如果没有可靠来源，请明确说无法确认。"
    if reason.startswith("llm."):
        return "上一版没有可靠完成用户意图。请重新检查当前工具结果和用户问题，继续执行必要步骤后再给最终答复。"
    return "请继续完成用户的任务；需要信息就调用工具，已完成就直接给用户最终答复。"


def render_control_text(
    event: ControlEvent,
    *,
    channel_platform: str = "",
    available_tools: set[str] | frozenset[str] | None = None,
) -> str:
    if event.kind == "deliverable":
        text = _render_deliverable(event, available_tools)
    elif event.kind == "contact_enrichment":
        text = _render_contact_gate(event)
    elif event.kind == "unmatched_read":
        text = "你已经读取了内容但还没有执行修改。请调用对应写入工具完成操作，不要只是描述你会怎么改。"
    elif event.kind == "missing_action":
        missing = _missing_text(event.payload.get("missing_actions") or event.payload.get("missing"))
        prefix = f"用户明确要求的动作还没有完成：{missing}。" if missing else "用户明确要求的动作还没有完成。"
        return prefix + "请调用对应工具完成实际操作，完成后再汇报结果。"
    elif event.kind == "empty_retry":
        text = "上一轮没有产生可用回复。请继续执行任务；如果还有操作要做，直接调用工具；如果已完成，用文字告诉用户结果。"
    elif event.kind == "public_output_scrub":
        text = "上一版回复混入了不适合展示给用户的流程内容。请直接重写为用户可读的最终答复，不要提审查流程、控制消息或工具摘要。"
    elif event.kind == "exit_governor":
        text = _render_exit_governor(event, channel_platform)
    else:
        text = "请继续完成用户的任务；需要信息就调用工具，已完成就直接给用户最终答复。"
    return _INTERNAL_CONTROL_RE.sub("", text).strip()


def render_control_event(
    event: ControlEvent,
    *,
    provider: str,
    channel_platform: str = "",
    available_tools: set[str] | frozenset[str] | None = None,
) -> dict[str, str]:
    text = render_control_text(
        event,
        channel_platform=channel_platform,
        available_tools=available_tools,
    )
    if provider.lower() in {"openai", "kimi"}:
        return {"role": "user", "content": text}
    if provider.lower() == "gemini":
        return {"role": "user", "text": text}
    return {"role": "user", "content": text}


def append_openai_control_event(
    messages: MutableSequence[dict],
    event: ControlEvent,
    *,
    channel_platform: str = "",
    available_tools: set[str] | frozenset[str] | None = None,
) -> None:
    messages.append(
        render_control_event(
            event,
            provider="openai",
            channel_platform=channel_platform,
            available_tools=available_tools,
        )
    )


def has_internal_control_leak(text: str) -> bool:
    return bool(text and _INTERNAL_CONTROL_RE.search(text))


def sanitize_public_reply(reply: str, channel_platform: str = "") -> PublicOutputDecision:
    _ = channel_platform
    text = (reply or "").strip()
    if not text:
        return PublicOutputDecision(text="", was_sanitized=False, should_retry=False)
    if not has_internal_control_leak(text):
        return PublicOutputDecision(text=text, was_sanitized=False, should_retry=False)

    safe_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not _INTERNAL_CONTROL_RE.search(line)
    ]
    cleaned = "\n".join(safe_lines).strip()
    if not cleaned:
        cleaned = "我重新整理一下再回答你。"
    return PublicOutputDecision(
        text=cleaned,
        was_sanitized=True,
        should_retry=True,
        audit_reason="internal_control_leak",
    )
