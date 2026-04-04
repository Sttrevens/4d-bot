from app.tools.xhs_ops import _extract_xhs_query_signals, _rank_xhs_user_candidates


def test_extract_xhs_query_signals_separates_anchor_and_modifiers():
    anchors, modifiers = _extract_xhs_query_signals("桃桃 coser 纽约")

    assert anchors == ["桃桃"]
    assert "coser" in modifiers
    assert "纽约" in modifiers


def test_rank_xhs_user_candidates_prefers_exact_name_with_constraints():
    ranked = _rank_xhs_user_candidates(
        [
            {
                "nickname": "雪山没有山",
                "desc": "北京 | 纽约 | NY留学生艰难搞二次元中",
                "red_id": "759245921",
            },
            {
                "nickname": "🍑桃桃🍑",
                "desc": "没有什么比搞考斯更开心咯 一起去漫展",
                "red_id": "1868861940",
            },
        ],
        "桃桃 coser 纽约",
    )

    assert ranked[0]["nickname"] == "🍑桃桃🍑"
    assert ranked[0]["_score"] > ranked[1]["_score"]
    assert any("桃桃" in reason for reason in ranked[0]["_reasons"])
