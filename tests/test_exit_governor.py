from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.harness.control_plane import render_control_event
from app.services.base_agent import evaluate_exit_governor
from app.tenant.config import ChannelConfig, TenantConfig
from app.tenant.context import set_current_channel, set_current_tenant


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
    assert decision.event is not None
    rendered = render_control_event(decision.event, provider="openai")
    assert "中立裁判" not in rendered["content"]


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
    assert decision.event is not None
    assert "behavioral=3" in decision.event.audit_summary
    rendered = render_control_event(decision.event, provider="openai")
    assert "behavioral=3" not in rendered["content"]


@pytest.mark.asyncio
async def test_exit_governor_nudges_intermediate_payload():
    decision = await evaluate_exit_governor(
        reply_text="<tools_used>\nweb_search → 返回了 300 字符数据\n</tools_used>",
        user_text="现在NBA季后赛正式出炉了，给我每轮比分预测",
        tool_names_called=["web_search"],
        action_outcomes=[("web_search", "→ query=NBA playoffs 2026; 返回了 300 字符数据")],
        gemini_client=None,
        enable_llm_judge=False,
    )
    assert decision.verdict == "nudge"
    assert decision.reason == "deterministic.intermediate_payload"


@pytest.mark.asyncio
async def test_exit_governor_does_not_re_nudge_completed_turn_when_judge_unparseable():
    fake_client = _FakeGeminiClient(
        texts=[
            "Here is the JSON requested:",
            "UNPARSABLE",
        ]
    )
    decision = await evaluate_exit_governor(
        reply_text="这是基于已检索结果整理的最终预测结论。",
        user_text="现在NBA季后赛正式出炉了，给我每轮比分预测",
        tool_names_called=["web_search", "fetch_url"],
        action_outcomes=[
            ("web_search", "NBA playoffs 2026 bracket ..."),
            ("fetch_url", "https://www.nba.com/... 2026 bracket"),
        ],
        gemini_client=fake_client,
        enable_llm_judge=True,
    )
    assert decision.verdict == "pass"


@pytest.mark.asyncio
async def test_exit_governor_nudges_qq_feishu_persona_leak_before_grounding():
    tenant = TenantConfig(
        tenant_id="pm-bot",
        platform="feishu",
        channels=[
            ChannelConfig(channel_id="pm-bot-feishu", platform="feishu"),
            ChannelConfig(channel_id="pm-bot-qq", platform="qq"),
        ],
    )
    set_current_tenant(tenant)
    set_current_channel(tenant.get_channel("qq"))

    decision = await evaluate_exit_governor(
        reply_text=(
            "在呢在呢！我是耀西，有什么我能帮你的？"
            "不管是想对对日程、理理任务，还是有啥项目上的事要我帮忙查查，你直说就行。"
        ),
        user_text="回我求你了",
        tool_names_called=[],
        action_outcomes=[],
        gemini_client=None,
        enable_llm_judge=False,
    )

    assert decision.verdict == "nudge"
    assert decision.reason == "deterministic.qq_persona"
    assert decision.event is not None
    assert decision.nudge_text == ""
    rendered = render_control_event(
        decision.event,
        provider="openai",
        channel_platform="qq",
    )
    assert "官方社群运营" in rendered["content"]
    assert "证据" not in rendered["content"]
    assert "grounding" not in rendered["content"].lower()


@pytest.mark.asyncio
async def test_exit_governor_rewrites_qq_work_scene_deflection_for_short_flirty_turn():
    set_current_channel(ChannelConfig(platform="qq"))

    decision = await evaluate_exit_governor(
        reply_text="这种称呼不太合适哦，我们还是聊聊工作吧。今天有什么排期或文档需要我帮忙吗？",
        user_text="老婆",
        tool_names_called=[],
        action_outcomes=[],
        gemini_client=None,
        enable_llm_judge=False,
    )

    assert decision.verdict == "nudge"
    assert decision.reason == "deterministic.qq_persona"
    assert decision.event is not None
    assert decision.event.action == "rewrite_for_channel"


@pytest.mark.asyncio
async def test_exit_governor_grounding_returns_structured_control_event():
    decision = await evaluate_exit_governor(
        reply_text="执行董事：张三，监事：李四，总经理：王五。",
        user_text="这家公司现在的管理层有哪些人？",
        tool_names_called=[],
        action_outcomes=[],
        gemini_client=None,
        enable_llm_judge=False,
    )

    assert decision.verdict == "grounding"
    assert decision.reason == "deterministic.grounding"
    assert decision.event is not None
    assert decision.event.kind == "exit_governor"
    assert decision.event.reason_code == "deterministic.grounding"
    assert decision.nudge_text == ""
    rendered = render_control_event(decision.event, provider="openai")
    assert "证据账本" not in rendered["content"]
    assert "grounding" not in rendered["content"].lower()
