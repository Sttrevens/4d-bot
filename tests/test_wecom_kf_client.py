import pytest


@pytest.mark.asyncio
async def test_sync_msg_invalid_callback_token_does_not_refresh_access_token(monkeypatch):
    from app.services.wecom_kf import WeComKfClient

    calls = {"token": 0, "post": 0}

    async def fake_get_token():
        calls["token"] += 1
        return "access-token"

    class FakeResponse:
        def json(self):
            return {
                "errcode": 95007,
                "errmsg": "invalid msg token",
                "msg_list": [],
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            calls["post"] += 1
            return FakeResponse()

    client = WeComKfClient()
    monkeypatch.setattr(client, "_get_token", fake_get_token)
    monkeypatch.setattr("app.services.wecom_kf.httpx.AsyncClient", FakeAsyncClient)

    result = await client.sync_msg(callback_token="expired-callback-token")

    assert result["errcode"] == 95007
    assert calls == {"token": 1, "post": 1}


def test_kf_sync_token_freshness_uses_short_recovery_window(monkeypatch):
    from app.webhook.wecom_kf_handler import _kf_sync_token_is_fresh

    monkeypatch.setenv("WECOM_KF_SYNC_TOKEN_MAX_AGE_S", "300")

    assert _kf_sync_token_is_fresh(1_000.0, now=1_250.0) is True
    assert _kf_sync_token_is_fresh(1_000.0, now=1_301.0) is False
    assert _kf_sync_token_is_fresh(0.0, now=1_001.0) is False
