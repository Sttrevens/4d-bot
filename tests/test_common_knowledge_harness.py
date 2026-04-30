from app.harness.common_knowledge import classify_common_knowledge_turn
from app.harness.grounding import detect_evidence_contract_gap
from app.harness.tool_escalation import build_stable_common_knowledge_search_nudge
from app.services.base_agent import _sanitize_progress_hint


def test_cucumber_only_vegetable_turn_is_stable_common_knowledge():
    decision = classify_common_knowledge_turn("蔬菜里如果只吃黄瓜会怎样")

    assert decision.is_stable_common_knowledge
    assert decision.blocks_search
    assert decision.reason == "stable_nutrition_common_sense"


def test_cucumber_only_vegetable_turn_does_not_force_grounding_search():
    nudge = detect_evidence_contract_gap(
        reply_text=(
            "单吃黄瓜其实挺亏的。黄瓜水分很多，热量低，但维生素、矿物质和膳食纤维种类都比较单一。"
            "长期只靠它当蔬菜，容易让叶酸、胡萝卜素、钙钾镁等来源变窄。"
        ),
        user_text="蔬菜里如果只吃黄瓜会怎样",
        tool_names_called=[],
        action_outcomes=[],
    )

    assert nudge is None


def test_cucumber_only_vegetable_web_search_is_blocked():
    nudge = build_stable_common_knowledge_search_nudge(
        "蔬菜里如果只吃黄瓜会怎样",
        ["web_search"],
    )

    assert nudge is not None
    assert "稳定常识" in nudge


def test_explicit_source_brand_or_medical_context_allows_search():
    source = classify_common_knowledge_turn("请给来源，查一下只吃黄瓜作为唯一蔬菜的营养风险")
    brand = classify_common_knowledge_turn("某品牌黄瓜营养表准确数据是多少")
    medical = classify_common_knowledge_turn("肾病患者只吃黄瓜会怎样")

    assert not source.blocks_search
    assert not brand.blocks_search
    assert not medical.blocks_search


def test_common_knowledge_progress_hint_does_not_claim_searching():
    sanitized = _sanitize_progress_hint(
        "在查只吃黄瓜的生化数据，这减脂思路有点极端了",
        [],
        user_text="蔬菜里如果只吃黄瓜会怎样",
    )

    assert sanitized
    assert "查" not in sanitized
    assert "收集资料" not in sanitized
