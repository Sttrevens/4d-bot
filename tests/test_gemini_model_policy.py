from pathlib import Path
from types import SimpleNamespace


def test_gemini_strong_model_defaults_to_31_pro_customtools(monkeypatch):
    from app.services import gemini_provider

    monkeypatch.delenv("GEMINI_STRONG_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_ALLOW_LEGACY_25_PRO", raising=False)

    assert gemini_provider._default_gemini_strong_model() == "gemini-3.1-pro-preview-customtools"


def test_stale_25_pro_strong_model_is_replaced_by_default(monkeypatch):
    from app.services import gemini_provider

    monkeypatch.delenv("GEMINI_STRONG_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_ALLOW_LEGACY_25_PRO", raising=False)
    tenant = SimpleNamespace(llm_model="gemini-3-flash-preview", llm_model_strong="gemini-2.5-pro")

    config = gemini_provider._resolve_gemini_model_config(tenant)

    assert config.base_model == "gemini-3-flash-preview"
    assert config.strong_model == "gemini-3.1-pro-preview-customtools"
    assert config.strong_model_replaced is True


def test_legacy_25_pro_can_be_explicitly_allowed(monkeypatch):
    from app.services import gemini_provider

    monkeypatch.setenv("GEMINI_ALLOW_LEGACY_25_PRO", "1")
    tenant = SimpleNamespace(llm_model="gemini-3-flash-preview", llm_model_strong="gemini-2.5-pro")

    config = gemini_provider._resolve_gemini_model_config(tenant)

    assert config.strong_model == "gemini-2.5-pro"
    assert config.strong_model_replaced is False


def test_voice_turn_forces_strong_model_start():
    from app.services import gemini_provider

    assert gemini_provider._should_start_strong_model(
        user_text="[语音消息] 请听取并理解这段语音，然后回复用户",
        task_type="normal",
        groups={"core"},
        sub_agent_type=None,
        base_model="gemini-3-flash-preview",
        strong_model="gemini-3.1-pro-preview-customtools",
    )


def test_tenant_examples_no_longer_pin_legacy_25_pro():
    example = Path("tenants.example.json").read_text(encoding="utf-8")

    assert "gemini-2.5-pro" not in example
    assert "gemini-3.1-pro-preview-customtools" in example
