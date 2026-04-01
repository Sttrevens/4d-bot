from types import SimpleNamespace

from app.harness import (
    build_continuation_context,
    remember_visual_turn,
    should_reuse_recent_visual,
)
from app.harness import session_facts


def setup_function():
    session_facts._MEM_CACHE.clear()


def test_recent_visual_followup_reuses_images(monkeypatch):
    monkeypatch.setattr(session_facts, "get_current_tenant", lambda: SimpleNamespace(tenant_id="tenant-a"))
    monkeypatch.setattr(session_facts, "_get_redis_client", lambda: None)

    remember_visual_turn(
        sender_id="user-1",
        user_text="请查看这张图片，我吃了这些和一个鸡蛋羹",
        image_urls=["data:image/png;base64,abc"],
        assistant_reply="这顿里有烧鸟和鸡蛋羹。",
    )

    continuation = build_continuation_context(
        sender_id="user-1",
        user_text="我刚才那顿大概有多少卡？",
        image_urls=None,
    )

    assert continuation.reused_images == ("data:image/png;base64,abc",)
    assert "同一轮" in continuation.note
    assert "烧鸟" in continuation.note


def test_non_followup_does_not_reuse_recent_visual(monkeypatch):
    monkeypatch.setattr(session_facts, "get_current_tenant", lambda: SimpleNamespace(tenant_id="tenant-a"))
    monkeypatch.setattr(session_facts, "_get_redis_client", lambda: None)

    remember_visual_turn(
        sender_id="user-1",
        user_text="请查看这张图片",
        image_urls=["data:image/png;base64,abc"],
        assistant_reply="看到了。",
    )

    continuation = build_continuation_context(
        sender_id="user-1",
        user_text="顺便给我讲讲减脂原理",
        image_urls=None,
    )

    assert continuation.reused_images == ()
    assert continuation.note == ""
    assert should_reuse_recent_visual("我刚才那顿大概有多少卡？")
    assert not should_reuse_recent_visual("顺便给我讲讲减脂原理")
