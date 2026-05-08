from app.services import memory


def test_numeric_prediction_facts_are_saved_for_later_keyword_recall(monkeypatch):
    saved_entries = []

    def fake_append_journal(entry, channel_id=""):
        saved_entries.append(entry)
        return len(saved_entries)

    monkeypatch.setattr(memory.memory_store, "append_journal", fake_append_journal)
    monkeypatch.setattr(memory, "_append_index", lambda *_args, **_kwargs: None)

    user_id = "ou_743c3f5d599cbe5621934727a20e8551"
    memory.remember_numeric_facts(
        user_id=user_id,
        user_name="吴天骄",
        user_text="继续猜这些游戏的销量",
        reply=(
            "4. **Outbound** (愿望单 897.5k)\n"
            "看名字和愿望单量级，应该是一匹黑马。\n"
            "**耀西预测：92,000**"
        ),
    )

    assert saved_entries
    entry = saved_entries[0]
    assert entry["type"] == "numeric_fact"
    assert entry["user_id"] == user_id[:12]
    assert "Outbound" in entry["action"]
    assert "92,000" in entry["action"]
    assert "愿望单 897.5k" in entry["action"]
    assert "预测" in entry["tags"]


def test_recall_can_find_old_numeric_prediction_fact_by_entity(monkeypatch):
    uid = "ou_743c3f5d599cbe5621934727a20e8551"
    compact_uid = uid[:12]
    old_prediction = {
        "type": "numeric_fact",
        "user_id": compact_uid,
        "user_name": "吴天骄",
        "action": "数字事实: Outbound 愿望单 140万；首周销量预测 40-60w",
        "tags": ["预测", "数字"],
        "time": "2026-04-18T00:00:00+00:00",
    }
    noise = [
        {
            "user_id": compact_uid,
            "action": f"无关话题{i}",
            "tags": ["其他"],
            "time": f"2026-05-01T00:00:{i:02d}+00:00",
        }
        for i in range(120)
    ]

    def fake_read_journal_safe(limit: int = 100):
        if limit <= 0:
            return [old_prediction, *noise]
        return noise[:limit]

    monkeypatch.setattr(memory, "_read_journal_safe", fake_read_journal_safe)

    hits = memory.recall(
        user_id=uid,
        keyword="outbound",
        query_text="你之前猜outbound是40-60w，你记得么？",
        limit=5,
    )

    assert any("40-60w" in h.get("action", "") for h in hits)


def test_diary_prompt_requires_entity_number_time_scope_and_conclusion():
    assert "实体名" in memory._DIARY_PROMPT
    assert "数字" in memory._DIARY_PROMPT
    assert "时间口径" in memory._DIARY_PROMPT
    assert "结论" in memory._DIARY_PROMPT
