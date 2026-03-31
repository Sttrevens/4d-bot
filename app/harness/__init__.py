"""Harness foundation modules.

Phase 1 centralizes context-compaction policy.
Phase 2 starts extracting shared context/inbox pipeline helpers and loop policy.
"""

from app.harness.compaction import (
    DEFAULT_COMPACTION_AFTER_ROUND,
    DEFAULT_COMPACTION_KEEP_RECENT,
    compress_gemini_function_results,
    compress_openai_tool_results,
)
from app.harness.context import (
    append_openai_inbox_messages,
    normalize_inbox_item,
)
from app.harness.grounding import (
    build_grounding_nudge,
    reply_contains_dense_factual_claims,
    requires_external_grounding,
    should_relax_fact_grounding,
)
from app.harness.orchestrator import (
    should_compact_history,
    should_nudge_unmatched_reads,
)
from app.harness.task_board import (
    PLAN_ACTIVE_STATUSES,
    advance_next_step,
    build_active_plan_context,
    format_plan_text,
    normalize_steps,
    prune_invalid_dependencies,
)
from app.harness.turn_mode import (
    TurnMode,
    has_explicit_code_intent,
    infer_turn_mode,
    is_non_actionable_turn,
    is_product_pricing_turn,
    sanitize_suggested_groups,
    should_run_code_preflight,
)

__all__ = [
    "DEFAULT_COMPACTION_AFTER_ROUND",
    "DEFAULT_COMPACTION_KEEP_RECENT",
    "compress_gemini_function_results",
    "compress_openai_tool_results",
    "append_openai_inbox_messages",
    "normalize_inbox_item",
    "build_grounding_nudge",
    "reply_contains_dense_factual_claims",
    "requires_external_grounding",
    "should_relax_fact_grounding",
    "should_compact_history",
    "should_nudge_unmatched_reads",
    "PLAN_ACTIVE_STATUSES",
    "advance_next_step",
    "build_active_plan_context",
    "format_plan_text",
    "normalize_steps",
    "prune_invalid_dependencies",
    "TurnMode",
    "has_explicit_code_intent",
    "infer_turn_mode",
    "is_non_actionable_turn",
    "is_product_pricing_turn",
    "sanitize_suggested_groups",
    "should_run_code_preflight",
]
