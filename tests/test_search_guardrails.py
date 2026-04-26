from app.harness.search_guardrails import (
    extract_focus_terms,
    is_query_off_topic,
    is_temporal_scope_drift_query,
    rewrite_web_search_query,
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


def test_rewrite_injects_current_year_for_temporal_sports_query():
    rewritten = rewrite_web_search_query(
        "NBA playoffs bracket matchups",
        user_text="现在NBA季后赛正式出炉了，给我预测每轮比分",
        current_year=2026,
    )
    assert "2026" in rewritten


def test_rewrite_prefers_nba_authoritative_domains():
    rewritten = rewrite_web_search_query(
        "NBA playoff bracket",
        user_text="现在NBA季后赛正式出炉了",
        current_year=2026,
    )
    assert "site:nba.com" in rewritten


def test_rewrite_prefers_weather_authoritative_domains():
    rewritten = rewrite_web_search_query(
        "上海今天降雨和气温",
        user_text="现在天气怎么样",
        current_year=2026,
    )
    assert "site:weather.com" in rewritten or "site:noaa.gov" in rewritten


def test_rewrite_xd_latest_report_query_prefers_official_ir_not_steam():
    rewritten = rewrite_web_search_query(
        "心动网络 AI 游戏 研发 进展",
        user_text="帮我看下心动最新的报告里提到没提到在研的AI游戏，有多少相关资料",
        current_year=2026,
    )
    assert "site:2400.hk" in rewritten
    assert "site:hkexnews.hk" in rewritten
    assert "site:store.steampowered.com" not in rewritten
    assert "site:.gov" not in rewritten


def test_rewrite_xd_annual_results_query_avoids_us_finance_domains():
    rewritten = rewrite_web_search_query(
        "XD Inc 2025 annual results AI strategy",
        user_text="帮我看下心动最新的报告里提到没提到在研的AI游戏，有多少相关资料",
        current_year=2026,
    )
    assert "site:2400.hk" in rewritten
    assert "site:hkexnews.hk" in rewritten
    assert "finance.yahoo.com" not in rewritten
    assert "investing.com" not in rewritten
    assert "site:sec.gov" not in rewritten


def test_ai_market_forecast_does_not_trigger_weather_domains():
    rewritten = rewrite_web_search_query(
        "IDC AI agent market forecast and business impact",
        user_text="帮我梳理智能体商业化和获客雷达的真实市场判断",
        current_year=2026,
    )
    assert "weather.com" not in rewritten
    assert "noaa.gov" not in rewritten


def test_ai_roi_report_does_not_trigger_finance_or_gov_domains():
    rewritten = rewrite_web_search_query(
        "McKinsey 2024 2025 AI agent business impact report ROI",
        user_text="客户想理解智能体业务规划和商业回报",
        current_year=2026,
    )
    assert "finance.yahoo.com" not in rewritten
    assert "investing.com" not in rewritten
    assert "site:sec.gov" not in rewritten
    assert "site:.gov" not in rewritten


def test_creator_economy_ai_report_keeps_general_query():
    rewritten = rewrite_web_search_query(
        "2026 creator economy trends AI monetization knowledge IP report",
        user_text="帮我找知识IP用AI分身变现的真实趋势",
        current_year=2026,
    )
    assert "weather.com" not in rewritten
    assert "finance.yahoo.com" not in rewritten
    assert "site:.gov" not in rewritten


def test_temporal_scope_drift_query_detects_future_projection_on_now_turn():
    user_text = "现在NBA季后赛正式出炉了，给我做每轮比分预测"
    assert is_temporal_scope_drift_query(
        "NBA future power rankings three-year outlook 2026",
        user_text,
    )
    assert not is_temporal_scope_drift_query(
        "NBA playoff bracket matchups standings 2026",
        user_text,
    )
