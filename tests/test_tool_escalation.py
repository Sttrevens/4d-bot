from app.harness.tool_escalation import build_tool_domain_nudge, build_tool_settle_nudge


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


def test_research_turn_blocks_task_calendar_tool_drift():
    nudge = build_tool_domain_nudge(
        "继续帮我调研这几个游戏首周销量",
        ["list_feishu_tasks", "list_calendar_events"],
        task_type="research",
    )
    assert nudge is not None
    assert "调研/问答任务" in nudge


def test_task_turn_allows_task_calendar_tools():
    nudge = build_tool_domain_nudge(
        "把今天会议纪要里的待办都加到我的任务里",
        ["list_feishu_tasks", "create_feishu_task"],
        task_type="research",
    )
    assert nudge is None


def test_file_deliverable_turn_blocks_code_exploration_drift():
    nudge = build_tool_domain_nudge(
        "帮我把这份评估逻辑导出成PDF发我",
        ["search_files", "list_files"],
        task_type="normal",
    )
    assert nudge is not None
    assert "文件交付" in nudge
