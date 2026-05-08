from app.tools import memory_ops


def test_recall_memory_tool_passes_query_text(monkeypatch):
    captured = {}

    def fake_recall(*, user_id="", tags=None, keyword="", limit=10, query_text=""):
        captured["user_id"] = user_id
        captured["keyword"] = keyword
        captured["limit"] = limit
        captured["query_text"] = query_text
        return [{
            "user_id": user_id,
            "action": "ok",
            "tags": [],
            "time": "2026-05-08T00:00:00+00:00",
        }]

    monkeypatch.setattr(memory_ops.mem, "recall", fake_recall)
    result = memory_ops.recall_memory({
        "user_id": "u1",
        "keyword": "搜索",
        "query_text": "胡说，再试试搜索这两个",
        "limit": 5,
    })
    assert result.ok
    assert captured["query_text"] == "胡说，再试试搜索这两个"
