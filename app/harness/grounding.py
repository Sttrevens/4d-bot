"""Shared grounding policy for factual and time-sensitive turns."""

from __future__ import annotations

import re

from app.harness.turn_mode import infer_turn_mode, is_non_actionable_turn

_EXPLICIT_RESEARCH_RE = re.compile(
    r"(搜[一搜索]|查[一查找询]|research|调研|调查|了解一下|看看|帮我[找查搜看]|"
    r"谁是|有哪些|现在[是有]|最新的|目前|现状|什么情况|怎么样了)",
    re.IGNORECASE,
)

_PRICING_RE = re.compile(
    r"(价格|定价|报价|套餐|额度|配额|用量|周额度|月额度|extra|充值|"
    r"pricing|price|quota|usage|subscription|plan|tier)",
    re.IGNORECASE,
)
_CODEX_PRODUCT_RE = re.compile(r"codex|chatgpt\s*codex|openai\s*codex", re.IGNORECASE)

_PUBLIC_ENTITY_FACT_RE = re.compile(
    r"(哪些人|成员|董事|高管|管理层|创始人|CEO|CTO|CFO|COO|执行董事|总经理|"
    r"董事长|联合创始人|总裁|副总裁|法人|股东|估值|融资)",
    re.IGNORECASE,
)

_CONCEPTUAL_TOPICS_RE = re.compile(
    r"(哲学|教义|宿命论|决定论|离散|连续|逻辑|因果关系|三观|观点|"
    r"什么意思|什么逻辑|怎么看|为什么|如何|解释|分析|总结)",
    re.IGNORECASE,
)

_FACTUAL_CLAIM_SIGNALS = re.compile(
    r"(?:执行董事|监事|总经理|董事长|CEO|CTO|CFO|COO|创始人|联合创始人|总裁|副总裁|法人)"
    r".{0,5}[：:].{0,5}[\u4e00-\u9fff]{2,4}"
    r"|根据(?:公开|工商|官方|最新|公开的).{0,8}(?:信息|资料|数据|披露|显示|记录)"
    r"|[\u4e00-\u9fff]{2,4}[、,，][\u4e00-\u9fff]{2,4}[、,，][\u4e00-\u9fff]{2,4}"
)


def requires_external_grounding(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return bool(
        _EXPLICIT_RESEARCH_RE.search(text)
        or _PRICING_RE.search(text)
        or _PUBLIC_ENTITY_FACT_RE.search(text)
    )


def should_relax_fact_grounding(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if _PRICING_RE.search(text) or _PUBLIC_ENTITY_FACT_RE.search(text):
        return False
    if is_non_actionable_turn(text) and _CONCEPTUAL_TOPICS_RE.search(text):
        return True
    turn_mode = infer_turn_mode(text)
    return turn_mode.mode == "analysis" and "research" not in turn_mode.groups


def reply_contains_dense_factual_claims(reply_text: str) -> bool:
    return bool(reply_text and _FACTUAL_CLAIM_SIGNALS.search(reply_text))


def build_grounding_nudge(user_text: str, reply_text: str = "") -> str:
    text = (user_text or "").strip()
    if _PRICING_RE.search(text):
        if _CODEX_PRODUCT_RE.search(text):
            return (
                "⚠️ 用户问的是当前 Codex 产品的价格/定价/套餐/额度。"
                "先搜索当前的 OpenAI Codex 官方定价/pricing/help 页面，再回答。"
                "不要把当前 Codex 产品和历史上的旧 Codex API 模型混为一谈。"
                "优先搜索“OpenAI Codex pricing official”“OpenAI Codex help”这类查询，"
                "只引用与你当前问题直接相关的官方来源或可靠来源。"
                "如果官方页面没写清楚，就明确说没查到，不要猜套餐关系、倍数或额度规则。"
            )
        return (
            "⚠️ 用户问的是价格、套餐、额度或配额这类会变化的信息。"
            "请先用 web_search 查当前、可靠、最好是官方来源的定价/配额说明，再回答。"
            "只搜索当前问题本身，不要搜索无关人物、公司梗或内部玩笑。"
            "如果没查到明确来源，就直接说没查到，不要猜倍数、套餐细则或额度。"
        )
    if _PUBLIC_ENTITY_FACT_RE.search(text):
        return (
            "⚠️ 用户问的是公开事实（人名、职位、管理层或公司信息）。"
            "请先用 web_search 验证与当前问题直接相关的公开资料，再回答。"
            "不要把用户昵称、人设玩笑或无关内部信息当作事实去搜索。"
            "如果公开来源没有答案，就明确说公开资料未找到。"
        )
    if should_relax_fact_grounding(text):
        return (
            "⚠️ 当前问题更偏概念解释。除非用户明确要求查资料，否则不要为了人设段子或无关名字去搜索。"
            "如果你确实需要查资料，只搜索当前概念问题直接相关的来源。"
        )
    return (
        "⚠️ 你没有使用任何搜索工具就给出了带事实性的回答。"
        "请先用 web_search 查与当前问题直接相关的可靠来源，再基于结果回答。"
        "如果缺少可靠来源，就明确说明不确定，不要猜。"
    )
