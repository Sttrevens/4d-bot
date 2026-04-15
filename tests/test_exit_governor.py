from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.base_agent import evaluate_exit_governor


class _FakeModels:
    def __init__(self, texts: list[str]):
        self._texts = list(texts)

    async def generate_content(self, **kwargs):  # noqa: ARG002 - keep fake signature flexible
        text = self._texts.pop(0) if self._texts else ""
        return SimpleNamespace(text=text)


class _FakeAio:
    def __init__(self, texts: list[str]):
        self.models = _FakeModels(texts)


class _FakeGeminiClient:
    def __init__(self, texts: list[str]):
        self.aio = _FakeAio(texts)


@pytest.mark.asyncio
async def test_exit_governor_blocks_unverified_history_assertion():
    decision = await evaluate_exit_governor(
        reply_text="你之前说过你乳糖完全耐受。",
        user_text="不对吧，我之前明明说过自己会拉肚子。",
        tool_names_called=[],
        action_outcomes=[],
        gemini_client=None,
        enable_llm_judge=False,
    )
    assert decision.verdict == "nudge"
    assert decision.reason == "deterministic.history_assertion"


@pytest.mark.asyncio
async def test_exit_governor_fallback_nudges_pending_action_when_judge_unparseable():
    fake_client = _FakeGeminiClient(
        texts=[
            "Here is the JSON requested:",
            "I will keep going now.",
        ]
    )
    decision = await evaluate_exit_governor(
        reply_text="想起来了，我这就去重新搜。",
        user_text="你搜错了，再试试",
        tool_names_called=[],
        action_outcomes=[],
        gemini_client=fake_client,
        enable_llm_judge=True,
    )
    assert decision.verdict == "nudge"
    assert decision.reason == "llm.nudge"
    assert "中立裁判意见" in decision.nudge_text


@pytest.mark.asyncio
async def test_exit_governor_accepts_structured_judge_nudge_with_context():
    fake_client = _FakeGeminiClient(
        texts=[
            (
                '{"decision":"nudge","relevance":6,"factual":7,'
                '"behavioral":3,"reason":"promised work without execution"}'
            )
        ]
    )
    decision = await evaluate_exit_governor(
        reply_text="我来继续推进这个任务，稍等我处理。",
        user_text="继续。",
        tool_names_called=[],
        action_outcomes=[],
        gemini_client=fake_client,
        enable_llm_judge=True,
    )
    assert decision.verdict == "nudge"
    assert decision.reason == "llm.nudge"
    assert "behavioral=3" in decision.nudge_text
