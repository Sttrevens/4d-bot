"""Guardrails for query-topic alignment in follow-up turns."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from urllib.parse import unquote

_SPACE_RE = re.compile(r"\s+")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

_FOCUS_STOPWORDS = {
    "the",
    "this",
    "that",
    "these",
    "those",
    "and",
    "for",
    "with",
    "about",
    "from",
    "into",
    "continue",
    "search",
    "result",
    "results",
    "keyword",
    "site",
    "https",
    "http",
    "www",
    "com",
    "cn",
    "org",
    "net",
    "你",
    "我",
    "他",
    "她",
    "它",
    "我们",
    "你们",
    "他们",
    "这个",
    "那个",
    "这些",
    "那些",
    "这里",
    "那里",
    "刚才",
    "继续",
    "再试试",
    "搜索",
    "一下",
    "一下子",
    "问题",
    "内容",
    "信息",
    "资料",
    "看看",
    "查查",
}

_FOCUS_FILLER_WORDS = {
    "game",
    "games",
    "info",
    "steam",
    "游戏",
    "信息",
    "资料",
    "介绍",
    "官网",
}

_STEAM_HINT_RE = re.compile(r"(steam|游戏|发售|wishlist|愿望单)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_SEASON_RE = re.compile(r"\b((?:19|20)\d{2})\s*[-/]\s*(\d{2,4})\b")
_TEMPORAL_NOW_RE = re.compile(
    r"(现在|目前|当前|最新|刚刚|刚出炉|今日|今天|本周|本月|今年|截至目前|latest|current|today|now)",
    re.IGNORECASE,
)
_SPORTS_RE = re.compile(
    r"(nba|wnba|nfl|mlb|nhl|季后赛|playoff|总决赛|分区决赛|对阵|战绩|排名|赛程|比分)",
    re.IGNORECASE,
)
_NBA_RE = re.compile(r"\bnba\b|湖人|勇士|凯尔特人|雷霆|掘金|尼克斯|雄鹿|76人", re.IGNORECASE)
_FINANCE_RE = re.compile(
    r"(股价|财报|市值|营收|利润|估值|融资|汇率|利率|通胀|gdp|cpi|ppi|非农|"
    r"stock|earnings|revenue|market cap|forex|rate)",
    re.IGNORECASE,
)
_WEATHER_RE = re.compile(
    r"(天气|气温|降雨|台风|预警|空气质量|weather|forecast|temperature|rain|storm)",
    re.IGNORECASE,
)
_POLICY_RE = re.compile(
    r"(政策|法规|法案|监管|公告|白皮书|行政令|law|regulation|policy|act|guideline)",
    re.IGNORECASE,
)
_FUTURE_SCOPE_RE = re.compile(
    r"(future|long\s*term|three[-\s]?year|projection|projected|outlook|"
    r"未来|长期|三年|潜力|前景|远期|王朝|dynasty|power\s*rankings?)",
    re.IGNORECASE,
)
_PREDICTION_TASK_RE = re.compile(
    r"(预测|预判|推演|胜率|比分|推荐|策略|odds|forecast|prediction|predict|projection)",
    re.IGNORECASE,
)
_FACT_PACK_QUERY_RE = re.compile(
    r"(bracket|matchup|matchups|standings|schedule|injur|injury|seed|play-?in|official|confirmed|"
    r"对阵|排名|赛程|伤病|附加赛|名单|战报|赛果|实时)",
    re.IGNORECASE,
)
_OPINION_QUERY_RE = re.compile(
    r"(expert|experts|picks?|预测|前瞻|盘口|odds|power\s*rankings?|future|outlook|projection|"
    r"专家|看好|冠军归属|谁会夺冠|谁能夺冠)",
    re.IGNORECASE,
)


def _extract_focus_tokens(text: str) -> list[str]:
    source = unquote((text or "").lower())
    if not source:
        return []
    source = _SPACE_RE.sub(" ", source)
    ascii_tokens = [
        t for t in _ASCII_TOKEN_RE.findall(source)
        if len(t) >= 3 and t not in _FOCUS_FILLER_WORDS and t not in _FOCUS_STOPWORDS
    ]
    cjk_tokens = [
        t for t in _CJK_TOKEN_RE.findall(source)
        if t not in _FOCUS_FILLER_WORDS and t not in _FOCUS_STOPWORDS
    ]
    return ascii_tokens + cjk_tokens


def extract_focus_terms(*texts: str, max_terms: int = 24) -> tuple[str, ...]:
    """Extract stable topic terms from user/context text."""
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(_extract_focus_tokens(text))
    if not counter:
        return ()
    ranked = sorted(
        counter.items(),
        key=lambda item: (-item[1], -len(item[0]), item[0]),
    )
    return tuple(term for term, _ in ranked[:max_terms])


def is_query_off_topic(query: str, focus_terms: list[str] | tuple[str, ...]) -> bool:
    """Return True when a search query has zero lexical overlap with focus terms."""
    if not query or not focus_terms:
        return False
    q_tokens = set(_extract_focus_tokens(query))
    if not q_tokens:
        return False
    focus_set = set(focus_terms)
    if q_tokens & focus_set:
        return False

    q_text = unquote(query).lower()
    for term in focus_set:
        if len(term) >= 4 and term in q_text:
            return False
    return True


def is_temporal_scope_drift_query(query: str, user_text: str) -> bool:
    """Return True when query drifts into future/projection scope for a current-facts turn."""
    q = (query or "").strip()
    u = (user_text or "").strip()
    if not q or not u:
        return False
    if not _TEMPORAL_NOW_RE.search(u):
        return False
    if _FUTURE_SCOPE_RE.search(u):
        return False
    if not (_SPORTS_RE.search(u) or _FINANCE_RE.search(u) or _POLICY_RE.search(u) or _WEATHER_RE.search(u)):
        return False
    return bool(_FUTURE_SCOPE_RE.search(q))


def requires_fact_pack_first(user_text: str) -> bool:
    """Return True when the turn is a current-facts prediction/decision task."""
    u = (user_text or "").strip()
    if not u:
        return False
    if not _TEMPORAL_NOW_RE.search(u):
        return False
    if not _PREDICTION_TASK_RE.search(u):
        return False
    return bool(_SPORTS_RE.search(u) or _FINANCE_RE.search(u) or _POLICY_RE.search(u))


def is_fact_pack_query(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    return bool(_FACT_PACK_QUERY_RE.search(q))


def is_opinion_query(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    return bool(_OPINION_QUERY_RE.search(q))


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


def _inject_temporal_anchor(query: str, user_text: str, *, current_year: int) -> str:
    if _extract_years(query):
        return query
    context = f"{user_text} {query}".strip()
    if not _TEMPORAL_NOW_RE.search(context):
        return query
    if not (
        _SPORTS_RE.search(context)
        or _FINANCE_RE.search(context)
        or _WEATHER_RE.search(context)
        or _POLICY_RE.search(context)
    ):
        return query
    return f"{query} {current_year}".strip()


def _inject_domain_hint(query: str, user_text: str) -> str:
    lower = query.lower()
    if "site:" in lower:
        return query
    context = f"{user_text} {query}".strip()
    if _NBA_RE.search(context):
        return f"{query} (site:nba.com OR site:espn.com OR site:cbssports.com)"
    if _SPORTS_RE.search(context):
        return f"{query} (site:espn.com OR site:cbssports.com)"
    if _FINANCE_RE.search(context):
        return f"{query} (site:finance.yahoo.com OR site:investing.com OR site:sec.gov)"
    if _WEATHER_RE.search(context):
        return f"{query} (site:weather.com OR site:noaa.gov)"
    if _POLICY_RE.search(context):
        return f"{query} site:.gov"
    return query


def rewrite_web_search_query(
    query: str,
    *,
    user_text: str = "",
    current_year: int | None = None,
) -> str:
    """Rewrite low-signal queries toward timely, authoritative sources."""
    raw = (query or "").strip()
    if not raw:
        return raw
    if current_year is None:
        current_year = datetime.now().year

    raw = _inject_temporal_anchor(raw, user_text, current_year=current_year)
    raw = _inject_domain_hint(raw, user_text)

    lower = raw.lower()
    if "site:" in lower or "store.steampowered.com" in lower:
        return raw
    if _STEAM_HINT_RE.search(raw):
        return f"{raw} site:store.steampowered.com"
    return raw
