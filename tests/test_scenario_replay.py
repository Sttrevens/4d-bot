from pathlib import Path

import pytest

from app.harness.scenario_replay import ScenarioReplayError, replay_scenario_file
from app.harness.trajectory import TrajectoryRecorder


FIXTURE = Path(__file__).parent / "fixtures" / "scenarios" / "basic_tool_replay.json"
WEB_SEARCH_FIXTURE = Path(__file__).parent / "fixtures" / "scenarios" / "web_search_summary.json"


def test_replays_basic_tool_scenario_from_json() -> None:
    result = replay_scenario_file(FIXTURE)

    assert result.scenario_id == "basic_tool_replay"
    assert result.tenant_id == "pm-bot"
    assert result.platform == "feishu"
    assert result.user_text.startswith("帮我查一下 Hermes 工单")
    assert result.visible_tools == ["lookup_ticket", "send_delivery_receipt"]
    assert result.tool_calls[0].arguments == {"ticket_id": "H-100"}
    assert result.call_sequence == ["lookup_ticket", "send_delivery_receipt"]
    assert result.tool_outputs[1].text == "已发送交付凭证，delivery evidence ledger_id=ledger-file-42。"
    assert result.final_text == "Hermes 工单 H-100 已完成，交付凭证已发送。"
    assert result.ledger_ids == ["ledger-ticket-7", "ledger-file-42"]


def test_replay_fails_when_expected_call_sequence_drifts(tmp_path: Path) -> None:
    scenario = tmp_path / "drift.json"
    scenario.write_text(
        """{
  "id": "drift",
  "context": {"tenant_id": "pm-bot", "platform": "feishu"},
  "user_text": "check ticket",
  "expected_visible_tools": ["lookup_ticket"],
  "scripted_model_tool_calls": [{"name": "lookup_ticket", "arguments": {"ticket_id": "H-100"}}],
  "scripted_tool_results": [{"name": "lookup_ticket", "text": "ledger_id=ledger-ticket-7"}],
  "final_text": "done",
  "assertions": {
    "final_text_contains": ["done"],
    "expected_call_sequence": ["send_delivery_receipt"],
    "tool_output_ledger_ids": ["ledger-ticket-7"]
  }
}
""",
        encoding="utf-8",
    )

    with pytest.raises(ScenarioReplayError, match="call sequence"):
        replay_scenario_file(scenario)


def test_replay_fails_when_ledger_id_is_missing_from_tool_output(tmp_path: Path) -> None:
    scenario = tmp_path / "missing-ledger.json"
    scenario.write_text(
        """{
  "id": "missing-ledger",
  "context": {"tenant_id": "pm-bot", "platform": "feishu"},
  "user_text": "send receipt",
  "expected_visible_tools": ["send_delivery_receipt"],
  "scripted_model_tool_calls": [{"name": "send_delivery_receipt", "arguments": {"ledger_id": "ledger-file-42"}}],
  "scripted_tool_results": [{"name": "send_delivery_receipt", "text": "sent receipt without id"}],
  "final_text": "receipt sent",
  "assertions": {
    "final_text_contains": ["receipt sent"],
    "expected_call_sequence": ["send_delivery_receipt"],
    "tool_output_ledger_ids": ["ledger-file-42"]
  }
}
""",
        encoding="utf-8",
    )

    with pytest.raises(ScenarioReplayError, match="ledger-file-42"):
        replay_scenario_file(scenario)


def test_replays_web_search_summary_scenario() -> None:
    result = replay_scenario_file(WEB_SEARCH_FIXTURE)

    assert result.scenario_id == "web_search_summary"
    assert result.tenant_id == "code-bot"
    assert result.platform == "feishu"
    assert result.visible_tools == ["web_search", "summarize_results"]
    assert result.call_sequence == ["web_search", "summarize_results"]
    assert result.tool_calls[0].arguments == {"query": "Python 3.13 release notes"}
    assert "Python 3.13" in result.final_text
    assert "JIT compiler" in result.final_text
    assert result.ledger_ids == ["ledger-search-001", "ledger-summary-002"]


def test_replay_with_trajectory_recorder(tmp_path: Path) -> None:
    trajectory_path = tmp_path / "trajectory.jsonl"
    recorder = TrajectoryRecorder(trajectory_path, "web_search_summary")

    result = replay_scenario_file(WEB_SEARCH_FIXTURE, recorder=recorder)

    assert result.scenario_id == "web_search_summary"
    lines = trajectory_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5
    import json

    events = [json.loads(line) for line in lines]
    assert events[0]["event"] == "tool_call"
    assert events[0]["data"]["name"] == "web_search"
    assert events[1]["event"] == "tool_result"
    assert events[1]["data"]["name"] == "web_search"
    assert events[2]["event"] == "tool_call"
    assert events[2]["data"]["name"] == "summarize_results"
    assert events[3]["event"] == "tool_result"
    assert events[3]["data"]["name"] == "summarize_results"
    assert events[4]["event"] == "final"
    assert "Python 3.13" in events[4]["data"]["text"]


def test_replay_fails_on_visible_tools_mismatch(tmp_path: Path) -> None:
    scenario = tmp_path / "mismatch.json"
    scenario.write_text(
        """{
  "id": "mismatch",
  "context": {"tenant_id": "code-bot", "platform": "feishu"},
  "user_text": "search something",
  "expected_visible_tools": ["web_search"],
  "scripted_model_tool_calls": [{"name": "web_search", "arguments": {"query": "test"}}],
  "scripted_tool_results": [{"name": "web_search", "text": "result ledger_id=l1"}],
  "final_text": "done",
  "assertions": {
    "final_text_contains": ["done"],
    "expected_call_sequence": ["web_search"],
    "tool_output_ledger_ids": ["l1"]
  }
}
""",
        encoding="utf-8",
    )

    with pytest.raises(ScenarioReplayError, match="visible tools mismatch"):
        replay_scenario_file(scenario, visible_tools=["web_search", "extra_tool"])


def test_replay_fails_on_tool_call_result_name_mismatch(tmp_path: Path) -> None:
    scenario = tmp_path / "name-mismatch.json"
    scenario.write_text(
        """{
  "id": "name-mismatch",
  "context": {"tenant_id": "code-bot", "platform": "feishu"},
  "user_text": "do something",
  "expected_visible_tools": ["tool_a"],
  "scripted_model_tool_calls": [{"name": "tool_a", "arguments": {}}],
  "scripted_tool_results": [{"name": "tool_b", "text": "result ledger_id=l1"}],
  "final_text": "done",
  "assertions": {
    "final_text_contains": ["done"],
    "expected_call_sequence": ["tool_a"],
    "tool_output_ledger_ids": ["l1"]
  }
}
""",
        encoding="utf-8",
    )

    with pytest.raises(ScenarioReplayError, match="tool result #1 mismatch"):
        replay_scenario_file(scenario)
