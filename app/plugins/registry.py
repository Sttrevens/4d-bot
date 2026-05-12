"""插件注册表 —— 自动发现并按需加载工具模块

借鉴 NanoClaw 的 "skills over features" 理念：
- 工具模块自描述元数据（group、platform、权限要求）
- 按 tenant 配置按需加载，不是一股脑全 import
- 新增工具只需在 app/tools/ 放文件 + 声明 TOOL_MANIFEST

用法：
    from app.plugins.registry import plugin_registry
    # 启动时扫描（一次）
    plugin_registry.discover()
    # 按需加载
    defs, map_ = plugin_registry.get_tools_for_tenant(tenant, platform="feishu")
"""

from __future__ import annotations

import ast
import importlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── 工具模块元数据 ──

@dataclass
class ToolManifest:
    """工具模块的自描述元数据。

    每个 app/tools/*_ops.py 可以导出 TOOL_MANIFEST = {...}，
    registry 扫描时读取。没有声明的模块使用默认值。
    """
    module_name: str = ""           # "app.tools.calendar_ops"
    group: str = "core"             # 所属工具组 (core/feishu_collab/code_dev/research/...)
    platforms: list[str] = field(default_factory=lambda: ["all"])  # 限定平台 ["feishu"] / ["wecom_kf"] / ["all"]
    requires_self_iteration: bool = False    # 需要 self_iteration_enabled
    requires_instance_mgmt: bool = False     # 需要 instance_management_enabled
    inject_tenant_id: bool = False           # 工具调用时注入 tenant_id
    public_tools: list[str] = field(default_factory=list)  # 模块受限时仍公开的工具
    description: str = ""                    # 人类可读描述


# ── 插件条目（已扫描但可能未加载）──

@dataclass
class PluginEntry:
    """一个已发现但可能还没 import 的工具模块。"""
    manifest: ToolManifest
    _module: Any = None
    _tool_defs: list[dict] | None = None
    _tool_map: dict[str, Callable] | None = None
    _loaded: bool = False

    def load(self) -> tuple[list[dict], dict[str, Callable]]:
        """懒加载：首次调用时 import 模块并读取 TOOL_DEFINITIONS/TOOL_MAP。"""
        if self._loaded:
            return self._tool_defs or [], self._tool_map or {}
        try:
            mod = importlib.import_module(self.manifest.module_name)
            self._module = mod
            self._tool_defs = getattr(mod, "TOOL_DEFINITIONS", [])
            self._tool_map = getattr(mod, "TOOL_MAP", {})
            self._loaded = True
            logger.debug("plugin loaded: %s (%d tools)", self.manifest.module_name, len(self._tool_defs))
        except Exception:
            logger.warning("plugin load failed: %s", self.manifest.module_name, exc_info=True)
            self._tool_defs = []
            self._tool_map = {}
            self._loaded = True
        return self._tool_defs, self._tool_map

    @property
    def tool_names(self) -> set[str]:
        defs, _ = self.load()
        return {d["name"] for d in defs}


# ── 已知工具模块的默认 manifest 映射 ──
# 新工具如果在 app/tools/ 中声明了 TOOL_MANIFEST，会覆盖这里的默认值

_DEFAULT_MANIFESTS: dict[str, dict] = {
    "calendar_ops":     {"group": "feishu_collab", "platforms": ["feishu"]},
    "doc_ops":          {"group": "feishu_collab", "platforms": ["feishu"]},
    "minutes_ops":      {"group": "feishu_collab", "platforms": ["feishu"]},
    "task_ops":         {"group": "feishu_collab", "platforms": ["feishu"]},
    "user_ops":         {"group": "feishu_collab", "platforms": ["feishu"]},
    "message_ops":      {"group": "feishu_collab", "platforms": ["feishu"]},
    "bitable_ops":      {"group": "feishu_collab", "platforms": ["feishu"]},
    "mail_ops":         {"group": "feishu_collab", "platforms": ["feishu"]},
    "openapi_ops":      {"group": "feishu_collab", "platforms": ["feishu"]},
    "cerul_ops":        {"group": "core"},
    "file_ops":         {"group": "code_dev"},
    "git_ops":          {"group": "code_dev"},
    "github_ops":       {"group": "code_dev"},
    "repo_search":      {"group": "code_dev"},
    "issue_ops":        {"group": "code_dev"},
    "unity_ops":        {"group": "code_dev"},
    "remote_dev_ops":   {"group": "code_dev"},
    "self_ops":         {"group": "devops", "requires_self_iteration": True},
    "server_ops":       {"group": "devops", "requires_self_iteration": True},
    "railway_ops":      {"group": "devops", "requires_self_iteration": True},
    "social_media_ops": {"group": "research"},
    "xhs_ops":          {"group": "research"},
    "browser_ops":      {"group": "research"},
    "leadgen_ops":      {"group": "leadgen"},
    "outreach_ops":     {"group": "leadgen"},
    "file_export":      {"group": "content"},
    "video_url_ops":    {"group": "content"},
    "image_ops":        {"group": "content"},
    "provision_ops":    {"group": "admin", "requires_instance_mgmt": True},
    "customer_ops":     {"group": "admin", "requires_instance_mgmt": True, "public_tools": ["notify_admin"]},
    "env_ops":          {"group": "admin"},
    "capability_ops":   {"group": "admin"},
    "custom_tool_ops":  {"group": "extension", "inject_tenant_id": True},
    "skill_ops":        {"group": "extension", "inject_tenant_id": True},
    "skill_mgmt_ops":   {"group": "extension", "inject_tenant_id": True},
    "skill_engine":     {"group": "extension"},
    "web_search":       {"group": "core"},
    "memory_ops":       {"group": "core"},
    "module_ops":       {"group": "core"},
    "reminder_ops":     {"group": "automation"},
    "identity_ops":     {"group": "core"},
    "sandbox":          {"group": "core"},
    "sandbox_caps":     {"group": "core"},
    "cron_agent_ops":   {"group": "automation"},
    "tool_output_ops":  {"group": "core"},
}


def _read_tool_manifest(py_file: Path) -> dict[str, Any]:
    """Read literal TOOL_MANIFEST from source without importing the module."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    except (OSError, SyntaxError):
        logger.warning("plugin manifest parse failed: %s", py_file, exc_info=True)
        return {}

    for node in tree.body:
        value_node: ast.AST | None = None
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "TOOL_MANIFEST" for target in node.targets):
                value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "TOOL_MANIFEST":
                value_node = node.value

        if value_node is None:
            continue

        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            logger.warning("plugin manifest is not a literal dict: %s", py_file, exc_info=True)
            return {}
        return value if isinstance(value, dict) else {}

    return {}


class PluginRegistry:
    """工具插件注册表 —— 管理所有工具模块的发现、加载、过滤。"""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginEntry] = {}  # module_short_name → entry
        self._discovered = False
        self._discover_time: float = 0

    def discover(self, tools_dir: str | None = None, *, force: bool = False) -> int:
        """扫描 app/tools/ 目录，发现所有工具模块。

        只读取文件名和 TOOL_MANIFEST（如果有），不 import 整个模块。
        返回发现的模块数。
        """
        if tools_dir is None:
            if self._discovered and not force:
                return len(self._plugins)
            tools_dir = str(Path(__file__).parent.parent / "tools")

        tools_path = Path(tools_dir)
        if not tools_path.exists():
            logger.warning("tools directory not found: %s", tools_dir)
            return 0

        count = 0
        for py_file in sorted(tools_path.glob("*.py")):
            name = py_file.stem
            if name.startswith("_") or name in ("tool_result", "feishu_api", "github_api", "source_registry", "task_ops_patch"):
                continue

            module_name = f"app.tools.{name}"
            defaults = {**_DEFAULT_MANIFESTS.get(name, {}), **_read_tool_manifest(py_file)}
            manifest = ToolManifest(
                module_name=module_name,
                group=defaults.get("group", "core"),
                platforms=defaults.get("platforms", ["all"]),
                requires_self_iteration=defaults.get("requires_self_iteration", False),
                requires_instance_mgmt=defaults.get("requires_instance_mgmt", False),
                inject_tenant_id=defaults.get("inject_tenant_id", False),
                public_tools=defaults.get("public_tools", []),
                description=defaults.get("description", ""),
            )
            self._plugins[name] = PluginEntry(manifest=manifest)
            count += 1

        self._discovered = True
        self._discover_time = time.time()
        logger.info("plugin discovery: found %d tool modules in %s", count, tools_dir)
        return count

    def get_plugin(self, name: str) -> PluginEntry | None:
        return self._plugins.get(name)

    def list_plugins(self) -> dict[str, PluginEntry]:
        return dict(self._plugins)

    def get_tools_for_tenant(
        self,
        tenant,
        platform: str = "feishu",
        groups: set[str] | None = None,
    ) -> tuple[list[dict], dict[str, Callable]]:
        """按租户配置返回过滤后的工具集。

        Args:
            tenant: TenantConfig
            platform: 当前请求的平台
            groups: 指定加载的工具组（None = 全部）

        Returns:
            (tool_definitions, tool_map)
        """
        if not self._discovered:
            self.discover()

        all_defs: list[dict] = []
        all_map: dict[str, Callable] = {}
        tools_whitelist = set(tenant.tools_enabled) if tenant.tools_enabled else None

        for name, entry in self._plugins.items():
            manifest = entry.manifest

            # 平台过滤
            if "all" not in manifest.platforms and platform not in manifest.platforms:
                continue

            # 权限过滤
            if manifest.requires_self_iteration and not tenant.self_iteration_enabled:
                continue
            instance_restricted = (
                manifest.requires_instance_mgmt
                and not getattr(tenant, "instance_management_enabled", False)
            )
            if instance_restricted and not manifest.public_tools:
                continue

            # 工具组过滤
            if groups and manifest.group not in groups:
                continue

            # 加载模块
            defs, map_ = entry.load()

            if instance_restricted:
                public = set(manifest.public_tools)
                defs = [d for d in defs if d["name"] in public]
                map_ = {k: v for k, v in map_.items() if k in public}

            # tools_enabled 白名单过滤
            if tools_whitelist:
                defs = [d for d in defs if d["name"] in tools_whitelist]
                map_ = {k: v for k, v in map_.items() if k in tools_whitelist}

            all_defs.extend(defs)
            all_map.update(map_)

        return all_defs, all_map

    def get_group_tool_names(self, groups: set[str]) -> set[str]:
        """获取指定工具组包含的所有工具名。"""
        if not self._discovered:
            self.discover()

        names: set[str] = set()
        for entry in self._plugins.values():
            if entry.manifest.group in groups:
                names |= entry.tool_names
        return names

    def get_all_groups(self) -> dict[str, list[str]]:
        """返回所有工具组及其包含的模块名。"""
        result: dict[str, list[str]] = {}
        for name, entry in self._plugins.items():
            group = entry.manifest.group
            result.setdefault(group, []).append(name)
        return result

    def register_external(self, name: str, manifest: ToolManifest, module: Any) -> None:
        """注册一个外部工具模块（非 app/tools/ 目录下的）。

        用于第三方插件或运行时动态创建的工具。
        """
        entry = PluginEntry(manifest=manifest)
        entry._module = module
        entry._tool_defs = getattr(module, "TOOL_DEFINITIONS", [])
        entry._tool_map = getattr(module, "TOOL_MAP", {})
        entry._loaded = True
        self._plugins[name] = entry
        logger.info("external plugin registered: %s (%d tools)", name, len(entry._tool_defs))


# 全局单例
plugin_registry = PluginRegistry()
