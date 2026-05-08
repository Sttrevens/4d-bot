from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from app.tenant.config import ChannelConfig, TenantConfig
from app.tenant.context import set_current_channel, set_current_tenant

def test_qq_reply_preview_is_logged_truncated(caplog):
    from app.webhook.qq_handler import _log_reply_preview

    long_reply = "第一行\n" + "很长的回复" * 80

    with caplog.at_level(logging.INFO, logger="app.webhook.qq_handler"):
        _log_reply_preview("21F48F966E118A909E400F0348B27289", "p2p", long_reply, 2)

    assert "qq: reply to 21F48F96 chat=p2p chunks=2 text=" in caplog.text
    assert "第一行 / 很长的回复" in caplog.text
    assert len(caplog.records[0].message) < 360


def test_qq_send_result_is_logged_with_api_error(caplog):
    from app.webhook.qq_handler import _log_send_result

    with caplog.at_level(logging.WARNING, logger="app.webhook.qq_handler"):
        _log_send_result(
            "21F48F966E118A909E400F0348B27289",
            "p2p",
            {"error": '{"message":"请求数据异常"}', "status": 400},
            chunk_index=1,
            chunks_count=1,
        )

    assert "qq: send failed to 21F48F96 chat=p2p chunk=1/1 status=400" in caplog.text
    assert "请求数据异常" in caplog.text


@pytest.mark.asyncio
async def test_qq_self_role_question_still_uses_llm(monkeypatch):
    from app.webhook import qq_handler

    tenant = TenantConfig(
        tenant_id="pm-bot",
        platform="feishu",
        channels=[
            ChannelConfig(channel_id="pm-bot-feishu", platform="feishu"),
            ChannelConfig(
                channel_id="pm-bot-qq",
                platform="qq",
                qq_app_id="qq-app",
                qq_app_secret="qq-secret",
                qq_token="qq-token",
            ),
        ],
    )
    set_current_tenant(tenant)
    set_current_channel(tenant.get_channel("qq"))

    route_message = AsyncMock(return_value="我是耀西，四缔游戏官方社群运营，主要负责玩家答疑和反馈收集。")
    sent: list[str] = []
    monkeypatch.setattr(qq_handler, "route_message", route_message)
    monkeypatch.setattr(
        qq_handler.qq_api,
        "reply_text",
        AsyncMock(side_effect=lambda _chat_id, _msg_id, text, **_kwargs: sent.append(text) or {}),
    )

    await qq_handler._process_and_reply(
        "你是做什么的",
        "p2p:21F48F966E118A909E400F0348B27289:msg-1",
        "21F48F966E118A909E400F0348B27289",
        "p2p:21F48F966E118A909E400F0348B27289",
        "p2p",
    )

    route_message.assert_awaited_once()
    assert sent
    assert "官方社群运营" in sent[0]
    assert "玩家答疑" in sent[0]
    assert "项目运营与日程助理" not in sent[0]
    assert "CEO" not in sent[0]


def test_qq_credentials_fall_back_to_tenant_qq_channel_when_context_is_feishu():
    from app.services import qq as qq_api

    tenant = TenantConfig(
        tenant_id="pm-bot",
        platform="feishu",
        channels=[
            ChannelConfig(channel_id="pm-bot-feishu", platform="feishu"),
            ChannelConfig(
                channel_id="pm-bot-qq",
                platform="qq",
                qq_app_id="qq-app",
                qq_app_secret="qq-secret",
                qq_token="qq-token",
            ),
        ],
    )
    set_current_tenant(tenant)
    set_current_channel(tenant.get_channel("feishu"))

    assert qq_api._get_credentials() == ("qq-app", "qq-secret", "qq-token")
