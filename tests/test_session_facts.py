from types import SimpleNamespace

from app.harness import (
    build_continuation_context,
    remember_active_constraints,
    remember_recent_topic,
    remember_visual_turn,
    should_reuse_active_constraints,
    should_reuse_recent_topic,
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


def test_recent_topic_followup_reuses_previous_topic(monkeypatch):
    monkeypatch.setattr(session_facts, "get_current_tenant", lambda: SimpleNamespace(tenant_id="tenant-a"))
    monkeypatch.setattr(session_facts, "_get_redis_client", lambda: None)

    remember_recent_topic(
        sender_id="user-1",
        user_text="妈妈要去灵隐寺啦 给妈妈个攻略吧？ 妈妈想给家人求健康，想给自己求财",
        image_urls=None,
        assistant_reply="先去药师殿求健康，再去北高峰财神庙求财。",
    )

    continuation = build_continuation_context(
        sender_id="user-1",
        user_text="来个速通版，来个一小时的",
        image_urls=None,
    )

    assert continuation.reused_images == ()
    assert "最近会话话题" in continuation.note
    assert "灵隐寺" in continuation.note
    assert "药师殿" in continuation.note
    assert should_reuse_recent_topic("来个速通版，来个一小时的")


def test_short_greeting_does_not_override_recent_topic(monkeypatch):
    monkeypatch.setattr(session_facts, "get_current_tenant", lambda: SimpleNamespace(tenant_id="tenant-a"))
    monkeypatch.setattr(session_facts, "_get_redis_client", lambda: None)

    remember_recent_topic(
        sender_id="user-1",
        user_text="妈妈要去灵隐寺啦 给妈妈个攻略吧？",
        image_urls=None,
        assistant_reply="可以先礼佛，再去北高峰。",
    )
    remember_recent_topic(
        sender_id="user-1",
        user_text="宝宝",
        image_urls=None,
        assistant_reply="我在呢。",
    )

    continuation = build_continuation_context(
        sender_id="user-1",
        user_text="来个速通版",
        image_urls=None,
    )

    assert "灵隐寺" in continuation.note


def test_active_constraints_followup_reuses_previous_constraints(monkeypatch):
    monkeypatch.setattr(session_facts, "get_current_tenant", lambda: SimpleNamespace(tenant_id="tenant-a"))
    monkeypatch.setattr(session_facts, "_get_redis_client", lambda: None)

    remember_recent_topic(
        sender_id="user-1",
        user_text="帮我在小红书找一下桃桃",
        image_urls=None,
        assistant_reply="我先去搜搜这个人。",
    )
    remember_active_constraints(
        sender_id="user-1",
        user_text="是个coser",
        image_urls=None,
    )

    continuation = build_continuation_context(
        sender_id="user-1",
        user_text="继续搜",
        image_urls=None,
    )

    assert "最近会话话题" in continuation.note
    assert "当前任务约束" in continuation.note
    assert "是个coser" in continuation.note
    assert should_reuse_active_constraints("继续搜")


def test_multiple_constraints_are_merged_for_followup(monkeypatch):
    monkeypatch.setattr(session_facts, "get_current_tenant", lambda: SimpleNamespace(tenant_id="tenant-a"))
    monkeypatch.setattr(session_facts, "_get_redis_client", lambda: None)

    remember_active_constraints(sender_id="user-1", user_text="是个coser", image_urls=None)
    remember_active_constraints(sender_id="user-1", user_text="不是电影号", image_urls=None)

    continuation = build_continuation_context(
        sender_id="user-1",
        user_text="搜的怎么样了",
        image_urls=None,
    )

    assert "是个coser" in continuation.note
    assert "不是电影号" in continuation.note
