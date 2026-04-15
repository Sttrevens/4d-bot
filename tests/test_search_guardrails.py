from app.harness.search_guardrails import (
    extract_focus_terms,
    is_query_off_topic,
)


def test_extract_focus_terms_keeps_entity_tokens():
    terms = extract_focus_terms(
        "胡说，再试试搜索这两个",
        "[最近会话话题] 你们上一段主要在聊：PRAGMATA 和 Outbound 的首周销量预测",
    )
    assert "pragmata" in terms
    assert "outbound" in terms


def test_query_off_topic_when_no_overlap_with_followup_focus():
    focus_terms = extract_focus_terms(
        "上一轮在聊 PRAGMATA、Outbound",
        "请继续查这两个游戏",
    )
    assert is_query_off_topic("Kode AI vs Claude Code", focus_terms)
    assert not is_query_off_topic("Outbound release date steam", focus_terms)
