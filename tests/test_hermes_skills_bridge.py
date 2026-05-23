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


def test_skill_bridge_detects_repo_skill_context():
    from app.hermes_runtime.skills_bridge import is_repo_skill_activation

    assert is_repo_skill_activation('<skill name="x" type="repo">hello</skill>')
    assert not is_repo_skill_activation('<skill name="x">hello</skill>')
