"""Tests for reminder system"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest


# ── calc_next_trigger ──


class TestCalcNextTrigger:

    def _tz(self):
        return ZoneInfo("Asia/Shanghai")

    def _make_base(self, hour=10, minute=0, weekday_offset=0):
        """Create a base datetime for testing. weekday_offset shifts from Monday."""
        # 2026-03-09 is a Monday
        base = datetime(2026, 3, 9 + weekday_offset, hour, minute, tzinfo=self._tz())
        return base

    @patch("app.tools.reminder_ops._get_timezone")
    def test_daily_next_day(self, mock_tz):
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = self._make_base(hour=10)  # 10:00 Monday
        result = calc_next_trigger({"type": "daily", "time": "09:00"}, after=base)
        # 09:00 is before 10:00, so next trigger is tomorrow 09:00
        assert result is not None
        assert result.hour == 9
        assert result.day == 10  # Tuesday

    @patch("app.tools.reminder_ops._get_timezone")
    def test_daily_same_day(self, mock_tz):
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = self._make_base(hour=8)  # 08:00 Monday
        result = calc_next_trigger({"type": "daily", "time": "09:00"}, after=base)
        # 09:00 is after 08:00, so today
        assert result is not None
        assert result.hour == 9
        assert result.day == 9  # same day

    @patch("app.tools.reminder_ops._get_timezone")
    def test_weekly_same_week(self, mock_tz):
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = self._make_base(hour=10)  # Monday 10:00
        # Weekly on Wednesday (3)
        result = calc_next_trigger({"type": "weekly", "days": [3], "time": "09:00"}, after=base)
        assert result is not None
        assert result.isoweekday() == 3  # Wednesday
        assert result.day == 11  # March 11

    @patch("app.tools.reminder_ops._get_timezone")
    def test_weekly_next_week(self, mock_tz):
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = self._make_base(hour=10)  # Monday 10:00
        # Weekly on Monday (1) at 09:00 — already past today's Monday 09:00
        result = calc_next_trigger({"type": "weekly", "days": [1], "time": "09:00"}, after=base)
        assert result is not None
        assert result.isoweekday() == 1
        assert result.day == 16  # next Monday

    @patch("app.tools.reminder_ops._get_timezone")
    def test_weekly_multiple_days(self, mock_tz):
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = self._make_base(hour=10)  # Monday 10:00
        # Mon+Wed+Fri — closest future is Wed
        result = calc_next_trigger({"type": "weekly", "days": [1, 3, 5], "time": "09:00"}, after=base)
        assert result is not None
        assert result.isoweekday() == 3  # Wednesday is closest

    @patch("app.tools.reminder_ops._get_timezone")
    def test_monthly(self, mock_tz):
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = datetime(2026, 3, 15, 10, 0, tzinfo=self._tz())
        # Monthly on 1st — already past this month, so next month
        result = calc_next_trigger({"type": "monthly", "days": [1], "time": "09:00"}, after=base)
        assert result is not None
        assert result.month == 4
        assert result.day == 1

    @patch("app.tools.reminder_ops._get_timezone")
    def test_monthly_same_month(self, mock_tz):
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = datetime(2026, 3, 10, 8, 0, tzinfo=self._tz())
        # Monthly on 15th — still this month
        result = calc_next_trigger({"type": "monthly", "days": [15], "time": "09:00"}, after=base)
        assert result is not None
        assert result.month == 3
        assert result.day == 15

    @patch("app.tools.reminder_ops._get_timezone")
    def test_interval_hours(self, mock_tz):
        """每小时间隔应该从触发时间加 1 小时，不重置 hour/minute"""
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = self._make_base(hour=9)  # 09:00
        result = calc_next_trigger(
            {"type": "interval", "interval_hours": 1, "time": "09:00"},
            after=base,
        )
        assert result is not None
        assert result.hour == 10  # 09:00 + 1h = 10:00
        assert result.day == 9  # 同一天！不是明天

    @patch("app.tools.reminder_ops._get_timezone")
    def test_interval_minutes(self, mock_tz):
        """每 30 分钟间隔"""
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = self._make_base(hour=9, minute=0)
        result = calc_next_trigger(
            {"type": "interval", "interval_minutes": 30},
            after=base,
        )
        assert result is not None
        assert result.hour == 9
        assert result.minute == 30

    @patch("app.tools.reminder_ops._get_timezone")
    def test_interval_hours_wraps_day(self, mock_tz):
        """23:00 + 2h = 次日 01:00"""
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = datetime(2026, 3, 9, 23, 0, tzinfo=self._tz())
        result = calc_next_trigger(
            {"type": "interval", "interval_hours": 2},
            after=base,
        )
        assert result is not None
        assert result.day == 10
        assert result.hour == 1

    @patch("app.tools.reminder_ops._get_timezone")
    def test_interval_days_resets_time(self, mock_tz):
        """天级间隔仍然重置到指定时间"""
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = datetime(2026, 3, 9, 1, 0, tzinfo=self._tz())  # 凌晨 1 点触发
        result = calc_next_trigger(
            {"type": "interval", "interval_days": 1, "time": "09:00"},
            after=base,
        )
        assert result is not None
        assert result.day == 10  # 明天
        assert result.hour == 9  # 重置到 09:00

    @patch("app.tools.reminder_ops._get_timezone")
    def test_interval_combined_hours_minutes(self, mock_tz):
        """1 小时 30 分钟间隔"""
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = self._make_base(hour=9, minute=0)
        result = calc_next_trigger(
            {"type": "interval", "interval_hours": 1, "interval_minutes": 30},
            after=base,
        )
        assert result is not None
        assert result.hour == 10
        assert result.minute == 30

    @patch("app.tools.reminder_ops._get_timezone")
    def test_interval_no_fields_defaults_daily(self, mock_tz):
        """interval 不带任何字段默认 1 天"""
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        base = self._make_base(hour=1)
        result = calc_next_trigger(
            {"type": "interval", "time": "09:00"},
            after=base,
        )
        assert result is not None
        assert result.day == 10  # 明天
        assert result.hour == 9

    @patch("app.tools.reminder_ops._get_timezone")
    def test_none_type(self, mock_tz):
        from app.tools.reminder_ops import calc_next_trigger
        mock_tz.return_value = self._tz()
        result = calc_next_trigger({"type": "none"})
        assert result is None


class TestParseTimeTimezone:
    """Test that timezone parameter correctly handles DST."""

    def test_tz_name_overrides_offset(self):
        """IANA timezone should override the literal UTC offset for DST correctness."""
        from app.tools.reminder_ops import _parse_time
        # March 11 in San Francisco is PDT (UTC-7), not PST (UTC-8)
        # LLM might pass -08:00 (wrong), but tz_name fixes it
        result = _parse_time("2026-03-11T09:00:00-08:00", tz_name="America/Los_Angeles")
        assert result is not None
        # Should be interpreted as 9am LA time (PDT = UTC-7)
        assert result.utcoffset().total_seconds() == -7 * 3600  # PDT
        assert result.hour == 9

    def test_tz_name_with_naive_datetime(self):
        """Naive datetime + tz_name should use the IANA timezone."""
        from app.tools.reminder_ops import _parse_time
        result = _parse_time("2026-03-11T09:00:00", tz_name="America/Los_Angeles")
        assert result is not None
        assert result.hour == 9
        assert result.utcoffset().total_seconds() == -7 * 3600  # PDT in March

    def test_no_tz_name_keeps_literal_offset(self):
        """Without tz_name, the literal offset is preserved (legacy behavior)."""
        from app.tools.reminder_ops import _parse_time
        result = _parse_time("2026-03-11T09:00:00-08:00")
        assert result is not None
        assert result.utcoffset().total_seconds() == -8 * 3600  # literal -08:00

    def test_invalid_tz_name_falls_through(self):
        """Invalid timezone name should not crash, just keep the original."""
        from app.tools.reminder_ops import _parse_time
        result = _parse_time("2026-03-11T09:00:00-08:00", tz_name="Not/A/Timezone")
        assert result is not None
        # Falls back to literal offset
        assert result.utcoffset().total_seconds() == -8 * 3600


# ── set_reminder ──


class TestSetReminder:

    @patch("app.tools.reminder_ops.redis_client")
    @patch("app.tools.reminder_ops._get_tenant_id", return_value="test-tenant")
    @patch("app.tools.reminder_ops._get_timezone", return_value=ZoneInfo("Asia/Shanghai"))
    def test_set_onetime(self, mock_tz, mock_tid, mock_redis):
        from app.tools.reminder_ops import set_reminder
        mock_redis.execute.return_value = 1  # ZADD success

        future = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(hours=1)).isoformat()
        result = set_reminder({
            "text": "交报告",
            "remind_at": future,
            "user_id": "user_001",
            "user_name": "张三",
        })
        assert result.ok
        assert "交报告" in result.content
        assert "rem_" in result.content
        assert mock_redis.execute.call_count >= 1
        call_args = mock_redis.execute.call_args_list[0][0]
        assert call_args[0] == "ZADD"
        assert any(call[0][0] == "SADD" for call in mock_redis.execute.call_args_list[1:])

    @patch("app.tools.reminder_ops.redis_client")
    @patch("app.tools.reminder_ops._get_tenant_id", return_value="test-tenant")
    @patch("app.tools.reminder_ops._get_timezone", return_value=ZoneInfo("Asia/Shanghai"))
    def test_set_recurring(self, mock_tz, mock_tid, mock_redis):
        from app.tools.reminder_ops import set_reminder
        mock_redis.execute.return_value = 1

        result = set_reminder({
            "text": "写周报",
            "recurrence": {"type": "weekly", "days": [5], "time": "15:00"},
            "user_id": "user_001",
        })
        assert result.ok
        assert "每周" in result.content

    def test_set_missing_text(self):
        from app.tools.reminder_ops import set_reminder
        result = set_reminder({"remind_at": "2026-03-11T09:00:00+08:00", "user_id": "u"})
        assert not result.ok
        assert result.code == "invalid_param"

    @patch("app.tools.reminder_ops._get_tenant_id", return_value="test-tenant")
    @patch("app.tools.reminder_ops._get_timezone", return_value=ZoneInfo("Asia/Shanghai"))
    def test_set_past_time_rejected(self, mock_tz, mock_tid):
        from app.tools.reminder_ops import set_reminder
        past = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(hours=1)).isoformat()
        result = set_reminder({"text": "过时了", "remind_at": past, "user_id": "u"})
        assert not result.ok
        assert "未来" in result.content


# ── list_reminders ──


class TestListReminders:

    @patch("app.tools.reminder_ops.redis_client")
    @patch("app.tools.reminder_ops._get_tenant_id", return_value="test-tenant")
    def test_list_empty(self, mock_tid, mock_redis):
        from app.tools.reminder_ops import list_reminders
        mock_redis.execute.return_value = []
        result = list_reminders({"user_id": "user_001"})
        assert result.ok
        assert "没有" in result.content

    @patch("app.tools.reminder_ops.redis_client")
    @patch("app.tools.reminder_ops._get_tenant_id", return_value="test-tenant")
    def test_list_filters_by_user(self, mock_tid, mock_redis):
        from app.tools.reminder_ops import list_reminders
        r1 = json.dumps({"id": "rem_aaa", "user_id": "user_001", "text": "A", "next_trigger": "2026-03-11T09:00:00+08:00", "recurrence": {"type": "none"}})
        r2 = json.dumps({"id": "rem_bbb", "user_id": "user_002", "text": "B", "next_trigger": "2026-03-12T09:00:00+08:00", "recurrence": {"type": "none"}})
        mock_redis.execute.return_value = [r1, r2]

        result = list_reminders({"user_id": "user_001"})
        assert result.ok
        assert "rem_aaa" in result.content
        assert "rem_bbb" not in result.content


# ── cancel_reminder ──


class TestCancelReminder:

    @patch("app.tools.reminder_ops.redis_client")
    @patch("app.tools.reminder_ops._get_tenant_id", return_value="test-tenant")
    def test_cancel_success(self, mock_tid, mock_redis):
        from app.tools.reminder_ops import cancel_reminder
        reminder = json.dumps({"id": "rem_aaa", "text": "测试", "user_id": "u1", "next_trigger": "2026-03-11T09:00:00+08:00"})
        # First call: ZRANGEBYSCORE to find; second call: ZREM; then cleanup checks
        mock_redis.execute.side_effect = [
            [reminder],  # find
            1,           # ZREM success
            0,           # ZCARD after removal
            1,           # SREM cleanup
        ]
        result = cancel_reminder({"reminder_id": "rem_aaa", "user_id": "u1"})
        assert result.ok
        assert "测试" in result.content

    @patch("app.tools.reminder_ops.redis_client")
    @patch("app.tools.reminder_ops._get_tenant_id", return_value="test-tenant")
    def test_cancel_not_found(self, mock_tid, mock_redis):
        from app.tools.reminder_ops import cancel_reminder
        mock_redis.execute.return_value = []
        result = cancel_reminder({"reminder_id": "rem_nonexist", "user_id": "u1"})
        assert not result.ok
        assert result.code == "not_found"


# ── get_due_reminders ──


class TestGetDueReminders:

    @patch("app.tools.reminder_ops.redis_client")
    def test_returns_due_items(self, mock_redis):
        from app.tools.reminder_ops import get_due_reminders
        r = json.dumps({"id": "rem_aaa", "text": "到期了"})
        mock_redis.execute.return_value = [r]
        result = get_due_reminders("test-tenant")
        assert len(result) == 1
        assert result[0][0]["id"] == "rem_aaa"

    @patch("app.tools.reminder_ops.redis_client")
    def test_returns_empty_when_none(self, mock_redis):
        from app.tools.reminder_ops import get_due_reminders
        mock_redis.execute.return_value = []
        result = get_due_reminders("test-tenant")
        assert result == []

    @patch("app.tools.reminder_ops.redis_client")
    def test_redis_unavailable(self, mock_redis):
        from app.tools.reminder_ops import get_due_reminders
        mock_redis.execute.return_value = None
        result = get_due_reminders("test-tenant")
        assert result == []
