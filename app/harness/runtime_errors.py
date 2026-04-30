"""Deterministic runtime error classification for auto-fix gating."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


TRANSIENT_UPSTREAM = "transient_upstream"
CODE_BUG = "code_bug"
MANUAL_DIAGNOSTIC = "manual_diagnostic"
NEEDS_TRIAGE = "needs_triage"
IGNORED = "ignored"

_ALLOWED_AUTOFIX_PATH_RE = re.compile(r"(?:^|[/\\])app[/\\](?:tools|knowledge)[/\\]", re.I)
_CORE_SERVICE_PATH_RE = re.compile(
    r"(?:^|[/\\])app[/\\](?:services|webhook|channels|router|tenant|harness)[/\\]"
    r"|(?:^|[/\\])app[/\\](?:main|config)\.py",
    re.I,
)
_UNKNOWN_TOOL_RE = re.compile(r"\bunknown tool\b|未知工具", re.I)
_SELF_FIX_RE = re.compile(r"self_fix_error|auto_fix|autofix|自我修复", re.I)
_GITHUB_RE = re.compile(r"github|api\.github\.com|_self_get", re.I)
_GEMINI_RE = re.compile(r"gemini|google_genai|google\.genai", re.I)

_NETWORK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "remote_protocol",
        re.compile(
            r"RemoteProtocolError|Server disconnected without sending a response|"
            r"ProtocolError|proxy\s*error|ProxyError",
            re.I,
        ),
    ),
    (
        "network",
        re.compile(
            r"httpx\.(?:ConnectTimeout|ConnectError|ReadError|ReadTimeout|PoolTimeout|ProxyError|RemoteProtocolError)|"
            r"httpcore\.(?:ConnectTimeout|ConnectError|ReadError|ReadTimeout|PoolTimeout|ProxyError|RemoteProtocolError)|"
            r"\b(?:ConnectTimeout|ConnectError|ReadError|ReadTimeout|PoolTimeout|ProxyError|RemoteProtocolError)\b|"
            r"TLS handshake|ssl|dns|name resolution|temporary failure|network is unreachable|"
            r"connection (?:reset|aborted|refused)|econnreset|econnrefused|no route to host|timed out",
            re.I,
        ),
    ),
    ("browser_open", re.compile(r"Page\.goto: Timeout|Timeout \d+ms exceeded|playwright.*TimeoutError", re.I)),
    ("web_search", re.compile(r"web_search 连续失败|auto-lesson for web_search|web_search.*(?:timeout|timed out|failed|失败)", re.I)),
    ("xhs_search", re.compile(r"xhs_search timed out|xhs_ops: search .* timed out|login wall|验证码|CAPTCHA", re.I)),
    ("search_social_media", re.compile(r"search_social_media.*timeout|social_media.*timeout", re.I)),
    ("wecom_kf", re.compile(r"95018|send msg session status invalid|95007|invalid msg token|95013|conversation end", re.I)),
    ("third_party_api", re.compile(r"\b(?:429|500|502|503|504)\b|rate limit|temporar|service unavailable|gateway timeout", re.I)),
)

_TRIAGE_CATEGORIES = frozenset({"tool_error", "api_error", "timeout"})
_CODE_BUG_CATEGORIES = frozenset({"unhandled", "tool_exception", "startup_check"})
_SKIP_CATEGORIES = frozenset({"self_fix_error"})


@dataclass(frozen=True)
class RuntimeErrorDecision:
    kind: str
    autofix_allowed: bool
    diagnostic_only: bool
    reason: str
    labels: frozenset[str]


@dataclass(frozen=True)
class RuntimeErrorBatchDecision:
    decisions: tuple[RuntimeErrorDecision, ...]
    autofixable_errors: list[Any]
    triage_errors: list[Any]
    transient_count: int
    manual_count: int

    @property
    def should_autofix(self) -> bool:
        return bool(self.autofixable_errors)


def _blob(error: Any) -> str:
    return "\n".join(
        str(getattr(error, attr, "") or "")
        for attr in ("category", "tool_name", "summary", "detail", "tool_args")
    )


def _labels_for_transient(text: str) -> set[str]:
    labels: set[str] = set()
    for label, pattern in _NETWORK_PATTERNS:
        if pattern.search(text):
            labels.add(label)
    if not labels:
        return labels
    labels.add("transient")
    if "network" in labels or "remote_protocol" in labels:
        labels.add("network")
    if _GITHUB_RE.search(text):
        labels.add("github")
    if _GEMINI_RE.search(text):
        labels.add("gemini")
    return labels


def classify_runtime_error(error: Any) -> RuntimeErrorDecision:
    category = str(getattr(error, "category", "") or "")
    text = _blob(error)

    if category in _SKIP_CATEGORIES:
        return RuntimeErrorDecision(IGNORED, False, False, "skip_category", frozenset({"skip", category}))

    transient_labels = _labels_for_transient(text)
    if transient_labels or re.search(r"auto_fix gemini API call failed|_self_get connection error", text, re.I):
        transient_labels.update(_labels_for_transient(text))
        if _GITHUB_RE.search(text):
            transient_labels.add("github")
        if _GEMINI_RE.search(text):
            transient_labels.add("gemini")
        if not transient_labels:
            transient_labels.add("transient")
        return RuntimeErrorDecision(
            TRANSIENT_UPSTREAM,
            False,
            False,
            "upstream_or_network_failure",
            frozenset(transient_labels),
        )

    if category == "timeout":
        return RuntimeErrorDecision(
            TRANSIENT_UPSTREAM,
            False,
            False,
            "runtime_timeout",
            frozenset({"transient", "timeout"}),
        )

    if _SELF_FIX_RE.search(text):
        return RuntimeErrorDecision(MANUAL_DIAGNOSTIC, False, True, "self_fix_failure", frozenset({"self_fix"}))

    if _UNKNOWN_TOOL_RE.search(text):
        return RuntimeErrorDecision(
            CODE_BUG,
            False,
            True,
            "unknown_tool_without_allowed_path",
            frozenset({"unknown_tool"}),
        )

    if _ALLOWED_AUTOFIX_PATH_RE.search(text):
        return RuntimeErrorDecision(
            CODE_BUG,
            True,
            False,
            "stack_trace_points_to_allowed_autofix_path",
            frozenset({"allowed_path"}),
        )

    if category in _CODE_BUG_CATEGORIES:
        labels = {"code_category"}
        if _CORE_SERVICE_PATH_RE.search(text):
            labels.add("core_service_path")
        return RuntimeErrorDecision(
            MANUAL_DIAGNOSTIC,
            False,
            True,
            "code_error_outside_autofix_write_boundary",
            frozenset(labels),
        )

    if category in _TRIAGE_CATEGORIES:
        return RuntimeErrorDecision(
            NEEDS_TRIAGE,
            False,
            False,
            "requires_llm_code_bug_triage",
            frozenset({"llm_triage"}),
        )

    return RuntimeErrorDecision(
        MANUAL_DIAGNOSTIC,
        False,
        True,
        "unrecognized_or_unsafe_for_autofix",
        frozenset({"fail_closed"}),
    )


def classify_runtime_error_batch(errors: list[Any]) -> RuntimeErrorBatchDecision:
    decisions = tuple(classify_runtime_error(error) for error in errors)
    return RuntimeErrorBatchDecision(
        decisions=decisions,
        autofixable_errors=[
            error for error, decision in zip(errors, decisions, strict=False)
            if decision.autofix_allowed
        ],
        triage_errors=[
            error for error, decision in zip(errors, decisions, strict=False)
            if decision.kind == NEEDS_TRIAGE
        ],
        transient_count=sum(1 for decision in decisions if decision.kind == TRANSIENT_UPSTREAM),
        manual_count=sum(1 for decision in decisions if decision.diagnostic_only),
    )
