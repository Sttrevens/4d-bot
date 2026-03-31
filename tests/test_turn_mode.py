from app.harness import (
    infer_turn_mode,
    is_non_actionable_turn,
    is_product_pricing_turn,
    sanitize_suggested_groups,
    should_run_code_preflight,
)


def test_analysis_turn_detected():
    mode = infer_turn_mode("有没有哪些哲学家，或者比如教义，也是类似的观点？")
    assert mode.mode in {"analysis", "research"}
    assert "core" in mode.groups


def test_action_turn_detected():
    mode = infer_turn_mode("今晚提醒我报销")
    assert mode.mode == "action"
    assert "feishu_collab" in mode.groups


def test_code_turn_detected():
    mode = infer_turn_mode("帮我看下这个 bug，顺便修一下代码")
    assert mode.mode == "code"
    assert mode.task_type == "deep"
    assert "code_dev" in mode.groups


def test_non_actionable_turn_helper():
    assert is_non_actionable_turn("他的宿命论具体是什么逻辑")
    assert not is_non_actionable_turn("今晚提醒我报销")


def test_pricing_turn_detected_as_research():
    mode = infer_turn_mode("我用 codex 两天用了 20 刀周额度的 60%，是开 200 刀套餐还是充 extra 额度？")
    assert mode.mode == "research"
    assert mode.task_type == "research"


def test_codex_pricing_turn_is_not_treated_as_code_task():
    text = "胡扯，codex 怎么可能不公布自己的官方 pricing"
    assert is_product_pricing_turn(text)
    groups = sanitize_suggested_groups(text, ["core", "code_dev", "research"])
    assert "research" in groups
    assert "code_dev" not in groups
    assert not should_run_code_preflight(text, groups)
