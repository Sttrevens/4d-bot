"""Tests for detect_action_claims — fast local action-claim detector.

Catches "光说不做" (empty promises) where the bot claims to have performed
actions in text but didn't actually call the corresponding tools.
"""

import pytest
from app.services.base_agent import detect_action_claims


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
