"""Tests for detect_action_claims — fast local action-claim detector.

Catches "光说不做" (empty promises) where the bot claims to have performed
actions in text but didn't actually call the corresponding tools.
"""

import pytest
from app.services.base_agent import (
    _sanitize_progress_hint,
    detect_action_claims,
    check_unfulfilled_actions,
)


class TestDeleteClaims:
    """Bot claims it deleted something but didn't call delete tools."""

    def test_claimed_delete_no_tool(self):
        assert detect_action_claims("我已经把之前的日程都删掉了", [])

    def test_claimed_delete_with_irrelevant_tool(self):
        assert detect_action_claims("已经删除了所有日历事件", ["fetch_url", "web_search"])

    def test_claimed_delete_with_actual_delete_tool(self):
        assert not detect_action_claims("已经删除了所有日历事件", ["delete_calendar_event"])

    def test_all_deleted(self):
        assert detect_action_claims("全部删掉了，干干净净", ["create_calendar_event"])

    def test_cleared(self):
        assert detect_action_claims("已经清理了旧的日程", ["fetch_url"])


class TestCreateClaims:
    """Bot claims it created something but didn't call create tools."""

    def test_claimed_create_no_tool(self):
        assert detect_action_claims("已经创建了5个日历事件", [])

    def test_claimed_add_no_tool(self):
        assert detect_action_claims("已经添加了新的日程", ["fetch_url"])

    def test_claimed_create_with_actual_tool(self):
        assert not detect_action_claims("已经创建了日历事件", ["create_calendar_event"])

    def test_all_added(self):
        assert detect_action_claims("全部加好了", ["web_search"])


class TestSendClaims:
    """Bot claims it sent something but didn't call send tools."""

    def test_claimed_send_no_tool(self):
        assert detect_action_claims("已经发送了邮件", [])

    def test_claimed_send_with_actual_tool(self):
        assert not detect_action_claims("已经发送了邮件", ["send_mail"])

    def test_claimed_send_with_failed_outcome(self):
        assert detect_action_claims(
            "已经发到群里了",
            ["send_message_to_group"],
            action_outcomes=[("send_message_to_group", "→ 失败: [ERROR] 权限不足")],
        )

    def test_claimed_send_with_success_outcome(self):
        assert not detect_action_claims(
            "已经发到群里了",
            ["send_message_to_group"],
            action_outcomes=[("send_message_to_group", "→ 成功 message_id=om_xxx")],
        )

    def test_file_sent_claim_requires_confirmed_export_success(self):
        assert detect_action_claims(
            "文件已经发你了，记得看。",
            ["export_file", "web_search", "think"],
            action_outcomes=[("export_file", "→ 完成")],
        )

    def test_file_sent_claim_allows_confirmed_export_success(self):
        assert not detect_action_claims(
            "文件已经发你了，记得看。",
            ["export_file"],
            action_outcomes=[("export_file", "文件 食堂减脂指南.md 已发送给用户（1.2KB）。用户可在聊天中直接下载。")],
        )

    def test_pdf_generated_claim_allows_export_success(self):
        assert not detect_action_claims(
            "PDF 已经生成，可以直接下载。",
            ["export_file"],
            action_outcomes=[("export_file", "文件 DaiLing_AI_Growth_v1.0.pdf 已发送给用户（75KB）。用户可在聊天中直接下载。")],
        )

    def test_reminder_is_not_direct_notification_success(self):
        assert detect_action_claims(
            "我已经通知Steven了，他会处理审批。",
            ["set_reminder"],
            action_outcomes=[("set_reminder", "已设置提醒\n内容：请审批\nID：rem_123")],
        )

    def test_notify_admin_success_satisfies_direct_notification(self):
        assert not detect_action_claims(
            "我已经通知Steven了，他会处理审批。",
            ["notify_admin"],
            action_outcomes=[(
                "notify_admin",
                "→ 成功 delivered_channels=feishu/code-bot queued_alert_id=admin_alert_123",
            )],
        )

    def test_daily_reminder_claim_requires_reminder_success(self):
        assert detect_action_claims(
            "我已经帮你设置了每日提醒，从明天早上8点开始，每天给你发当天的餐单。",
            ["save_memory"],
            action_outcomes=[("save_memory", "→ 成功")],
        )

    def test_daily_reminder_claim_allows_set_reminder_success(self):
        assert not detect_action_claims(
            "我已经帮你设置了每日提醒，从明天早上8点开始，每天给你发当天的餐单。",
            ["set_reminder"],
            action_outcomes=[("set_reminder", "已设置重复提醒（每天）\n内容：每日减脂餐单\n下次提醒：2026-06-10 08:00\nID：rem_123")],
        )


class TestProvisionClaims:
    def test_generated_link_claim_requires_real_link_tool(self):
        assert detect_action_claims(
            "我已经给你生成专属客服链接了。",
            ["set_reminder"],
            action_outcomes=[("set_reminder", "已设置提醒\nID：rem_123")],
        )

    def test_generated_link_claim_satisfied_by_kf_link_tool(self):
        assert not detect_action_claims(
            "我已经给你生成专属客服链接了。",
            ["get_kf_account_link"],
            action_outcomes=[("get_kf_account_link", "→ 成功 {\"url\":\"https://work.weixin.qq.com/kfid/xxx\"}")],
        )

    def test_approval_claim_requires_approval_tool(self):
        assert detect_action_claims(
            "我已经审批通过这个开通申请了。",
            ["notify_admin"],
            action_outcomes=[("notify_admin", "→ 成功 delivered_channels=dashboard_alert")],
        )

    def test_approval_claim_satisfied_by_approval_tool(self):
        assert not detect_action_claims(
            "我已经审批通过这个开通申请了。",
            ["approve_provision_request"],
            action_outcomes=[("approve_provision_request", "→ 成功 已批准并自动开通实例 kf-demo。")],
        )


class TestModifyClaims:
    """Bot claims it modified something but didn't call update tools."""

    def test_claimed_update_no_tool(self):
        assert detect_action_claims("已经修改了日历事件的时区", [])

    def test_claimed_update_with_actual_tool(self):
        assert not detect_action_claims("已经更新了日历事件", ["update_calendar_event"])

    def test_claimed_edit_with_doc_tool(self):
        assert not detect_action_claims("已经编辑了文档", ["edit_feishu_doc"])


class TestPromisePatterns:
    """Bot promises to do something (future tense) — always an empty promise."""

    def test_wo_qu(self):
        assert detect_action_claims("我去删一下日程", [])

    def test_mashang(self):
        assert detect_action_claims("马上处理这个问题", [])

    def test_zhejiu(self):
        assert detect_action_claims("这就开始创建日历事件", [])

    def test_xianqu(self):
        assert detect_action_claims("先去修改一下时区设置", [])

    def test_wo_xianzai(self):
        assert detect_action_claims("我现在开始添加日程", [])

    def test_promise_even_with_tools(self):
        # "我去做" patterns are ALWAYS empty promises (bot should call tools, not announce)
        assert detect_action_claims("我去删一下", ["fetch_url", "web_search"])

    def test_transfer_message_to_steven_is_action_promise(self):
        assert detect_action_claims("明白了，我这就转告Steven让他去后台处理", [])


class TestFalsePositives:
    """Cases that should NOT trigger the detector."""

    def test_short_reply(self):
        assert not detect_action_claims("好的", [])

    def test_empty_reply(self):
        assert not detect_action_claims("", [])

    def test_normal_conversation(self):
        assert not detect_action_claims("你想让我怎么处理这些日程呢？", [])

    def test_reporting_results(self):
        assert not detect_action_claims(
            "我查到了以下信息：GDC 2025 在旧金山举行",
            ["web_search"],
        )

    def test_historical_reference(self):
        """'之前已经删了' is describing past actions, not current claims."""
        assert not detect_action_claims("之前已经删了旧的日程", [])

    def test_asking_question(self):
        assert not detect_action_claims("需要我帮你删除这些日程吗？", [])

    def test_pure_info(self):
        assert not detect_action_claims(
            "根据 Google Sheet 的数据，3月12日到15日有以下活动...",
            ["fetch_url"],
        )

    def test_explanation_frame_on_analysis_question(self):
        assert not detect_action_claims(
            "虽然您已经洞察了一切，但我还是给您罗列几个臭味投合的凡人理论吧：",
            ["web_search"],
            user_text="有没有哪些哲学家，或者比如教义，也是类似的观点？",
        )

    def test_explanation_frame_on_source_question(self):
        assert not detect_action_claims(
            "我来给你解释一下他的宿命论逻辑，再顺手梳理一下马尔可夫过程。",
            ["analyze_video_url"],
            user_text="他的宿命论具体是什么逻辑",
        )

    def test_explanation_frame_on_followup_continue(self):
        assert not detect_action_claims(
            "我来给你梳理一下我们刚才定下来的评估维度和权重",
            [],
            user_text="继续",
        )


class TestEdgeCases:
    def test_none_reply(self):
        assert not detect_action_claims(None, [])

    def test_mixed_tools_partial_match(self):
        """Claimed delete + create, but only did create → should catch the delete claim."""
        assert detect_action_claims(
            "已经删掉旧的，然后创建了新日程",
            ["create_calendar_event"],
        )

    def test_actual_completion_with_all_tools(self):
        """Actually did everything → no false alarm."""
        assert not detect_action_claims(
            "已经删除了旧日程，创建了新的",
            ["delete_calendar_event", "create_calendar_event"],
        )

    def test_action_request_still_catches_empty_promise(self):
        assert detect_action_claims(
            "好的，我这就去给你创建提醒。",
            [],
            user_text="今晚提醒我报销",
        )


class TestProgressHintSanitizer:
    def test_rejects_admin_sync_claim_without_notify_success(self):
        assert _sanitize_progress_hint(
            "文件我看到了，正在同步给 Steven 确认你的想法呢",
            ["export_file"],
            user_text="把方案转成 PDF",
        ) is None

    def test_allows_neutral_progress_hint(self):
        assert _sanitize_progress_hint(
            "我先把这份方案整理成更清楚的版本",
            ["export_file"],
            user_text="把方案转成 PDF",
        ) == "我先把这份方案整理成更清楚的版本"

    @pytest.mark.parametrize(
        "hint",
        [
            "小红书上那几家冷门山居民宿我快筛完了，马上给你发几个合适的选项",
            "这就去小红书帮你翻翻那几个避世山头，有结果了发你",
            "我还在处理这条消息，已经在查资料了，马上给你结论",
        ],
    )
    def test_rejects_overconfident_result_ready_progress(self, hint):
        assert _sanitize_progress_hint(
            hint,
            ["xhs_search", "web_search"],
            user_text="帮我找上海周边小众山居民宿",
        ) is None


class TestUnfulfilledActions:
    def test_group_send_request_requires_send_tool(self):
        missing = check_unfulfilled_actions(
            "基于这次会议，在CAMDOWN大群里告知大家后续待办",
            ["get_feishu_minute_transcript", "list_bot_groups"],
        )
        assert "群消息通知" in missing

    def test_group_send_request_satisfied_when_called(self):
        missing = check_unfulfilled_actions(
            "帮我发到项目大群同步一下",
            ["send_message_to_group"],
        )
        assert missing == []

    def test_group_send_request_called_but_failed_still_missing(self):
        missing = check_unfulfilled_actions(
            "帮我发到项目大群同步一下",
            ["send_message_to_group"],
            action_outcomes=[("send_message_to_group", "→ 失败: [ERROR] 权限不足")],
        )
        assert "群消息通知" in missing

    def test_group_send_request_satisfied_with_success_outcome(self):
        missing = check_unfulfilled_actions(
            "帮我发到项目大群同步一下",
            ["send_message_to_group"],
            action_outcomes=[("send_message_to_group", "→ 成功 message_id=om_xxx")],
        )
        assert missing == []

    def test_notify_steven_request_can_use_notify_admin(self):
        missing = check_unfulfilled_actions(
            "帮我通知Steven审批一下这个开通申请",
            ["notify_admin"],
            action_outcomes=[("notify_admin", "→ 成功 delivered_channels=feishu/code-bot")],
        )
        assert missing == []

    def test_notify_steven_request_not_satisfied_by_reminder(self):
        missing = check_unfulfilled_actions(
            "帮我通知Steven审批一下这个开通申请",
            ["set_reminder"],
            action_outcomes=[("set_reminder", "已设置提醒\n内容：请审批\nID：rem_123")],
        )
        assert "私信通知" in missing

    def test_daily_meal_request_requires_reminder_or_cron(self):
        missing = check_unfulfilled_actions(
            "以后每天给我推荐早中午和晚上的餐食，要尽量不重样且不复杂",
            ["save_memory", "web_search"],
            action_outcomes=[("save_memory", "→ 成功")],
        )
        assert "定时提醒" in missing

    def test_task_assignment_claim_rejected_when_assignee_add_partially_failed(self):
        assert detect_action_claims(
            "已经帮你把这 5 个任务创建并分配好，全都放到 CAM DOWN 任务表里了。",
            ["create_feishu_task", "create_feishu_task", "create_feishu_task"],
            action_outcomes=[
                (
                    "create_feishu_task",
                    "任务已创建: 干掉/改造lobby\nID: task_1\n已分配给 0/1 人\n"
                    "负责人添加失败: 吴天骄 (ou_b029): [ERROR] code=1470500",
                )
            ],
        )
