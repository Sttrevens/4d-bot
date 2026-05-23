def test_side_effect_classifies_message_send_as_irreversible():
    from app.hermes_runtime.side_effects import classify_side_effect

    effect = classify_side_effect("send_message", {"chat_id": "oc_1"})

    assert effect.side_effect is True
    assert effect.rollback_available is False
    assert effect.side_effect_class == "message_send"


def test_checkpoint_required_for_code_mutation():
    from app.hermes_runtime.side_effects import classify_side_effect

    effect = classify_side_effect("self_edit_file", {"path": "app/tools/x.py"})

    assert effect.side_effect is True
    assert effect.requires_checkpoint is True
    assert effect.side_effect_class == "code_mutation"


def test_checkpoint_metadata_is_tenant_scoped():
    from app.hermes_runtime.checkpoint import build_checkpoint_record

    record = build_checkpoint_record(
        run_id="run-1",
        tenant_id="pm-bot",
        tool_name="self_edit_file",
        target="app/tools/x.py",
    )

    assert record["run_id"] == "run-1"
    assert record["tenant_id"] == "pm-bot"
    assert record["checkpoint_id"].startswith("cp_")


def test_tool_bridge_records_checkpoint_ledger_for_code_mutation():
    from app.hermes_runtime.tool_bridge import RuntimeToolset, execute_tool_call
    from app.hermes_runtime.types import RuntimePolicy

    ledger = []
    toolset = RuntimeToolset(handlers={"self_edit_file": lambda args: "edited"})

    result = execute_tool_call(
        toolset,
        RuntimePolicy(allowed_tool_names=["self_edit_file"]),
        "self_edit_file",
        {"path": "app/tools/x.py"},
        run_id="run-1",
        tenant_id="pm-bot",
        ledger=ledger,
    )

    assert result.ok is True
    assert len(ledger) == 1
    assert ledger[0]["status"] == "completed"
    assert ledger[0]["side_effect_class"] == "code_mutation"
    assert ledger[0]["checkpoint_id"].startswith("cp_")
    assert ledger[0]["target"] == "app/tools/x.py"
    assert ledger[0]["rollback_available"] is True
    assert ledger[0]["user_visible"] is False


def test_tool_bridge_marks_side_effect_unknown_when_mutating_tool_raises():
    from app.hermes_runtime.tool_bridge import RuntimeToolset, execute_tool_call
    from app.hermes_runtime.types import RuntimePolicy

    def crash(_args):
        raise RuntimeError("lost response after mutation")

    ledger = []
    toolset = RuntimeToolset(handlers={"self_edit_file": crash})

    result = execute_tool_call(
        toolset,
        RuntimePolicy(allowed_tool_names=["self_edit_file"]),
        "self_edit_file",
        {"path": "app/tools/x.py"},
        run_id="run-1",
        tenant_id="pm-bot",
        ledger=ledger,
    )

    assert result.ok is False
    assert result.code == "side_effect_unknown"
    assert result.outcome == "blocked"
    assert len(ledger) == 1
    assert ledger[0]["status"] == "unknown"
    assert ledger[0]["side_effect_class"] == "code_mutation"
    assert ledger[0]["checkpoint_id"].startswith("cp_")


def test_tool_bridge_does_not_record_checkpoint_for_read_only_tool():
    from app.hermes_runtime.tool_bridge import RuntimeToolset, execute_tool_call
    from app.hermes_runtime.types import RuntimePolicy

    ledger = []
    toolset = RuntimeToolset(handlers={"read_file": lambda args: "content"})

    result = execute_tool_call(
        toolset,
        RuntimePolicy(allowed_tool_names=["read_file"]),
        "read_file",
        {"path": "README.md"},
        run_id="run-1",
        tenant_id="pm-bot",
        ledger=ledger,
    )

    assert result.ok is True
    assert ledger == []
