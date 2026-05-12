from pathlib import Path

import pytest

from app.harness.scenario_replay import ScenarioReplayError, replay_scenario_file


FIXTURE = Path(__file__).parent / "fixtures" / "scenarios" / "basic_tool_replay.json"


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
