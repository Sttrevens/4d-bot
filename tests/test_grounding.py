from app.harness import (
    build_grounding_nudge,
    reply_contains_dense_factual_claims,
    requires_external_grounding,
    should_relax_fact_grounding,
)
from app.services.base_agent import detect_ungrounded_claims


def test_conceptual_question_relaxes_fact_grounding():
    assert should_relax_fact_grounding(
        "我关注的不是宿命论，这两个理念有没有明晰的因果关系？"
    )


def test_conceptual_question_with_persona_names_does_not_trigger_grounding():
    assert (
        detect_ungrounded_claims(
            "我再说一遍：宿命论和世界离散性没有必然因果关系。至于四缔游戏和吴总那些梗先放一边。",
            "我关注的不是宿命论，或者说这些宿命论的哲学家必定会觉得世界是离散的嘛？这两个理念有没有明晰的因果关系呢",
            [],
        )
        is None
    )


def test_pricing_question_requires_grounding_even_without_search_verbs():
    user_text = "我用 codex 两天用了 20 刀周额度的 60%，是开 200 刀套餐还是充 extra 额度？"
    assert requires_external_grounding(user_text)
    nudge = detect_ungrounded_claims(
        "按我估算你应该直接充 extra，200 刀套餐大概不值。",
        user_text,
        [],
    )
    assert nudge is not None
    assert "官方来源" in nudge or "定价" in nudge


def test_dense_factual_claims_still_trigger_on_public_facts():
    assert reply_contains_dense_factual_claims("执行董事：张三，监事：李四，总经理：王五")
    nudge = detect_ungrounded_claims(
        "执行董事：张三，监事：李四，总经理：王五。",
        "这家公司现在的管理层有哪些人？",
        [],
    )
    assert nudge is not None
    assert "公开事实" in nudge or "公开资料" in nudge


def test_pricing_nudge_discourages_guessing():
    nudge = build_grounding_nudge("codex 的 extra 额度怎么收费？200 刀是 20 刀的多少倍？")
    assert "不要猜" in nudge


def test_codex_pricing_nudge_disambiguates_product():
    nudge = build_grounding_nudge("胡扯，codex 怎么可能不公布自己的官方 pricing")
    assert "旧 Codex API 模型" in nudge
    assert "官方" in nudge
