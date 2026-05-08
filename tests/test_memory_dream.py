from datetime import datetime, timezone

from app.services import memory


def test_rebuild_profile_promotes_repeated_support_preference_and_expires_old_short_term():
    now = datetime(2026, 5, 8, tzinfo=timezone.utc)
    existing = {
        "name": "吴天骄",
        "interaction_count": 3,
        "identity_facts": [],
        "current_goals": [],
        "open_loops": ["检查 memory dashboard 是否真的可用"],
        "important_entities": [],
        "communication_style": [],
        "short_term_state": [{
            "text": "一周前因为发布延期很烦",
            "kind": "emotional_state",
            "expires_at": "2026-04-20T00:00:00+00:00",
            "confidence": 0.45,
        }],
        "support_preferences": [],
        "relationship_notes": [],
        "profile_confidence": {},
    }
    entries = [
        {
            "user_id": "ou_743c3f5d5",
            "user_name": "吴天骄",
            "action": "以后我情绪低落的时候别急着讲大道理，先直接陪我把问题拆清楚。",
            "tags": ["记忆"],
            "time": "2026-05-01T00:00:00+00:00",
        },
        {
            "user_id": "ou_743c3f5d5",
            "user_name": "吴天骄",
            "action": "我还是希望你在我烦的时候先陪我拆问题，不要上来讲大道理。",
            "tags": ["记忆"],
            "time": "2026-05-03T00:00:00+00:00",
        },
        {
            "user_id": "ou_743c3f5d5",
            "user_name": "吴天骄",
            "action": "已完成检查 memory dashboard 是否真的可用",
            "tags": ["记忆"],
            "time": "2026-05-04T00:00:00+00:00",
        },
    ]

    rebuilt = memory.rebuild_user_profile_from_entries(
        "ou_743c3f5d599cbe5621934727a20e8551",
        entries,
        existing_profile=existing,
        now=now,
    )

    assert not rebuilt["short_term_state"]
    assert any("讲大道理" in item for item in rebuilt["support_preferences"])
    assert "检查 memory dashboard 是否真的可用" not in rebuilt["open_loops"]
    assert "检查 memory dashboard 是否真的可用" in rebuilt["resolved_open_loops"]
    assert rebuilt["memory_last_consolidated"].startswith("2026-05-08")


def test_rebuild_profile_keeps_one_off_emotion_short_term():
    rebuilt = memory.rebuild_user_profile_from_entries(
        "ou_743c3f5d599cbe5621934727a20e8551",
        [{
            "user_id": "ou_743c3f5d5",
            "user_name": "吴天骄",
            "action": "今天真的很烦，发布又延期了，先别给我讲大道理。",
            "tags": ["记忆"],
            "time": "2026-05-08T00:00:00+00:00",
        }],
        existing_profile={"name": "吴天骄", "interaction_count": 1},
        now=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )

    assert any("发布又延期" in item["text"] for item in rebuilt["short_term_state"])
    assert not rebuilt["emotional_patterns"]
