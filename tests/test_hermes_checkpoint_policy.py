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
