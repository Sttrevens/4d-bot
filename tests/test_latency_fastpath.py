from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest


def test_latency_trace_payload_contains_spans_models_and_tools(caplog):
    from app.services.latency_trace import LatencyTrace

    caplog.set_level(logging.INFO)
    trace = LatencyTrace(tenant_id="pm-bot", run_id="run_1")
    with trace.span("intent_classify", mode="adaptive"):
        pass
    trace.record_round("gemini-3-flash-preview")
    trace.record_tool("recall_memory")

    payload = trace.finish("success")

    assert payload["tenant_id"] == "pm-bot"
    assert payload["run_id"] == "run_1"
    assert payload["outcome"] == "success"
    assert payload["total_ms"] >= 0
    assert payload["rounds"] == 1
    assert payload["models"] == ["gemini-3-flash-preview"]
    assert payload["tools"] == ["recall_memory"]
    assert payload["spans"][0]["name"] == "intent_classify"
    assert "latency_trace:" in caplog.text


def test_webhook_batch_wait_defaults_to_fast_value(monkeypatch):
    from app.services.latency_trace import get_webhook_batch_wait_seconds

    monkeypatch.delenv("WEBHOOK_BATCH_WAIT_S", raising=False)
    assert get_webhook_batch_wait_seconds() == pytest.approx(0.8)

    monkeypatch.setenv("WEBHOOK_BATCH_WAIT_S", "0.25")
    assert get_webhook_batch_wait_seconds() == pytest.approx(0.25)

    monkeypatch.setenv("WEBHOOK_BATCH_WAIT_S", "-1")
    assert get_webhook_batch_wait_seconds() == pytest.approx(0.8)


def test_adaptive_intent_fastpath_skips_plain_image_classifier(monkeypatch):
    from app.services import gemini_provider

    monkeypatch.setenv("BOT_FASTPATH_ENABLED", "1")
    monkeypatch.setenv("BOT_INTENT_CLASSIFIER_MODE", "adaptive")

    image_turn = gemini_provider._adaptive_intent_fastpath(
        "请查看这张图片",
        has_media=True,
    )
    assert image_turn == {"type": "normal", "groups": ["core"]}

    quick_turn = gemini_provider._adaptive_intent_fastpath("你好", has_media=False)
    assert quick_turn == {"type": "quick", "groups": ["core"]}

    research_turn = gemini_provider._adaptive_intent_fastpath(
        "帮我深度调研一下小红书竞品账号",
        has_media=False,
    )
    assert research_turn is None


@pytest.mark.asyncio
async def test_adaptive_intent_classifier_timeout_falls_back_quickly(monkeypatch):
    from app.services import gemini_provider

    class SlowModels:
        async def generate_content(self, **_kwargs):
            await asyncio.sleep(5)
            return SimpleNamespace(text='{"type":"normal","groups":["core"]}')

    client = SimpleNamespace(aio=SimpleNamespace(models=SlowModels()))

    monkeypatch.setenv("BOT_FASTPATH_ENABLED", "1")
    monkeypatch.setenv("BOT_INTENT_CLASSIFIER_MODE", "adaptive")
    monkeypatch.setenv("BOT_INTENT_CLASSIFIER_TIMEOUT_S", "0.05")

    start = time.monotonic()
    result = await gemini_provider._classify_intent_adaptive(
        client,
        "gemini-3-flash-preview",
        "帮我调研一下这些游戏销量",
        has_media=False,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 0.5
    assert result["type"] == "research"
    assert "research" in result["groups"]


@pytest.mark.asyncio
async def test_memory_context_adaptive_skips_llm_recall_for_ordinary_turn(monkeypatch):
    from app.services import memory

    profile = {
        "name": "吴天骄",
        "interaction_count": 3,
        "preferences": ["汇报直接说结论"],
        "recent_topics": ["dashboard"],
    }

    llm_recall_called = False

    async def fail_recall_decision(_text):
        nonlocal llm_recall_called
        llm_recall_called = True
        return {"r": False, "t": [], "k": ""}

    monkeypatch.setenv("BOT_FASTPATH_ENABLED", "1")
    monkeypatch.setenv("BOT_MEMORY_RECALL_MODE", "adaptive")
    monkeypatch.setattr(memory, "get_user_profile", lambda _user_id: profile)
    monkeypatch.setattr(memory, "_llm_recall_decision", fail_recall_decision)
    monkeypatch.setattr(memory, "recall", lambda **_kwargs: [])

    context = await memory.build_memory_context("ou_1", "吴天骄", "帮我看下这个方案")

    assert llm_recall_called is False
    assert "用户画像" in context
    assert "汇报直接说结论" in context


@pytest.mark.asyncio
async def test_memory_context_adaptive_uses_deterministic_recall_for_history_turn(monkeypatch):
    from app.services import memory

    captured = {}
    llm_recall_called = False

    async def fail_recall_decision(_text):
        nonlocal llm_recall_called
        llm_recall_called = True
        return {"r": True, "t": ["预测"], "k": "Outbound"}

    def fake_recall(**kwargs):
        captured.update(kwargs)
        return [{
            "user_id": "ou_1",
            "action": "Outbound 首周销量预测 40-60w",
            "tags": ["预测", "数字"],
            "time": "2026-04-18T00:00:00+00:00",
        }]

    monkeypatch.setenv("BOT_FASTPATH_ENABLED", "1")
    monkeypatch.setenv("BOT_MEMORY_RECALL_MODE", "adaptive")
    monkeypatch.setattr(memory, "get_user_profile", lambda _user_id: {"interaction_count": 0})
    monkeypatch.setattr(memory, "_llm_recall_decision", fail_recall_decision)
    monkeypatch.setattr(memory, "recall", fake_recall)

    context = await memory.build_memory_context("ou_1", "吴天骄", "你之前猜 Outbound 是多少？")

    assert captured["query_text"] == "你之前猜 Outbound 是多少？"
    assert llm_recall_called is False
    assert "Outbound 首周销量预测" in context


@pytest.mark.asyncio
async def test_progress_hint_budget_does_not_block_on_slow_llm(monkeypatch):
    from app.services import base_agent

    async def slow_hint(*_args, **_kwargs):
        await asyncio.sleep(5)
        return "还在处理"

    monkeypatch.setattr(base_agent, "_generate_progress_hint", slow_hint)

    start = time.monotonic()
    result = await base_agent.generate_progress_hint_with_budget(
        [],
        0,
        max_wait_s=0.05,
    )
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_exit_governor_adaptive_skips_llm_for_plain_reply(monkeypatch):
    from app.services.base_agent import evaluate_exit_governor

    class FailingModels:
        async def generate_content(self, **_kwargs):
            raise AssertionError("plain deterministic pass should skip LLM judge")

    monkeypatch.setenv("BOT_FASTPATH_ENABLED", "1")
    monkeypatch.setenv("BOT_EXIT_JUDGE_MODE", "adaptive")

    decision = await evaluate_exit_governor(
        reply_text="收到，我先按两步拆。",
        user_text="帮我拆一下这个想法",
        tool_names_called=[],
        action_outcomes=[],
        gemini_client=SimpleNamespace(aio=SimpleNamespace(models=FailingModels())),
        enable_llm_judge=True,
    )

    assert decision.verdict == "pass"
    assert decision.reason == "llm_judge_skipped"


def test_context_tools_do_not_trigger_strong_model_escalation():
    from app.services import gemini_provider

    assert gemini_provider._should_escalate_after_tools(
        ["recall_memory", "fetch_chat_history"],
        task_type="normal",
    ) is False
    assert gemini_provider._should_escalate_after_tools(
        ["web_search", "fetch_url"],
        task_type="research",
    ) is True
    assert gemini_provider._should_escalate_after_tools(
        ["self_edit_file"],
        task_type="normal",
    ) is True


def test_image_view_turn_blocks_unnecessary_external_detours():
    from app.harness.tool_escalation import build_tool_domain_nudge

    nudge = build_tool_domain_nudge(
        "请查看这张图片",
        ["web_search", "list_bot_groups"],
        task_type="normal",
    )

    assert nudge is not None
    assert "当前图片" in nudge
