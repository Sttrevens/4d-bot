from app.harness.tool_escalation import build_tool_settle_nudge


def test_light_advice_stops_browser_escalation_after_enough_web_info():
    nudge = build_tool_settle_nudge(
        "来个灵隐寺一小时的速通攻略",
        ["browser_open"],
        [
            ("web_search", "→ 返回了 820 字符数据"),
            ("web_search", "→ 返回了 640 字符数据"),
            ("xhs_search", "→ 失败: 小红书搜索超时"),
        ],
    )
    assert nudge is not None
    assert "不要再继续升级到 browser_open" in nudge


def test_light_advice_stops_xhs_login_after_timeout_with_some_public_info():
    nudge = build_tool_settle_nudge(
        "来个灵隐寺一小时的速通攻略",
        ["xhs_login"],
        [
            ("web_search", "→ 返回了 700 字符数据"),
            ("xhs_search", "→ 失败: 小红书搜索超时"),
        ],
    )
    assert nudge is not None
    assert "停止继续升级重工具" in nudge


def test_non_advice_turn_does_not_trigger_settle_nudge():
    nudge = build_tool_settle_nudge(
        "帮我深度调研灵隐寺文旅商业化案例并做完整报告",
        ["browser_open"],
        [
            ("web_search", "→ 返回了 820 字符数据"),
            ("web_search", "→ 返回了 640 字符数据"),
            ("xhs_search", "→ 失败: 小红书搜索超时"),
        ],
    )
    assert nudge is None

