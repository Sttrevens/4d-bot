from __future__ import annotations

import json

import pytest

from app.tenant.config import ChannelConfig, TenantConfig
from app.tenant.context import get_current_channel, set_current_channel


def _patch_redis(monkeypatch, data: dict[str, dict]) -> None:
    from app.services import tenant_sync

    encoded = {k: json.dumps(v, ensure_ascii=False) for k, v in data.items()}

    def fake_execute(command: str, *args):
        cmd = command.upper()
        if cmd == "SCAN":
            pattern = args[2]
            prefix = pattern.replace("*", "")
            return ["0", [k for k in encoded if k.startswith(prefix)]]
        if cmd == "GET":
            return encoded.get(args[0])
        return None

    monkeypatch.setattr(tenant_sync.redis, "available", lambda: True)
    monkeypatch.setattr(tenant_sync.redis, "execute", fake_execute)


def test_load_persisted_tenants_merges_channel_overlay_for_existing_local_tenant(monkeypatch):
    from app.services import tenant_sync
    from app.tenant.registry import tenant_registry

    old_tenants = tenant_registry._tenants.copy()
    try:
        tenant_registry._tenants.clear()
        tenant = TenantConfig(
            tenant_id="pm-bot",
            platform="feishu",
            app_id="cli-feishu",
            app_secret="feishu-secret",
        )
        tenant_registry.register(tenant)

        _patch_redis(monkeypatch, {
            "tenant_cfg:pm-bot": {
                "tenant_id": "pm-bot",
                "channels": [
                    {
                        "channel_id": "pm-bot-qq",
                        "platform": "qq",
                        "qq_app_id": "qq-app",
                        "qq_app_secret": "qq-secret",
                        "qq_token": "qq-token",
                    }
                ],
            }
        })

        tenant_sync.load_persisted_tenants()

        feishu = tenant.get_channel("feishu")
        qq = tenant.get_channel("qq")
        assert feishu is not None
        assert feishu.app_id == "cli-feishu"
        assert qq is not None
        assert qq.qq_app_id == "qq-app"
        assert qq.qq_app_secret == "qq-secret"
    finally:
        tenant_registry._tenants.clear()
        tenant_registry._tenants.update(old_tenants)


def test_hydrate_persisted_channels_for_tenant_reads_admin_overlay(monkeypatch):
    from app.services.tenant_sync import hydrate_persisted_channels_for_tenant

    tenant = TenantConfig(
        tenant_id="pm-bot",
        platform="feishu",
        app_id="cli-feishu",
        app_secret="feishu-secret",
    )
    _patch_redis(monkeypatch, {
        "admin:tenant:pm-bot": {
            "tenant_id": "pm-bot",
            "channels": [
                {
                    "channel_id": "pm-bot-qq",
                    "platform": "qq",
                    "qq_app_id": "qq-app",
                    "qq_app_secret": "qq-secret",
                    "qq_token": "qq-token",
                }
            ],
        }
    })

    assert hydrate_persisted_channels_for_tenant(tenant) is True
    qq = tenant.get_channel("qq")
    assert qq is not None
    assert qq.qq_app_id == "qq-app"


@pytest.mark.asyncio
async def test_qq_dispatch_sets_qq_context_even_without_configured_channel(monkeypatch):
    from unittest.mock import AsyncMock

    from app.webhook import qq_handler

    tenant = TenantConfig(
        tenant_id="pm-bot",
        platform="feishu",
        app_id="cli-feishu",
        app_secret="feishu-secret",
    )
    set_current_channel(ChannelConfig(platform="feishu"))

    seen_platforms: list[str] = []

    async def fake_route_message(*args, **kwargs):
        ch = get_current_channel()
        seen_platforms.append(ch.platform if ch else "")
        return "我是耀西，四缔游戏官方社群运营。"

    monkeypatch.setattr(qq_handler, "route_message", fake_route_message)
    monkeypatch.setattr(qq_handler.qq_api, "reply_text", AsyncMock(return_value={}))
    monkeypatch.setattr(qq_handler._state, "get_mode", lambda _sender: "safe")

    payload = {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "id": "evt-qq-context-test",
        "d": {
            "id": "msg-qq-context-test",
            "content": "你是做什么的",
            "author": {"user_openid": "qq-openid-1"},
        },
    }

    await qq_handler._dispatch_message(tenant, payload)

    assert seen_platforms == ["qq"]
