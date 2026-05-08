"""Shared memory-recall policy.

This module centralizes recall strategy decisions so memory behavior can evolve
without scattering one-off heuristics in memory service code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.harness.turn_mode import infer_turn_mode

_HISTORY_REFERENCE_RE = re.compile(
    r"(上次|之前|以前|还记得|你记不记得|你说过|我说过|历史|记录|档案|病历|后来)",
    re.IGNORECASE,
)

_FIRST_PERSON_STATE_RE = re.compile(
    r"(我.{0,12}(是不是|是否|有没有|能不能|会不会|适不适合|还会|耐不耐)|"
    r"我(现在|最近).{0,12}(怎么样|如何|状态))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MemoryRecallPlan:
    """Recall plan for one user turn."""

    deep_scan: bool
    enable_similarity_fallback: bool
    similarity_threshold: float
    max_similarity_candidates: int


def build_memory_recall_plan(
    *,
    user_text: str = "",
    keyword: str = "",
    tags: list[str] | None = None,
    limit: int = 10,
) -> MemoryRecallPlan:
    """Build a deterministic recall plan from turn context.

    The policy is intentionally domain-agnostic:
    - explicit historical references -> deep scan
    - first-person state questions -> deep scan (higher miss cost)
    - analysis/research turns -> deep scan
    """
    text = (user_text or "").strip()
    kw = (keyword or "").strip()
    tags = tags or []

    turn_mode = infer_turn_mode(text) if text else None
    wants_history = bool(text and _HISTORY_REFERENCE_RE.search(text))
    first_person_state = bool(text and _FIRST_PERSON_STATE_RE.search(text))
    analytical = bool(turn_mode and turn_mode.mode in {"analysis", "research"})

    # Avoid full-journal scans for generic analysis/research turns.
    # Deep scans are reserved for explicit historical/state-memory intent.
    deep_scan = wants_history or first_person_state
    # If caller has no query context at all, similarity fallback doesn't help.
    enable_similarity = bool(text or kw or tags)
    threshold = 0.10 if deep_scan else (0.16 if analytical else 0.18)
    # Keep bounded but larger for deep scans.
    base = max(80, limit * 12)
    max_candidates = max(base, 180 if deep_scan else 100)

    return MemoryRecallPlan(
        deep_scan=deep_scan,
        enable_similarity_fallback=enable_similarity,
        similarity_threshold=threshold,
        max_similarity_candidates=max_candidates,
    )
