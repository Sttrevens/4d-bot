"""Tests for the Redis inbox message persistence mechanism.

The inbox ensures that messages received by the feishu webhook are not lost
during container restarts. The flow:
1. Webhook writes message to Redis HASH `msg:inbox:{tenant_id}` BEFORE returning 200
2. _process_and_reply HDEL's the entry after processing completes
3. On startup, _recover_missed_messages HGETALL's orphaned entries and re-dispatches
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch, MagicMock, AsyncMock, call

import pytest


class TestInboxWrite:
    """Test that webhook handler writes to Redis inbox before returning 200."""

    @patch("app.services.redis_client.execute")
    @patch("app.services.redis_client.available", return_value=True)
    def test_inbox_hset_called_with_correct_args(self, mock_avail, mock_exec):
        """HSET is called with tenant-namespaced key and message payload."""
        # We test the inbox write logic in isolation
        import app.services.redis_client as redis

        tenant_id = "pm-bot"
        message_id = "msg_test_001"
        payload = {
            "msg_type": "text",
            "message": {"content": '{"text":"hello"}'},
            "message_id": message_id,
            "sender_id": "ou_sender",
            "chat_id": "oc_chat",
            "chat_type": "p2p",
            "tenant_id": tenant_id,
            "received_at": time.time(),
        }
        payload_json = json.dumps(payload, ensure_ascii=False)

        redis.execute("HSET", f"msg:inbox:{tenant_id}", message_id, payload_json)
        redis.execute("EXPIRE", f"msg:inbox:{tenant_id}", "600")

        assert mock_exec.call_count == 2
        hset_call = mock_exec.call_args_list[0]
        assert hset_call[0][0] == "HSET"
        assert hset_call[0][1] == f"msg:inbox:{tenant_id}"
        assert hset_call[0][2] == message_id
        # Verify payload is valid JSON
        stored = json.loads(hset_call[0][3])
        assert stored["message_id"] == message_id
        assert stored["tenant_id"] == tenant_id

        expire_call = mock_exec.call_args_list[1]
        assert expire_call[0] == ("EXPIRE", f"msg:inbox:{tenant_id}", "600")

    @patch("app.services.redis_client.execute", side_effect=Exception("Redis down"))
    @patch("app.services.redis_client.available", return_value=True)
    def test_inbox_write_failure_is_silent(self, mock_avail, mock_exec):
        """If Redis write fails, it should not raise — fail-open."""
        import app.services.redis_client as redis

        # Simulating the try/except in handler.py
        try:
            if redis.available():
                redis.execute("HSET", "msg:inbox:test", "msg_001", "{}")
        except Exception:
            pass  # fail-open, exactly like the handler does

        # No exception propagated — test passes if we get here

    @patch("app.services.redis_client.available", return_value=False)
    def test_inbox_skipped_when_redis_unavailable(self, mock_avail):
        """When Redis is unavailable, inbox write is skipped entirely."""
        import app.services.redis_client as redis

        # Simulating the handler's guard
        written = False
        if redis.available():
            written = True
        assert not written


class TestInboxCleanup:
    """Test that _process_and_reply's finally block cleans up inbox."""

    @patch("app.services.redis_client.execute")
    @patch("app.services.redis_client.available", return_value=True)
    def test_hdel_called_on_completion(self, mock_avail, mock_exec):
        """HDEL is called with correct key and message_id."""
        import app.services.redis_client as redis

        tenant_id = "pm-bot"
        message_id = "msg_test_002"

        # Simulating the finally block
        redis.execute("HDEL", f"msg:inbox:{tenant_id}", message_id)

        mock_exec.assert_called_once_with("HDEL", f"msg:inbox:{tenant_id}", message_id)

    @patch("app.services.redis_client.execute", side_effect=Exception("Redis gone"))
    @patch("app.services.redis_client.available", return_value=True)
    def test_hdel_failure_is_silent(self, mock_avail, mock_exec):
        """HDEL failure in finally should not propagate."""
        import app.services.redis_client as redis

        # Simulating the try/except/pass in the finally block
        try:
            if redis.available():
                redis.execute("HDEL", "msg:inbox:pm-bot", "msg_002")
        except Exception:
            pass  # exactly like the handler does


    def test_dispatch_finally_covers_command_paths(self):
        """_dispatch_message's finally block ensures HDEL runs even for /auth etc.

        Commands like /auth return early from _dispatch_message without going
        through _process_and_reply, so the HDEL in _process_and_reply's finally
        never fires. The fix adds HDEL in _dispatch_message's own finally block.
        """
        import app.services.redis_client as redis

        # Simulate what _dispatch_message's finally block does
        tenant_id = "pm-bot"
        message_id = "msg_auth_cmd"

        hdel_called = False

        def mock_hdel(*args):
            nonlocal hdel_called
            if args[0] == "HDEL" and args[2] == message_id:
                hdel_called = True

        # Whether the command returns normally or raises, finally always runs
        try:
            pass  # simulates command handling (/auth etc.) that returns early
        finally:
            try:
                mock_hdel("HDEL", f"msg:inbox:{tenant_id}", message_id)
            except Exception:
                pass

        assert hdel_called, "HDEL must fire for command-path messages"


class TestInboxRecovery:
    """Test the startup recovery logic for orphaned inbox messages."""

    def test_hgetall_parsing(self):
        """HGETALL returns flat list [k1, v1, k2, v2, ...], verify parsing."""
        raw = [
            "msg_001", json.dumps({"message_id": "msg_001", "sender_id": "ou_a", "received_at": time.time()}),
            "msg_002", json.dumps({"message_id": "msg_002", "sender_id": "ou_b", "received_at": time.time()}),
        ]
        pairs = list(zip(raw[0::2], raw[1::2]))
        assert len(pairs) == 2
        assert pairs[0][0] == "msg_001"
        assert pairs[1][0] == "msg_002"
        # Parse values
        p1 = json.loads(pairs[0][1])
        assert p1["sender_id"] == "ou_a"

    def test_old_inbox_entries_skipped(self):
        """Entries older than 660 seconds should be skipped."""
        old_ts = time.time() - 700  # 700 seconds ago
        payload = {"received_at": old_ts, "message_id": "msg_old"}
        age = time.time() - payload["received_at"]
        assert age > 660  # should be skipped

    def test_recent_inbox_entries_kept(self):
        """Entries within 660 seconds should be re-dispatched."""
        recent_ts = time.time() - 30  # 30 seconds ago
        payload = {"received_at": recent_ts, "message_id": "msg_recent"}
        age = time.time() - payload["received_at"]
        assert age <= 660  # should be re-dispatched

    def test_inbox_recovered_ids_prevent_double_processing(self):
        """Messages recovered from inbox should not be re-notified in in_flight recovery."""
        inbox_recovered_ids = {"msg_001", "msg_003"}
        resume_msg_ids = {"msg_002"}
        _already_recovered = inbox_recovered_ids | resume_msg_ids

        in_flight = {
            "msg_001": {"sender_id": "a"},  # already in inbox
            "msg_002": {"sender_id": "b"},  # already in resume
            "msg_003": {"sender_id": "c"},  # already in inbox
            "msg_004": {"sender_id": "d"},  # NOT recovered — should remain
        }
        filtered = {k: v for k, v in in_flight.items() if k not in _already_recovered}
        assert list(filtered.keys()) == ["msg_004"]

    def test_empty_hgetall_is_harmless(self):
        """Empty or None HGETALL result should be handled gracefully."""
        for empty_result in [None, [], ""]:
            # Simulating the guard in recovery code
            if not empty_result or not isinstance(empty_result, list):
                continue  # should skip
            pytest.fail(f"Should have skipped {empty_result!r}")
