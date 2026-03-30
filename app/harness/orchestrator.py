"""Shared loop-policy helpers for agent runtimes.

This does not own the full provider loop yet. It centralizes decisions that are
already common across providers so Phase 2 can converge on a unified
orchestrator without changing runtime behavior all at once.
"""

from __future__ import annotations

from typing import Callable


def should_compact_history(round_num: int, compact_after_round: int) -> bool:
    """Return True once the loop reaches the compaction threshold."""
    return round_num >= compact_after_round


def should_nudge_unmatched_reads(
    *,
    round_num: int,
    already_nudged: bool,
    tool_names_called: list[str],
    user_text: str,
    has_unmatched_reads: Callable[[list[str], str], bool],
) -> bool:
    """Return True when the model should be nudged to finish a pending write."""
    if already_nudged or round_num < 1:
        return False
    return has_unmatched_reads(tool_names_called, user_text)
