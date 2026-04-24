import base64
from types import SimpleNamespace

import pytest

from app.tools import xhs_ops


class _FakeXhsPage:
    url = "https://www.xiaohongshu.com/login"

    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_selector(self, *_args, **_kwargs):
        return None

    async def evaluate(self, *_args, **_kwargs):
        qr_png = base64.b64encode(b"fake-qr-png").decode("ascii")
        return f"data:image/png;base64,{qr_png}"


@pytest.mark.asyncio
async def test_xhs_login_awaits_qr_image_send(monkeypatch):
    sent_images: list[bytes] = []
    login_checks = {"count": 0}

    async def fake_sleep(_seconds):
        return None

    async def fake_get_session(_session_key):
        context = SimpleNamespace(cookies=lambda: _fake_cookies())
        return SimpleNamespace(page=_FakeXhsPage(), context=context, logged_in=False)

    async def _fake_cookies():
        return []

    async def fake_check_login_success(_page):
        login_checks["count"] += 1
        return login_checks["count"] >= 3

    async def fake_clear_cookies(_session_key):
        return None

    async def fake_save_cookies(_session_key, _cookies):
        return None

    async def fake_send_qr_image(screenshot_png: bytes) -> bool:
        sent_images.append(screenshot_png)
        return True

    monkeypatch.setattr(xhs_ops, "_check_playwright", lambda: None)
    monkeypatch.setattr(xhs_ops, "_get_tenant_id", lambda: "kf-steven-ai")
    monkeypatch.setattr(xhs_ops, "_get_session_key", lambda: "kf-steven-ai:user")
    monkeypatch.setattr(xhs_ops, "_get_or_create_xhs_session", fake_get_session)
    monkeypatch.setattr(xhs_ops, "_check_login_success", fake_check_login_success)
    monkeypatch.setattr(xhs_ops, "_clear_cookies_from_redis", fake_clear_cookies)
    monkeypatch.setattr(xhs_ops, "_save_cookies_to_redis", fake_save_cookies)
    monkeypatch.setattr(xhs_ops, "_send_qr_image_to_user", fake_send_qr_image)
    monkeypatch.setattr(xhs_ops.asyncio, "sleep", fake_sleep)

    result = await xhs_ops._handle_xhs_login({})

    assert result.ok
    assert "登录成功" in result.content
    assert sent_images == [b"fake-qr-png"]
