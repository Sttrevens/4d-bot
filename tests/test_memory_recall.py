import asyncio

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


def test_write_diary_updates_rich_user_model_from_diary(monkeypatch):
    stored = {}
    journal = []

    def fake_read_json(key):
        return stored.get(key)

    def fake_write_json(key, value):
        stored[key] = value
        return True

    def fake_append_journal(entry, channel_id=""):
        journal.append(entry)
        return len(journal)

    async def fake_diary(*_args, **_kwargs):
        return {
            "w": True,
            "s": "讨论 dashboard 记忆质量，希望像陪伴助手一样理解用户",
            "t": ["记忆", "产品"],
            "p": [],
            "sol": False,
            "uf": ["吴天骄负责推进 4D bot 的产品体验和线上质量"],
            "g": ["让 bot 记住用户是谁、在做什么、需要什么"],
            "ol": ["检查 dashboard memory 入口中记忆内容是否有用"],
            "ent": ["pm-bot", "4d-bot", "memory dashboard"],
            "style": ["喜欢直接指出问题并要求落地修复"],
            "need": "希望 bot 的长期记忆像智能体或陪伴助手一样形成用户理解",
        }

    monkeypatch.setattr(memory.memory_store, "read_json", fake_read_json)
    monkeypatch.setattr(memory.memory_store, "write_json", fake_write_json)
    monkeypatch.setattr(memory.memory_store, "append_journal", fake_append_journal)
    monkeypatch.setattr(memory, "_append_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(memory, "_llm_diary_entry", fake_diary)
    monkeypatch.setattr(memory, "remember_numeric_facts", lambda **_kwargs: 0)

    asyncio.run(memory.write_diary(
        user_id="ou_743c3f5d599cbe5621934727a20e8551",
        user_name="吴天骄",
        user_text="dashboard里的memory记得东西都没什么卵用",
        reply="你说得对，我会修成更像智能体的长期用户模型。",
    ))

    profile = stored["users/ou_743c3f5d5"]
    assert "吴天骄负责推进 4D bot 的产品体验和线上质量" in profile["identity_facts"]
    assert "让 bot 记住用户是谁、在做什么、需要什么" in profile["current_goals"]
    assert "检查 dashboard memory 入口中记忆内容是否有用" in profile["open_loops"]
    assert "memory dashboard" in profile["important_entities"]
    assert "喜欢直接指出问题并要求落地修复" in profile["communication_style"]
    assert profile["last_user_need"] == "希望 bot 的长期记忆像智能体或陪伴助手一样形成用户理解"


def test_build_memory_context_includes_rich_user_model(monkeypatch):
    profile = {
        "name": "吴天骄",
        "interaction_count": 7,
        "preferences": ["汇报: 直接说结论"],
        "recent_topics": ["记忆", "dashboard"],
        "identity_facts": ["吴天骄负责推进 4D bot 的产品体验和线上质量"],
        "current_goals": ["提升 bot 长期记忆的用户理解能力"],
        "open_loops": ["验证 memory dashboard 能否查到有效画像"],
        "important_entities": ["pm-bot", "Outbound"],
        "communication_style": ["喜欢直接指出问题并要求落地修复"],
        "last_user_need": "希望 bot 能自然记住用户是谁、在做什么、需要什么",
    }

    async def no_recall_decision(_text):
        return {"r": False, "t": [], "k": ""}

    monkeypatch.setattr(memory, "get_user_profile", lambda _user_id: profile)
    monkeypatch.setattr(memory, "_llm_recall_decision", no_recall_decision)
    monkeypatch.setattr(memory, "recall", lambda **_kwargs: [])

    context = asyncio.run(memory.build_memory_context(
        "ou_743c3f5d599cbe5621934727a20e8551",
        "吴天骄",
        "继续处理记忆系统",
    ))

    assert "身份/背景" in context
    assert "当前目标/需要" in context
    assert "未完成事项" in context
    assert "重要对象" in context
    assert "沟通风格" in context
    assert "长期记忆的用户理解能力" in context


def test_diary_prompt_requires_rich_user_model_fields():
    assert "用户是谁" in memory._DIARY_PROMPT
    assert "正在做什么" in memory._DIARY_PROMPT
    assert "需要什么" in memory._DIARY_PROMPT
    assert '"uf"' in memory._DIARY_PROMPT
    assert '"g"' in memory._DIARY_PROMPT
    assert '"ol"' in memory._DIARY_PROMPT
