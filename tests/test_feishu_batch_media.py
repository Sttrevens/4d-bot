from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_feishu_codex_batch_keeps_all_media_in_initial_message(monkeypatch):
    from app.tenant import context as tenant_context
    from app.webhook import handler

    tenant = SimpleNamespace(tenant_id="pm-bot", codex_channel_enabled=True)
    token = tenant_context._current_tenant.set(tenant)
    sender_id = "ou_test_multi_media"
    captured: dict = {}
    created_tasks = []

    async def fake_process_and_reply(
        user_text,
        image_urls,
        message_id,
        sender_id,
        chat_id,
        chat_type,
    ):
        captured.update(
            {
                "user_text": user_text,
                "image_urls": image_urls,
                "message_id": message_id,
                "sender_id": sender_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
            }
        )

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    try:
        handler._user_pending.clear()
        handler._batch_timers.clear()
        handler._state.deactivate(sender_id)
        handler._user_pending[handler.tuk(sender_id)] = [
            {
                "text": "把我日历里原来那两个上海北京往返行程替换成这两个",
                "images": [
                    "data:image/jpeg;base64,FIRST",
                    "data:image/jpeg;base64,SECOND",
                ],
                "message_id": "om_multi_1",
                "chat_id": "oc_multi_1",
                "chat_type": "p2p",
            }
        ]

        monkeypatch.setattr(handler, "_BATCH_WAIT", 0)
        monkeypatch.setattr(handler, "_process_and_reply", fake_process_and_reply)
        monkeypatch.setattr(handler.asyncio, "create_task", fake_create_task)

        await handler._flush_after_wait(sender_id)

        assert captured["image_urls"] == [
            "data:image/jpeg;base64,FIRST",
            "data:image/jpeg;base64,SECOND",
        ]
        assert "正在逐个发送" not in captured["user_text"]
        assert created_tasks == []
    finally:
        handler._user_pending.clear()
        handler._batch_timers.clear()
        handler._state.deactivate(sender_id)
        tenant_context._current_tenant.reset(token)
