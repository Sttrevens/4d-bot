"""Memory quality policy for user-model memories.

The goal is to keep memory useful without turning one emotional sentence into
a durable identity claim. This module is deterministic by design so it can be
used in tests, admin previews, and low-latency write paths.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum


class MemoryScope(str, Enum):
    SHORT_TERM = "short_term"
    PROFILE = "profile"
    JOURNAL = "journal"
    REVIEW = "review"


@dataclass(frozen=True)
class MemoryQualityDecision:
    kind: str
    scope: MemoryScope
    sensitivity: str
    confidence: float
    ttl_days: int
    reason: str
    text: str
    source: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["scope"] = self.scope.value
        return data


_POISONING_RE = re.compile(
    r"(忽略.{0,8}(系统|开发者|安全|规则|提示)|"
    r"ignore.{0,16}(system|developer|safety|instruction)|"
    r"(泄露|发给我|告诉我).{0,12}(token|密钥|api[_ -]?key|密码|凭证)|"
    r"(绕过|关闭).{0,10}(安全|权限|审核))",
    re.IGNORECASE,
)

_EMOTION_RE = re.compile(
    r"(烦|焦虑|压力|难受|崩溃|低落|沮丧|委屈|害怕|(?:^|\b)emo(?:\b|$)|累|疲惫|烦躁|不安)",
    re.IGNORECASE,
)
_HEALTH_RE = re.compile(r"(抑郁|自残|自杀|病|药|诊断|心理|乳糖不耐|过敏|疼)", re.IGNORECASE)
_SUPPORT_RE = re.compile(r"(陪我|共情|安慰|别.{0,6}大道理|不要.{0,6}大道理|先.{0,8}拆|支持|鼓励)", re.IGNORECASE)
_EXPLICIT_PREF_RE = re.compile(r"(以后|下次|每次|默认|记住|希望你|我希望|请你|别|不要)", re.IGNORECASE)
_STABLE_RE = re.compile(r"(一直|长期|总是|每次|经常|反复|稳定|通常)", re.IGNORECASE)
_IDENTITY_RE = re.compile(r"(我是|我负责|我的团队|我们团队|我在.{0,12}(公司|团队|项目|负责)|我的职责)", re.IGNORECASE)
_GOAL_RE = re.compile(r"(我想|我要|目标|正在|推进|希望把|要把|计划)", re.IGNORECASE)
_OPEN_LOOP_RE = re.compile(r"(待办|之后|后续|下次|记得|跟进|还要|需要再|没完成|继续)", re.IGNORECASE)
_RELATION_RE = re.compile(r"(信任你|依赖你|陪伴|像朋友|像搭档|关系|默契)", re.IGNORECASE)
_NUMERIC_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:万|w|k|%|份|元|天|周|月|年)?", re.IGNORECASE)
_NUMERIC_CUE_RE = re.compile(r"(预测|承诺|估算|销量|愿望单|目标|预算|报价|deadline|截止)", re.IGNORECASE)


def sanitize_memory_text(text: str) -> str:
    """Remove instruction-like or credential-seeking fragments before memory injection."""
    clean = str(text or "").strip()
    clean = re.sub(
        r"忽略.{0,12}(系统|开发者|安全|规则|提示)",
        "[疑似注入指令已省略]",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"ignore.{0,24}(system|developer|safety|instruction)s?",
        "[疑似注入指令已省略]",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"(管理员\s*)?(token|密钥|api[_ -]?key|密码|凭证)",
        "[敏感凭证请求已省略]",
        clean,
        flags=re.IGNORECASE,
    )
    return clean[:500]


def classify_memory_candidate(
    text: str,
    *,
    source: str = "",
    now: datetime | None = None,
) -> MemoryQualityDecision:
    """Classify one memory candidate into a storage scope and quality category."""
    _ = now or datetime.now(timezone.utc)
    raw = str(text or "").strip()
    clean = sanitize_memory_text(raw)

    if not raw:
        return MemoryQualityDecision(
            kind="empty",
            scope=MemoryScope.JOURNAL,
            sensitivity="low",
            confidence=0.0,
            ttl_days=0,
            reason="empty memory candidate",
            text="",
            source=source,
        )

    if _POISONING_RE.search(raw):
        return MemoryQualityDecision(
            kind="poisoning_risk",
            scope=MemoryScope.REVIEW,
            sensitivity="high",
            confidence=0.2,
            ttl_days=0,
            reason="looks like prompt injection or credential exfiltration",
            text=clean,
            source=source,
        )

    has_emotion = bool(_EMOTION_RE.search(raw))
    has_health = bool(_HEALTH_RE.search(raw))
    has_support = bool(_SUPPORT_RE.search(raw))
    explicit_pref = bool(_EXPLICIT_PREF_RE.search(raw))
    stable = bool(_STABLE_RE.search(raw))

    durable_support = has_support and bool(re.search(
        r"(以后|下次|每次|默认|记住|希望你|我希望|请你)",
        raw,
        re.IGNORECASE,
    ))

    if durable_support:
        return MemoryQualityDecision(
            kind="support_style",
            scope=MemoryScope.PROFILE,
            sensitivity="medium" if has_emotion or has_health else "low",
            confidence=0.82,
            ttl_days=365,
            reason="explicit support or collaboration preference",
            text=clean,
            source=source,
        )

    if has_emotion or has_health:
        profile_scope = stable and explicit_pref
        return MemoryQualityDecision(
            kind="emotional_state",
            scope=MemoryScope.PROFILE if profile_scope else MemoryScope.SHORT_TERM,
            sensitivity="high" if has_health else "medium",
            confidence=0.72 if profile_scope else 0.52,
            ttl_days=90 if profile_scope else 7,
            reason="emotional or wellbeing state; keep transient unless clearly stable",
            text=clean,
            source=source,
        )

    if _RELATION_RE.search(raw):
        return MemoryQualityDecision(
            kind="relationship_trust",
            scope=MemoryScope.PROFILE if stable or explicit_pref else MemoryScope.SHORT_TERM,
            sensitivity="medium",
            confidence=0.74 if stable or explicit_pref else 0.5,
            ttl_days=180 if stable or explicit_pref else 14,
            reason="relationship or trust signal",
            text=clean,
            source=source,
        )

    if _IDENTITY_RE.search(raw):
        return MemoryQualityDecision(
            kind="identity_background",
            scope=MemoryScope.PROFILE,
            sensitivity="low",
            confidence=0.78,
            ttl_days=365,
            reason="stable user role or background fact",
            text=clean,
            source=source,
        )

    if _OPEN_LOOP_RE.search(raw):
        return MemoryQualityDecision(
            kind="open_loop",
            scope=MemoryScope.PROFILE,
            sensitivity="low",
            confidence=0.7,
            ttl_days=120,
            reason="follow-up or unfinished work",
            text=clean,
            source=source,
        )

    if _GOAL_RE.search(raw):
        return MemoryQualityDecision(
            kind="current_goal",
            scope=MemoryScope.PROFILE,
            sensitivity="low",
            confidence=0.68,
            ttl_days=180,
            reason="current goal or project direction",
            text=clean,
            source=source,
        )

    if explicit_pref:
        return MemoryQualityDecision(
            kind="preference",
            scope=MemoryScope.PROFILE,
            sensitivity="low",
            confidence=0.72,
            ttl_days=365,
            reason="explicit preference or collaboration rule",
            text=clean,
            source=source,
        )

    if _NUMERIC_RE.search(raw) and _NUMERIC_CUE_RE.search(raw):
        return MemoryQualityDecision(
            kind="numeric_commitment",
            scope=MemoryScope.JOURNAL,
            sensitivity="low",
            confidence=0.75,
            ttl_days=365,
            reason="numeric prediction, estimate, or commitment",
            text=clean,
            source=source,
        )

    return MemoryQualityDecision(
        kind="journal",
        scope=MemoryScope.JOURNAL,
        sensitivity="low",
        confidence=0.4,
        ttl_days=90,
        reason="generic episodic memory",
        text=clean,
        source=source,
    )
