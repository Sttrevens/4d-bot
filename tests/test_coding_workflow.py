from app.harness.coding_workflow import (
    build_coding_workflow_instructions,
    is_coding_workflow_turn,
    should_clarify_before_coding,
    should_plan_before_coding,
)


def test_complex_code_request_requires_clarify_and_plan():
    user_text = "帮我重构这个 agent harness，把 coding workflow 整体做好"

    assert is_coding_workflow_turn(user_text, ("code_dev",))
    assert should_clarify_before_coding(user_text, ("code_dev",))
    assert should_plan_before_coding(user_text, ("code_dev",))

    instructions = build_coding_workflow_instructions(user_text, ("code_dev",))
    assert "先澄清再写" in instructions
    assert "先 create_plan" in instructions


def test_concrete_bugfix_does_not_force_clarify():
    user_text = "修一下 app/services/gemini_provider.py 这个 timeout bug"

    assert is_coding_workflow_turn(user_text, ("code_dev",))
    assert not should_clarify_before_coding(user_text, ("code_dev",))
    assert not should_plan_before_coding(user_text, ("code_dev",))


def test_runtime_tool_hints_also_enable_coding_workflow():
    user_text = "帮我修这个 deploy bug"
    runtime_hints = ("read_file", "git_status")

    assert is_coding_workflow_turn(user_text, runtime_hints)
    instructions = build_coding_workflow_instructions(user_text, runtime_hints)
    assert "编码工作流" in instructions
