from app.plugins.registry import PluginRegistry


def test_discover_applies_tool_manifest_from_module_source(tmp_path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "temp_ops.py").write_text(
        """
TOOL_MANIFEST = {
    "group": "research",
    "platforms": ["feishu", "wecom_kf"],
    "requires_self_iteration": True,
    "requires_instance_mgmt": True,
    "inject_tenant_id": True,
    "description": "Temp manifest from source",
}
TOOL_DEFINITIONS = []
TOOL_MAP = {}
""",
        encoding="utf-8",
    )

    registry = PluginRegistry()

    assert registry.discover(str(tools_dir)) == 1

    entry = registry.get_plugin("temp_ops")
    assert entry is not None
    assert entry.manifest.group == "research"
    assert entry.manifest.platforms == ["feishu", "wecom_kf"]
    assert entry.manifest.requires_self_iteration is True
    assert entry.manifest.requires_instance_mgmt is True
    assert entry.manifest.inject_tenant_id is True
    assert entry.manifest.description == "Temp manifest from source"


def test_discover_reads_tool_manifest_without_importing_module(tmp_path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    side_effect_path = tmp_path / "imported.txt"
    (tools_dir / "side_effect_ops.py").write_text(
        f"""
from pathlib import Path
Path({str(side_effect_path)!r}).write_text("imported", encoding="utf-8")

TOOL_MANIFEST = {{"group": "content", "platforms": ["qq"]}}
TOOL_DEFINITIONS = []
TOOL_MAP = {{}}
""",
        encoding="utf-8",
    )

    registry = PluginRegistry()

    assert registry.discover(str(tools_dir)) == 1

    entry = registry.get_plugin("side_effect_ops")
    assert entry is not None
    assert entry.manifest.group == "content"
    assert entry.manifest.platforms == ["qq"]
    assert not side_effect_path.exists()
