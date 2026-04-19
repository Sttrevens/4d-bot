"""Shared grounding policy for factual and time-sensitive turns."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

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
    "fetch_chat_history",
    "read_feishu_doc",
    "read_feishu_wiki",
    "list_feishu_tasks",
    "list_tasklist_tasks",
    "list_calendar_events",
    "get_feishu_minute_transcript",
})
_FIRST_PARTY_EVIDENCE_TOOLS = frozenset({
    "fetch_chat_history",
    "recall_memory",
    "read_feishu_doc",
    "read_feishu_wiki",
    "list_feishu_tasks",
    "list_tasklist_tasks",
    "list_calendar_events",
    "get_feishu_minute_transcript",
})
_FIRST_PARTY_SCOPE_RE = re.compile(
    r"(飞书|企微|微信群|群聊|大群|群里|本群|私聊|聊天记录|会话|频道|消息记录|"
    r"会议纪要|纪要|任务列表|日历)",
    re.IGNORECASE,
)
_FIRST_PARTY_LOOKUP_RE = re.compile(
    r"(看看|看下|查看|翻|爬楼|回顾|总结|同步|参与讨论|跟进|接着聊|刚才说|上条)",
    re.IGNORECASE,
)
_HIGH_RISK_DECISION_RE = re.compile(
    r"(预测|预判|推荐|建议|决策|评估|打分|胜率|比分|排名|投资|下注|买入|卖出|"
    r"forecast|predict|projection|recommendation|strategy)",
    re.IGNORECASE,
)
_ENTITY_RELATION_SIGNAL_RE = re.compile(
    r"(效力|在.*队|加盟|转会|担任|任职|属于|对阵|vs|面对|击败|冠军|排名|"
    r"is with|plays for|signed with|appointed as)",
    re.IGNORECASE,
)
_ENTITY_TOKEN_RE = re.compile(r"[A-Z][a-z]{2,}|[A-Z]{2,}|[\u4e00-\u9fff]{2,6}")
_EVIDENCE_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_TOKEN_STOPWORDS = {
    "现在", "目前", "当前", "最新", "今年", "本赛季", "球队", "球员", "预测", "分析",
    "首轮", "次轮", "东部", "西部", "总决赛", "比分", "对阵", "数据", "资料", "官方", "来源",
    "query", "site", "from", "with", "that", "this", "will", "would", "should",
    "then", "than", "playoff", "playoffs", "bracket", "matchup", "matchups",
}
_ERROR_OUTCOME_RE = re.compile(r"(失败|error|timeout|blocked|无权限|not found)", re.IGNORECASE)
_GROUNDING_DEEP_EVIDENCE_TOOLS = frozenset({"fetch_url", "browser_read", "browser_open"})


@dataclass(frozen=True)
class GroundingRiskProfile:
    level: str  # low | medium | high
    reason: str


@dataclass(frozen=True)
class EvidenceLedger:
    called_evidence_tools: frozenset[str]
    successful_evidence_count: int
    evidence_domains: frozenset[str]
    observed_years: frozenset[int]
    evidence_text: str


def _is_first_party_context_turn(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if not _FIRST_PARTY_SCOPE_RE.search(text):
        return False
    return bool(_FIRST_PARTY_LOOKUP_RE.search(text) or _TEMPORAL_URGENCY_RE.search(text))


def _extract_entity_tokens(text: str) -> set[str]:
    if not text:
        return set()
    tokens: set[str] = set()
    for raw in _ENTITY_TOKEN_RE.findall(text):
        token = raw.strip()
        if not token:
            continue
        lowered = token.lower()
        if lowered in _TOKEN_STOPWORDS:
            continue
        if token in _TOKEN_STOPWORDS:
            continue
        if len(token) <= 1:
            continue
        tokens.add(token)
    return tokens


def _extract_domains(text: str) -> set[str]:
    domains: set[str] = set()
    if not text:
        return domains
    for url in _EVIDENCE_URL_RE.findall(text):
        try:
            host = (urlparse(url).netloc or "").lower()
        except Exception:
            continue
        if host:
            domains.add(host)
    return domains


def _is_successful_evidence_outcome(outcome: str) -> bool:
    if not outcome:
        return False
    return not _ERROR_OUTCOME_RE.search(outcome)


def _lexical_coverage_ratio(reply_text: str, evidence_text: str) -> float:
    reply_tokens = _extract_entity_tokens(reply_text)
    if not reply_tokens:
        return 1.0
    evidence_tokens = _extract_entity_tokens(evidence_text)
    if not evidence_tokens:
        return 0.0
    hit = 0
    evidence_text_lower = (evidence_text or "").lower()
    for token in reply_tokens:
        if token in evidence_tokens or token.lower() in evidence_text_lower:
            hit += 1
    return hit / max(1, len(reply_tokens))


def classify_grounding_risk(user_text: str, reply_text: str) -> GroundingRiskProfile:
    text = (user_text or "").strip()
    if not text:
        return GroundingRiskProfile(level="low", reason="empty_turn")
    if should_relax_fact_grounding(text):
        return GroundingRiskProfile(level="low", reason="conceptual_turn")
    if requires_temporal_grounding(text):
        return GroundingRiskProfile(level="high", reason="temporal_facts")
    if _HIGH_RISK_DECISION_RE.search(text) and requires_external_grounding(text):
        return GroundingRiskProfile(level="high", reason="decision_like_task")
    if requires_external_grounding(text) or reply_contains_dense_factual_claims(reply_text):
        return GroundingRiskProfile(level="medium", reason="fact_lookup")
    return GroundingRiskProfile(level="low", reason="general_chat")


def build_evidence_ledger(
    tool_names_called: list[str],
    action_outcomes: list[tuple[str, str]] | None = None,
) -> EvidenceLedger:
    called = set(tool_names_called or [])
    called_evidence_tools = frozenset(called & _EVIDENCE_TOOLS)
    successful_count = 0
    domains: set[str] = set()
    years: set[int] = set()
    evidence_chunks: list[str] = []

    for name, outcome in action_outcomes or []:
        if name not in _EVIDENCE_TOOLS:
            continue
        if not _is_successful_evidence_outcome(outcome):
            continue
        successful_count += 1
        trimmed = (outcome or "")[:500]
        evidence_chunks.append(trimmed)
        domains.update(_extract_domains(trimmed))
        years.update(_extract_years(trimmed))

    evidence_text = "\n".join(evidence_chunks)
    return EvidenceLedger(
        called_evidence_tools=called_evidence_tools,
        successful_evidence_count=successful_count,
        evidence_domains=frozenset(domains),
        observed_years=frozenset(years),
        evidence_text=evidence_text,
    )


def build_evidence_contract_nudge(
    risk: GroundingRiskProfile,
    *,
    reason: str,
    successful_evidence_count: int,
    domain_count: int,
    coverage: float | None = None,
) -> str:
    detail = (
        f"当前证据条数={successful_evidence_count}，来源域名数={domain_count}"
        + (f"，实体覆盖率={coverage:.2f}" if coverage is not None else "")
    )
    return (
        "⚠️ 这是事实型回答，当前还没满足“证据账本”要求。"
        f"风险级别：{risk.level}（{risk.reason}），触发原因：{reason}。"
        f"{detail}。"
        "请先补齐证据（优先 fetch_url/browser_read 等深证据），再输出最终结论；"
        "若证据不足，请明确说“目前未查到可靠来源/无法确认”，不要把猜测写成事实。并优先引用官方来源。"
    )


def detect_evidence_contract_gap(
    reply_text: str,
    user_text: str,
    tool_names_called: list[str],
    action_outcomes: list[tuple[str, str]] | None = None,
) -> str | None:
    if not reply_text or not user_text:
        return None

    risk = classify_grounding_risk(user_text, reply_text)
    if risk.level == "low":
        return None

    ledger = build_evidence_ledger(tool_names_called, action_outcomes)
    if ledger.successful_evidence_count == 0:
        return build_evidence_contract_nudge(
            risk,
            reason="missing_evidence",
            successful_evidence_count=ledger.successful_evidence_count,
            domain_count=len(ledger.evidence_domains),
        )

    has_deep_evidence = bool(ledger.called_evidence_tools & _GROUNDING_DEEP_EVIDENCE_TOOLS)
    if risk.level == "high" and ledger.successful_evidence_count < 2 and not has_deep_evidence:
        return build_evidence_contract_nudge(
            risk,
            reason="insufficient_sources_for_high_risk",
            successful_evidence_count=ledger.successful_evidence_count,
            domain_count=len(ledger.evidence_domains),
        )

    if _ENTITY_RELATION_SIGNAL_RE.search(reply_text):
        reply_entities = _extract_entity_tokens(reply_text)
        if len(reply_entities) >= 3:
            coverage = _lexical_coverage_ratio(reply_text, ledger.evidence_text)
            threshold = 0.35 if risk.level == "high" else 0.20
            if coverage < threshold:
                return build_evidence_contract_nudge(
                    risk,
                    reason="low_entity_evidence_coverage",
                    successful_evidence_count=ledger.successful_evidence_count,
                    domain_count=len(ledger.evidence_domains),
                    coverage=coverage,
                )
    return None


def requires_external_grounding(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if _is_first_party_context_turn(text):
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
    if _is_first_party_context_turn(text):
        return set()
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
    if _is_first_party_context_turn(user_text):
        called = set(tool_names_called or [])
        if called & _FIRST_PARTY_EVIDENCE_TOOLS:
            if not action_outcomes:
                return None
            for name, outcome in action_outcomes:
                if name in _FIRST_PARTY_EVIDENCE_TOOLS and _is_successful_evidence_outcome(outcome):
                    return None
        return (
            "⚠️ 这是第一方上下文任务（群聊/会话/内部记录）。"
            "请先调用 fetch_chat_history / recall_memory / read_feishu_doc 等工具拿到当前上下文，"
            "再总结或参与讨论；不要跳到外部年份检索。"
        )
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
