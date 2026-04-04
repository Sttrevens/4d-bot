from app.services.gemini_provider import _build_empty_model_reply, _build_factual_summary


def test_empty_model_reply_never_uses_mechanical_ai_wording_without_tools():
    reply = _build_empty_model_reply("搜的怎么样了", [], [])
    assert "AI 返回了空结果" not in reply
    assert "继续" in reply


def test_empty_model_reply_summarizes_completed_work_naturally():
    reply = _build_empty_model_reply(
        "小红书搜一下",
        ["xhs_search"],
        [("xhs_search", "找到 3 个候选结果")],
        repeated=True,
    )
    assert "AI 连续返回空响应" not in reply
    assert "以下是已完成的操作：" in reply
    assert "xhs_search: 找到 3 个候选结果" in reply


def test_factual_summary_without_actions_uses_continue_style_copy():
    reply = _build_factual_summary([], [])
    assert "AI 返回了空结果" not in reply
    assert "继续" in reply
