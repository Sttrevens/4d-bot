from __future__ import annotations

import html
import re

from app.hermes_runtime.types import RuntimeRequest


_SKILL_TAG_RE = re.compile(r"<skill(?P<attrs>[^>]*)>(?P<body>.*?)</skill>", re.DOTALL | re.IGNORECASE)
_ATTR_RE = re.compile(r"(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)=[\"'](?P<value>.*?)[\"']")


def _tag_attrs(text: str) -> dict[str, str]:
    return {m.group("name"): m.group("value") for m in _ATTR_RE.finditer(text or "")}


def _line_value(lines: list[str], prefix: str) -> str:
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _repo_skill_summary(lines: list[str]) -> str:
    explicit = _line_value(lines, "Repo 型 skill 已激活：")
    if explicit:
        return explicit
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _normalize_repo_skill_cards(context: str) -> str:
    cards: list[str] = []
    for match in _SKILL_TAG_RE.finditer(context or ""):
        attrs = _tag_attrs(match.group("attrs"))
        if attrs.get("type") != "repo":
            continue
        name = attrs.get("name", "").strip()
        if not name:
            continue
        lines = [line.strip() for line in match.group("body").splitlines() if line.strip()]
        summary = _repo_skill_summary(lines)
        files_text = _line_value(lines, "可用文件:")
        files = [item.strip() for item in files_text.split(",") if item.strip()]
        file_list = ", ".join(files)
        cards.append(
            "\n".join(
                [
                    f'<skill-activation name="{html.escape(name)}" type="repo">',
                    f"  <summary>{html.escape(summary)}</summary>",
                    "  <how-to-use>Use list_agent_skill_files for the manifest, "
                    "read_agent_skill_file for detailed instructions/templates, and "
                    "export_agent_skill_template for exact template exports.</how-to-use>",
                    f'  <available-files count="{len(files)}">{html.escape(file_list)}</available-files>',
                    "</skill-activation>",
                ]
            )
        )
    return "\n\n".join(cards)


def build_skill_activation_context(tenant_id: str, user_text: str) -> str:
    if not tenant_id or not user_text:
        return ""
    from app.tools import skill_engine

    instructions, _, _ = skill_engine.load_triggered_skills(tenant_id, user_text)
    if not instructions:
        return ""
    normalized = _normalize_repo_skill_cards(instructions)
    return normalized or instructions


def append_skill_activation_context(request: RuntimeRequest) -> RuntimeRequest:
    activation = build_skill_activation_context(request.tenant_id, request.input.text)
    if not activation:
        return request
    updated = RuntimeRequest.from_dict(request.to_dict())
    existing = updated.input.chat_context.strip()
    updated.input.chat_context = f"{existing}\n\n{activation}" if existing else activation
    return updated


def is_repo_skill_activation(context: str) -> bool:
    value = context or ""
    return (
        'type="repo"' in value
        or "type='repo'" in value
        or "<skill-activation" in value
    )
