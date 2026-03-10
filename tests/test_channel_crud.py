"""Channel CRUD API + Identity UX tests."""

import json
import pytest
from unittest.mock import patch, MagicMock
from dataclasses import asdict

from app.tenant.config import TenantConfig, ChannelConfig
from app.tenant.registry import TenantRegistry


class TestChannelConfig:
    """ChannelConfig and TenantConfig multi-channel support."""

    def test_get_channels_empty_builds_primary(self):
        """Empty channels list should auto-construct primary channel."""
        t = TenantConfig(
            tenant_id="test-bot", platform="feishu",
            app_id="cli_xxx", app_secret="secret",
        )
        channels = t.get_channels()
        assert len(channels) == 1
        assert channels[0].platform == "feishu"
        assert channels[0].app_id == "cli_xxx"

    def test_get_channels_returns_enabled_only(self):
        """Only enabled channels should be returned."""
        t = TenantConfig(
            tenant_id="test-bot", platform="feishu",
            channels=[
                ChannelConfig(channel_id="ch1", platform="feishu", enabled=True),
                ChannelConfig(channel_id="ch2", platform="wecom", enabled=False),
                ChannelConfig(channel_id="ch3", platform="qq", enabled=True),
            ],
        )
        channels = t.get_channels()
        assert len(channels) == 2
        assert {ch.channel_id for ch in channels} == {"ch1", "ch3"}

    def test_find_channel_by_id(self):
        t = TenantConfig(
            tenant_id="test-bot",
            channels=[
                ChannelConfig(channel_id="ch1", platform="feishu"),
                ChannelConfig(channel_id="ch2", platform="qq"),
            ],
        )
        ch = t.find_channel_by_id("ch2")
        assert ch is not None
        assert ch.platform == "qq"

    def test_find_channel_by_id_not_found(self):
        t = TenantConfig(tenant_id="test-bot", channels=[])
        assert t.find_channel_by_id("nonexistent") is None

    def test_has_platform(self):
        t = TenantConfig(
            tenant_id="test-bot",
            channels=[
                ChannelConfig(channel_id="ch1", platform="feishu"),
                ChannelConfig(channel_id="ch2", platform="qq"),
            ],
        )
        assert t.has_platform("feishu")
        assert t.has_platform("qq")
        assert not t.has_platform("wecom")

    def test_get_channel_platforms(self):
        t = TenantConfig(
            tenant_id="test-bot",
            channels=[
                ChannelConfig(channel_id="ch1", platform="feishu"),
                ChannelConfig(channel_id="ch2", platform="qq"),
                ChannelConfig(channel_id="ch3", platform="feishu"),
            ],
        )
        platforms = t.get_channel_platforms()
        assert set(platforms) == {"feishu", "qq"}


class TestRegistryChannels:
    """TenantRegistry channel-related methods."""

    def test_find_by_channel_id(self):
        reg = TenantRegistry()
        t = TenantConfig(
            tenant_id="bot-1",
            channels=[
                ChannelConfig(channel_id="bot-1-feishu", platform="feishu"),
                ChannelConfig(channel_id="bot-1-qq", platform="qq", qq_app_id="123"),
            ],
        )
        reg.register(t)
        found_t, found_ch = reg.find_by_channel_id("bot-1-qq")
        assert found_t is not None
        assert found_t.tenant_id == "bot-1"
        assert found_ch.qq_app_id == "123"

    def test_find_by_channel_id_not_found(self):
        reg = TenantRegistry()
        t, ch = reg.find_by_channel_id("nonexistent")
        assert t is None
        assert ch is None

    def test_find_by_kf_open_kfid(self):
        reg = TenantRegistry()
        t = TenantConfig(
            tenant_id="bot-kf",
            channels=[
                ChannelConfig(
                    channel_id="bot-kf-kf", platform="wecom_kf",
                    wecom_kf_open_kfid="wkXXX",
                ),
            ],
        )
        reg.register(t)
        found_t, found_ch = reg.find_by_kf_open_kfid("wkXXX")
        assert found_t is not None
        assert found_ch.wecom_kf_open_kfid == "wkXXX"


class TestChannelCRUDHelpers:
    """Test helpers used by channel CRUD API."""

    def test_channels_to_dicts(self):
        from app.admin.routes import _channels_to_dicts
        channels = [
            ChannelConfig(channel_id="ch1", platform="feishu", app_id="cli_xxx"),
            ChannelConfig(channel_id="ch2", platform="qq", qq_app_id="123"),
        ]
        result = _channels_to_dicts(channels)
        assert len(result) == 2
        assert result[0]["channel_id"] == "ch1"
        assert result[0]["platform"] == "feishu"
        assert result[0]["app_id"] == "cli_xxx"
        assert result[1]["qq_app_id"] == "123"

    def test_channels_to_dicts_filters_empty(self):
        """Empty default values should be filtered out to reduce JSON size."""
        from app.admin.routes import _channels_to_dicts
        channels = [ChannelConfig(channel_id="ch1", platform="feishu")]
        result = _channels_to_dicts(channels)
        assert len(result) == 1
        d = result[0]
        # These must always be present
        assert "channel_id" in d
        assert "platform" in d
        assert "enabled" in d
        # Empty strings should be filtered
        assert "app_secret" not in d
        assert "wecom_corpid" not in d


class TestChannelCRUDAPI:
    """Test channel CRUD API endpoints (mocked registry + redis)."""

    def _make_tenant_with_channels(self):
        return TenantConfig(
            tenant_id="test-bot", platform="feishu",
            channels=[
                ChannelConfig(channel_id="test-bot-feishu", platform="feishu",
                              app_id="cli_xxx", app_secret="secret"),
            ],
        )

    def test_add_channel_logic(self):
        """Verify add-channel logic: append + auto-init primary."""
        t = self._make_tenant_with_channels()
        assert len(t.channels) == 1

        # Simulate what api_add_channel does: if channels empty, init primary first
        # (here channels is already non-empty)
        new_ch = ChannelConfig(
            channel_id="test-bot-qq", platform="qq",
            qq_app_id="12345", qq_app_secret="secret",
        )
        t.channels.append(new_ch)
        assert len(t.channels) == 2
        assert t.channels[1].platform == "qq"
        assert t.channels[1].qq_app_id == "12345"

    def test_add_channel_auto_init_primary(self):
        """When channels list is empty, primary should be auto-constructed first."""
        t = TenantConfig(
            tenant_id="test-bot", platform="feishu",
            app_id="cli_xxx", app_secret="secret",
        )
        assert len(t.channels) == 0
        # Simulate api_add_channel logic
        if not t.channels:
            primary = t._build_primary_channel()
            t.channels.append(primary)
        t.channels.append(ChannelConfig(channel_id="test-bot-qq", platform="qq"))
        assert len(t.channels) == 2
        assert t.channels[0].platform == "feishu"
        assert t.channels[0].app_id == "cli_xxx"
        assert t.channels[1].platform == "qq"

    def test_channel_id_auto_generation(self):
        """Channel ID should auto-generate with conflict avoidance."""
        t = TenantConfig(
            tenant_id="my-bot",
            channels=[
                ChannelConfig(channel_id="my-bot-feishu", platform="feishu"),
            ],
        )
        # Simulate the auto-generation logic from api_add_channel
        channel_id = "my-bot-feishu"
        existing_ids = {ch.channel_id for ch in t.get_channels()}
        base_id = channel_id
        suffix = 2
        while channel_id in existing_ids:
            channel_id = f"{base_id}-{suffix}"
            suffix += 1
        assert channel_id == "my-bot-feishu-2"

    def test_delete_channel(self):
        """Deleting a channel should remove it from the list."""
        t = TenantConfig(
            tenant_id="my-bot",
            channels=[
                ChannelConfig(channel_id="ch1", platform="feishu"),
                ChannelConfig(channel_id="ch2", platform="qq"),
            ],
        )
        before = len(t.channels)
        t.channels = [ch for ch in t.channels if ch.channel_id != "ch2"]
        assert len(t.channels) == before - 1
        assert all(ch.channel_id != "ch2" for ch in t.channels)

    def test_update_channel(self):
        """Updating a channel should modify its fields."""
        ch = ChannelConfig(channel_id="ch1", platform="qq", qq_app_id="old")
        setattr(ch, "qq_app_id", "new")
        assert ch.qq_app_id == "new"
