from __future__ import annotations


def build_skill_activation_context(tenant_id: str, user_text: str) -> str:
    if not tenant_id or not user_text:
        return ""
    from app.tools import skill_engine

    instructions, _, _ = skill_engine.load_triggered_skills(tenant_id, user_text)
    return instructions or ""


def is_repo_skill_activation(context: str) -> bool:
    return 'type="repo"' in (context or "") or "type='repo'" in (context or "")
