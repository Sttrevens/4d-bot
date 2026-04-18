"""Shared grounding policy for factual and time-sensitive turns."""

from __future__ import annotations

import re
from datetime import datetime

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
_FAKE_TOOL_TRACE_RE = re.compile(r"<(?:tools_used|execute_tool)>", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_SEASON_RE = re.compile(r"\b((?:19|20)\d{2})\s*[-/]\s*(\d{2,4})\b")
_TEMPORAL_URGENCY_RE = re.compile(
    r"(现在|目前|当前|最新|刚刚|刚才|今日|今天|本周|本月|今年|正式出炉|"
    r"刚公布|实时|战况|现阶段|截至目前|最新消息)",
    re.IGNORECASE,
)
_TEMPORAL_TOPIC_RE = re.compile(
    r"(赛程|对阵|比分|战绩|排名|榜单|名单|结果|走势|发布|上架|更新|"
    r"股价|汇率|利率|政策|法规|CEO|总统|选举|任命)",
    re.IGNORECASE,
)
_CURRENT_RELATIVE_RE = re.compile(r"(今年|本赛季|当前|目前|最新|截至目前)", re.IGNORECASE)
_EVIDENCE_TOOLS = frozenset({
    "web_search",
    "fetch_url",
    "browser_open",
    "browser_read",
    "search_social_media",
    "xhs_search",
    "xhs_playwright_search",
})


def requires_external_grounding(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return bool(
        _EXPLICIT_RESEARCH_RE.search(text)
        or _PRICING_RE.search(text)
        or _PUBLIC_ENTITY_FACT_RE.search(text)
    )


def _extract_years(text: str) -> set[int]:
    years: set[int] = set()
    if not text:
        return years
    for m in _YEAR_RE.findall(text):
        try:
            years.add(int(m))
        except ValueError:
            continue
    for y1_s, y2_s in _SEASON_RE.findall(text):
        try:
            y1 = int(y1_s)
            y2 = int(y2_s)
        except ValueError:
            continue
        if y2 < 100:
            century = y1 // 100
            y2 = century * 100 + y2
            if y2 < y1:
                y2 += 100
        years.add(y1)
        years.add(y2)
    return years


def requires_temporal_grounding(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    explicit_year = bool(_extract_years(text))
    return bool(
        explicit_year
        or (_TEMPORAL_URGENCY_RE.search(text) and (requires_external_grounding(text) or _TEMPORAL_TOPIC_RE.search(text)))
    )


def _target_years_for_turn(user_text: str) -> set[int]:
    text = (user_text or "").strip()
    years = _extract_years(text)
    if years:
        return years
    if requires_temporal_grounding(text):
        return {datetime.now().year}
    return set()


def build_temporal_grounding_nudge(
    user_text: str,
    *,
    target_years: set[int],
    observed_years: set[int] | None = None,
) -> str:
    years_label = " / ".join(str(y) for y in sorted(target_years)) if target_years else "当前年份"
    observed = sorted(observed_years or set())
    observed_label = "、".join(str(y) for y in observed) if observed else "无明确年份锚点"
    _ = user_text
    return (
        "⚠️ 这是时效性事实任务，但你的证据链没有锁定正确时间锚点。"
        f"目标年份应为：{years_label}；当前证据/回复年份：{observed_label}。"
        "请先检索并确认目标年份对应的官方或权威来源，再给结论。"
        "如果还未拿到该年份的数据，明确说“目前未查到可靠来源”，不要用旧年份替代。"
    )


def detect_temporal_grounding_issue(
    reply_text: str,
    user_text: str,
    tool_names_called: list[str],
    action_outcomes: list[tuple[str, str]] | None = None,
) -> str | None:
    if not user_text:
        return None
    if not requires_temporal_grounding(user_text):
        return None

    target_years = _target_years_for_turn(user_text)
    if not target_years:
        return None

    called = set(tool_names_called or [])
    if not (called & _EVIDENCE_TOOLS):
        return build_temporal_grounding_nudge(user_text, target_years=target_years)

    evidence_text = ""
    if action_outcomes:
        evidence_text = "\n".join(
            outcome for name, outcome in action_outcomes if name in _EVIDENCE_TOOLS
        )
    reply_years = _extract_years(reply_text or "")
    evidence_years = _extract_years(evidence_text)
    observed_years = reply_years | evidence_years

    if observed_years and not (observed_years & target_years):
        return build_temporal_grounding_nudge(
            user_text,
            target_years=target_years,
            observed_years=observed_years,
        )

    if not (observed_years & target_years):
        if reply_text and _CURRENT_RELATIVE_RE.search(reply_text):
            return None
        return build_temporal_grounding_nudge(
            user_text,
            target_years=target_years,
            observed_years=observed_years,
        )

    return None


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
    fake_tool_trace = bool(reply_text and _FAKE_TOOL_TRACE_RE.search(reply_text))
    if _PRICING_RE.search(text):
        if _CODEX_PRODUCT_RE.search(text):
            prefix = (
                "⚠️ 你刚才只是口头描述自己搜了什么，或者输出了伪工具标签。"
                "下一轮不要再解释，也不要输出 <tools_used>/<execute_tool>。"
                "先真实调用 web_search，再回来回答。"
                if fake_tool_trace
                else ""
            )
            return prefix + (
                "⚠️ 用户问的是当前 Codex 产品的价格/定价/套餐/额度。"
                "先搜索当前的 OpenAI Codex 官方定价/pricing/help 页面，再回答。"
                "不要把当前 Codex 产品和历史上的旧 Codex API 模型混为一谈。"
                "优先搜索“site:openai.com Codex pricing”“site:openai.com/chatgpt Codex pricing”“site:help.openai.com Codex pricing”这类查询，"
                "不要拿旧的 codex/completions/edit endpoints 下线公告来回答当前产品问题。"
                "只引用与你当前问题直接相关的官方来源或可靠来源。"
                "如果官方页面没写清楚，就明确说没查到，不要猜套餐关系、倍数或额度规则。"
            )
        prefix = (
            "⚠️ 你刚才只是描述自己搜过，没有真实调用搜索工具。下一轮先真实调用 web_search，再回答。"
            if fake_tool_trace
            else ""
        )
        return prefix + (
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
