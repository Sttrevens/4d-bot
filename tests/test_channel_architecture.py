"""Tests for the multi-channel architecture (borrowed from OpenClaw).

Tests:
- ChannelCapabilities declaration
- AgentProfile routing/binding
- EventBus cross-channel communication
- Markdown-aware message chunking
"""

from __future__ import annotations

import asyncio
import pytest

from app.channels.base import (
    ChannelCapabilities,
    FEISHU_CAPABILITIES,
    WECOM_CAPABILITIES,
    QQ_CAPABILITIES,
    DISCORD_CAPABILITIES,
)
from app.channels.routing import (
    AgentProfile,
    AgentBinding,
    AgentBindingMatch,
    resolve_agent_profile,
    parse_profiles_from_config,
    parse_bindings_from_config,
)
from app.channels.event_bus import EventBus, ChannelEvent
from app.channels.chunking import chunk_markdown


# ── ChannelCapabilities ──

class TestChannelCapabilities:
    def test_feishu_has_full_capabilities(self):
        cap = FEISHU_CAPABILITIES
        assert cap.documents
        assert cap.calendar
        assert cap.bitable
        assert cap.mail
        assert cap.oauth

    def test_wecom_limited_capabilities(self):
        cap = WECOM_CAPABILITIES
        assert not cap.documents
        assert not cap.calendar
        assert cap.file_upload

    def test_qq_capabilities(self):
        cap = QQ_CAPABILITIES
        assert cap.reactions
        assert not cap.documents

    def test_default_capabilities_empty(self):
        cap = ChannelCapabilities()
        assert not cap.documents
        assert not cap.calendar
        assert not cap.reactions


# ── AgentProfile Routing ──

class TestAgentRouting:
    def _make_profiles(self):
        return [
            AgentProfile(profile_id="support", name="客服"),
            AgentProfile(profile_id="dev", name="开发助手"),
            AgentProfile(profile_id="vip", name="VIP 专属"),
        ]

    def test_platform_binding(self):
        profiles = self._make_profiles()
        bindings = [
            AgentBinding(match=AgentBindingMatch(platform="discord"), profile_id="support"),
            AgentBinding(match=AgentBindingMatch(platform="feishu"), profile_id="dev"),
        ]
        result = resolve_agent_profile(profiles, bindings, platform="discord")
        assert result is not None
        assert result.profile_id == "support"

    def test_chat_id_overrides_platform(self):
        profiles = self._make_profiles()
        bindings = [
            AgentBinding(match=AgentBindingMatch(platform="feishu"), profile_id="dev"),
            AgentBinding(match=AgentBindingMatch(chat_id="oc_vip_123"), profile_id="vip"),
        ]
        # chat_id 匹配优先于 platform
        result = resolve_agent_profile(
            profiles, bindings,
            platform="feishu", chat_id="oc_vip_123",
        )
        assert result is not None
        assert result.profile_id == "vip"

    def test_sender_id_highest_priority(self):
        profiles = self._make_profiles()
        bindings = [
            AgentBinding(match=AgentBindingMatch(platform="feishu"), profile_id="dev"),
            AgentBinding(match=AgentBindingMatch(chat_id="oc_123"), profile_id="support"),
            AgentBinding(match=AgentBindingMatch(sender_id="ou_boss"), profile_id="vip"),
        ]
        result = resolve_agent_profile(
            profiles, bindings,
            platform="feishu", chat_id="oc_123", sender_id="ou_boss",
        )
        assert result is not None
        assert result.profile_id == "vip"

    def test_no_match_returns_none(self):
        profiles = self._make_profiles()
        bindings = [
            AgentBinding(match=AgentBindingMatch(platform="discord"), profile_id="support"),
        ]
        result = resolve_agent_profile(profiles, bindings, platform="feishu")
        assert result is None

    def test_empty_profiles_returns_none(self):
        result = resolve_agent_profile([], [], platform="feishu")
        assert result is None

    def test_missing_profile_id_returns_none(self):
        profiles = [AgentProfile(profile_id="dev")]
        bindings = [
            AgentBinding(match=AgentBindingMatch(platform="feishu"), profile_id="nonexistent"),
        ]
        result = resolve_agent_profile(profiles, bindings, platform="feishu")
        assert result is None

    def test_combined_match_higher_score(self):
        profiles = self._make_profiles()
        bindings = [
            AgentBinding(match=AgentBindingMatch(platform="feishu"), profile_id="dev"),
            AgentBinding(
                match=AgentBindingMatch(platform="feishu", chat_type="group"),
                profile_id="support",
            ),
        ]
        # platform + chat_type 比 platform alone 更精确
        result = resolve_agent_profile(
            profiles, bindings,
            platform="feishu", chat_type="group",
        )
        assert result is not None
        assert result.profile_id == "support"


class TestConfigParsing:
    def test_parse_profiles(self):
        raw = [
            {"profile_id": "a", "name": "Agent A", "system_prompt": "Be helpful"},
            {"profile_id": "b", "tools_enabled": ["web_search"]},
        ]
        profiles = parse_profiles_from_config(raw)
        assert len(profiles) == 2
        assert profiles[0].name == "Agent A"
        assert profiles[1].tools_enabled == ["web_search"]

    def test_parse_bindings(self):
        raw = [
            {"match": {"platform": "discord"}, "profile_id": "support"},
            {"match": {"chat_id": "oc_123", "sender_id": "ou_456"}, "profile_id": "vip"},
        ]
        bindings = parse_bindings_from_config(raw)
        assert len(bindings) == 2
        assert bindings[0].match.platform == "discord"
        assert bindings[1].match.chat_id == "oc_123"
        assert bindings[1].match.sender_id == "ou_456"


# ── EventBus ──

class TestEventBus:
    @pytest.mark.asyncio
    async def test_publish_subscribe(self):
        bus = EventBus()
        received = []

        async def handler(event: ChannelEvent):
            received.append(event)

        bus.subscribe("test_event", handler)
        count = await bus.publish(ChannelEvent(
            event_type="test_event",
            source_channel="discord",
            payload={"text": "hello"},
        ))
        await asyncio.sleep(0.05)  # let background task run

        assert count == 1
        assert len(received) == 1
        assert received[0].payload["text"] == "hello"

    @pytest.mark.asyncio
    async def test_wildcard_subscription(self):
        bus = EventBus()
        received = []

        async def handler(event: ChannelEvent):
            received.append(event.event_type)

        bus.subscribe("*", handler)
        await bus.publish(ChannelEvent(event_type="event_a"))
        await bus.publish(ChannelEvent(event_type="event_b"))
        await asyncio.sleep(0.05)

        assert "event_a" in received
        assert "event_b" in received

    @pytest.mark.asyncio
    async def test_source_channel_filter(self):
        bus = EventBus()
        received = []

        async def handler(event: ChannelEvent):
            received.append(event)

        bus.subscribe("feedback", handler, source_channel="discord")

        # 来自 discord 的应该收到
        await bus.publish(ChannelEvent(event_type="feedback", source_channel="discord"))
        # 来自 feishu 的应该被过滤
        await bus.publish(ChannelEvent(event_type="feedback", source_channel="feishu"))
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].source_channel == "discord"

    @pytest.mark.asyncio
    async def test_event_dedup(self):
        bus = EventBus()
        received = []

        async def handler(event: ChannelEvent):
            received.append(event)

        bus.subscribe("test", handler)
        await bus.publish(ChannelEvent(event_type="test", event_id="dedup_1"))
        await bus.publish(ChannelEvent(event_type="test", event_id="dedup_1"))  # dup
        await asyncio.sleep(0.05)

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_no_subscribers_returns_zero(self):
        bus = EventBus()
        count = await bus.publish(ChannelEvent(event_type="nobody_cares"))
        assert count == 0

    @pytest.mark.asyncio
    async def test_handler_error_doesnt_crash(self):
        bus = EventBus()

        async def bad_handler(event: ChannelEvent):
            raise ValueError("boom")

        bus.subscribe("test", bad_handler)
        count = await bus.publish(ChannelEvent(event_type="test"))
        assert count == 1  # still dispatched
        await asyncio.sleep(0.05)  # error logged but not raised

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        bus = EventBus()
        received = []

        async def handler(event: ChannelEvent):
            received.append(event)

        bus.subscribe("test", handler)
        bus.unsubscribe("test", handler)
        await bus.publish(ChannelEvent(event_type="test"))
        await asyncio.sleep(0.05)

        assert len(received) == 0


# ── Message Chunking ──

class TestChunking:
    def test_short_text_no_split(self):
        text = "Hello world"
        chunks = chunk_markdown(text, limit=100)
        assert chunks == [text]

    def test_split_at_paragraph(self):
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = chunk_markdown(text, limit=30)
        assert len(chunks) >= 2
        # 每块都不超过 limit
        for chunk in chunks:
            assert len(chunk) <= 30

    def test_split_at_code_block(self):
        text = "Before code.\n\n```python\nprint('hello')\n```\n\nAfter code."
        chunks = chunk_markdown(text, limit=40)
        assert len(chunks) >= 2

    def test_hard_cut_fallback(self):
        # 没有自然断点的长文
        text = "a" * 200
        chunks = chunk_markdown(text, limit=50)
        assert all(len(c) <= 50 for c in chunks)
        assert "".join(chunks) == text.strip()

    def test_split_preserves_content(self):
        # 用段落分隔确保不会在行中间切割
        lines = [f"Line {i}" for i in range(10)]
        text = "\n\n".join(lines)
        chunks = chunk_markdown(text, limit=50)
        rejoined = "\n\n".join(chunks)
        # 所有原始行都应该存在
        for line in lines:
            assert line in rejoined

    def test_chinese_text_split(self):
        text = "这是第一段话。这段话说了很多内容。\n\n这是第二段话。这段话也很长。"
        chunks = chunk_markdown(text, limit=30)
        assert len(chunks) >= 2
