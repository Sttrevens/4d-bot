def test_skill_bridge_returns_activation_card(monkeypatch):
    from app.hermes_runtime.skills_bridge import build_skill_activation_context

    monkeypatch.setattr(
        "app.tools.skill_engine.load_triggered_skills",
        lambda tenant_id, text: (
            "\n<skill name=\"guizang-ppt-skill\" type=\"repo\">\nRepo 型 skill 已激活\n</skill>",
            [],
            {},
        ),
    )

    context = build_skill_activation_context("pm-bot", "帮我做一份瑞士风 PPT")

    assert "guizang-ppt-skill" in context
    assert "Repo 型 skill 已激活" in context


def test_skill_bridge_normalizes_repo_activation_to_short_hermes_card(monkeypatch):
    from app.hermes_runtime.skills_bridge import build_skill_activation_context

    monkeypatch.setattr(
        "app.tools.skill_engine.load_triggered_skills",
        lambda tenant_id, text: (
            """
## 已激活技能

<skill name="guizang-ppt-skill" type="repo">
Repo 型 skill 已激活：生成横向翻页网页 PPT，提供瑞士国际主义和电子杂志风。
来源: github:op7418/guizang-ppt-skill
可用文件: SKILL.md, assets/template-swiss.html, references/layouts-swiss.md
Long instructions that should stay in SKILL.md, not in runtime context.
</skill>
""",
            [],
            {},
        ),
    )

    context = build_skill_activation_context("pm-bot", "帮我做一份瑞士风 PPT")

    assert '<skill-activation name="guizang-ppt-skill" type="repo">' in context
    assert "<summary>生成横向翻页网页 PPT" in context
    assert "<available-files count=\"3\">" in context
    assert "read_agent_skill_file" in context
    assert "Long instructions" not in context


def test_skill_bridge_appends_activation_to_runtime_request_without_mutation(monkeypatch):
    from app.hermes_runtime.skills_bridge import append_skill_activation_context
    from app.hermes_runtime.types import RuntimeConversation, RuntimeInput, RuntimeRequest, RuntimeSender

    monkeypatch.setattr(
        "app.tools.skill_engine.load_triggered_skills",
        lambda tenant_id, text: (
            """
<skill name="guizang-ppt-skill" type="repo">
Repo 型 skill 已激活：生成横向翻页网页 PPT。
可用文件: SKILL.md
</skill>
""",
            [],
            {},
        ),
    )
    request = RuntimeRequest(
        run_id="run-skill",
        tenant_id="pm-bot",
        channel_id="pm-bot-feishu",
        platform="feishu",
        sender=RuntimeSender(sender_id="ou_1"),
        conversation=RuntimeConversation(history_key="ou_1"),
        input=RuntimeInput(text="帮我做一份瑞士风 PPT", chat_context="existing context"),
    )

    updated = append_skill_activation_context(request)

    assert request.input.chat_context == "existing context"
    assert updated.input.chat_context.startswith("existing context\n\n<skill-activation")
    assert "guizang-ppt-skill" in updated.input.chat_context


def test_skill_bridge_detects_repo_skill_context():
    from app.hermes_runtime.skills_bridge import is_repo_skill_activation

    assert is_repo_skill_activation('<skill name="x" type="repo">hello</skill>')
    assert not is_repo_skill_activation('<skill name="x">hello</skill>')
