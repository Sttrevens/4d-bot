"""Stable common-knowledge policy shared by grounding and tool gates."""

from __future__ import annotations

import re
from dataclasses import dataclass


_EXPLICIT_SOURCE_OR_FRESHNESS_RE = re.compile(
    r"(搜|搜索|查|查询|检索|来源|链接|出处|引用|最新|目前|现在|今年|今日|今天|"
    r"官方|精确|准确|某品牌|品牌|营养表|配料表|价格|报价|政策|法规|"
    r"source|link|latest|current|official|brand|nutrition facts|price)",
    re.IGNORECASE,
)
_HIGH_RISK_HEALTH_RE = re.compile(
    r"(医疗|医学|药|药物|用药|病|疾病|症|肾病|肝病|糖尿病|高血压|低血糖|"
    r"孕妇|怀孕|儿童|婴儿|老人|过敏|治疗|医生|医院|手术|处方)",
    re.IGNORECASE,
)
_COMMON_FOOD_RE = re.compile(
    r"(黄瓜|蔬菜|水果|苹果|香蕉|米饭|面包|鸡蛋|牛奶|咖啡|茶|饮料|"
    r"清酒|威士忌|啤酒|红酒|白酒|烧酒|酒|鱼腥草|刺身|三文鱼|牛肉|鸡胸|豆腐|食物)",
    re.IGNORECASE,
)
_NUTRITION_COMMON_SENSE_RE = re.compile(
    r"(热量|卡路里|大卡|千卡|kcal|calorie|calories|营养|维生素|矿物质|"
    r"膳食纤维|蛋白质|脂肪|碳水|酒精度|abv|只吃|唯一|单吃|会怎样|怎么样|危害|缺什么|均衡)",
    re.IGNORECASE,
)
_UNIT_CONVERSION_RE = re.compile(
    r"(单位换算|换算|多少克|多少毫升|\bml\b|\bg\b|kg|千克|克|毫升|升)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CommonKnowledgeDecision:
    is_stable_common_knowledge: bool
    blocks_search: bool
    reason: str


def classify_common_knowledge_turn(
    user_text: str,
    proposed_query: str = "",
) -> CommonKnowledgeDecision:
    text = f"{user_text or ''}\n{proposed_query or ''}".strip()
    user_only = (user_text or "").strip()
    if not text:
        return CommonKnowledgeDecision(False, False, "empty")

    is_common = bool(
        _UNIT_CONVERSION_RE.search(text)
        or (_COMMON_FOOD_RE.search(text) and _NUTRITION_COMMON_SENSE_RE.search(text))
    )
    if not is_common:
        return CommonKnowledgeDecision(False, False, "not_common_knowledge")
    if _HIGH_RISK_HEALTH_RE.search(user_only):
        return CommonKnowledgeDecision(True, False, "high_risk_health_context")
    if _EXPLICIT_SOURCE_OR_FRESHNESS_RE.search(user_only):
        return CommonKnowledgeDecision(True, False, "explicit_source_or_freshness")
    return CommonKnowledgeDecision(True, True, "stable_nutrition_common_sense")


def should_relax_common_knowledge_grounding(user_text: str) -> bool:
    return classify_common_knowledge_turn(user_text).blocks_search


def build_common_knowledge_search_block_message() -> str:
    return (
        "POLICY BLOCK: 这是稳定常识/常见营养或单位换算类问题，不需要 web_search。"
        "请直接基于常识给大概范围或清晰判断，并说明不同品牌、度数、做法、份量或个体情况会浮动；"
        "不要声称查过来源，也不要编造链接。"
    )
