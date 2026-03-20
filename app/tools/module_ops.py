"""能力模块管理工具

bot 可以动态查看、加载、创建和更新能力模块（app/knowledge/modules/*.md）。
模块包含领域知识（社媒调研/代码开发/数据分析等），加载后 bot 获得对应领域的工作流和最佳实践。

三种使用方式：
1. 静态：tenants.json 配置 capability_modules → system prompt 自动注入
2. 动态加载：bot 对话中调用 load_capability_module → 返回模块内容供当次对话使用
3. 自进化：bot 用 save_capability_module 创建新模块或更新现有模块，积累领域知识
"""

import logging
import os

from app.services.redis_client import execute as redis_exec
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

_MODULES_DIR = os.path.join(os.path.dirname(__file__), "..", "knowledge", "modules")
_MODULE_TTL = 365 * 86400  # 1 year


def _load_registry() -> dict[str, dict]:
    """加载 registry.json 元数据（label/category/recommended_tools）。"""
    registry_path = os.path.join(_MODULES_DIR, "registry.json")
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            import json
            data = json.loads(f.read())
            return {m["name"]: m for m in data.get("modules", [])}
    except Exception:
        return {}


def list_capability_modules(args: dict) -> ToolResult:
    """列出所有可用的能力模块。"""
    from app.services.base_agent import list_available_modules
    modules = list_available_modules()
    if not modules:
        return ToolResult.success(
            "当前没有可用的能力模块。\n"
            "你可以用 save_capability_module 创建新模块，为特定领域沉淀工作流和最佳实践。"
        )
    registry = _load_registry()
    lines = ["可用能力模块：", ""]
    for m in modules:
        name = m["name"]
        reg = registry.get(name, {})
        label = reg.get("label", "")
        category = reg.get("category", "")
        desc = m["description"]
        prefix = f"[{category}] " if category else ""
        display_name = f"{label} ({name})" if label else name
        lines.append(f"- {prefix}{display_name} — {desc}")
    lines.append("")
    lines.append("load_capability_module(name) 加载详细内容 | save_capability_module 创建/更新模块")
    return ToolResult.success("\n".join(lines))


def load_capability_module(args: dict) -> ToolResult:
    """加载指定能力模块的完整内容，获取领域知识和工作流指引。"""
    name = args.get("name", "").strip()
    if not name:
        return ToolResult.invalid_param("请提供模块名称（用 list_capability_modules 查看可用模块）")
    from app.services.base_agent import load_module_content
    content = load_module_content(name)
    if content is None:
        return ToolResult.not_found(f"模块 '{name}' 不存在。用 list_capability_modules 查看可用模块。")
    return ToolResult.success(content)


def save_capability_module(args: dict) -> ToolResult:
    """创建或更新能力模块。bot 在完成某个领域任务后，可以把工作流沉淀为模块。"""
    name = args.get("name", "").strip()
    content = args.get("content", "").strip()

    if not name:
        return ToolResult.invalid_param("请提供模块名称（英文+下划线，如 social_media_research）")
    if not content:
        return ToolResult.invalid_param("请提供模块内容（markdown 格式的领域知识和工作流）")

    # 名称校验
    if not all(c.isalnum() or c in "_-" for c in name):
        return ToolResult.invalid_param("模块名只能包含字母、数字、下划线和横杠")
    if len(name) > 50:
        return ToolResult.invalid_param("模块名不能超过 50 个字符")

    # 内容大小保护
    if len(content) > 30000:
        return ToolResult.invalid_param(
            f"模块内容 {len(content)} 字符，超过 30000 上限。"
            "请精简内容，只保留核心工作流和关键知识。"
        )
    if len(content) < 50:
        return ToolResult.invalid_param("模块内容太短（<50 字符），请提供有意义的领域知识。")

    # 写入 Redis per-tenant
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    redis_key = f"modules:{tenant.tenant_id}:{name}"

    is_update = redis_exec("EXISTS", redis_key) == 1
    result_set = redis_exec("SET", redis_key, content, "EX", str(_MODULE_TTL))
    if result_set != "OK":
        logger.error("save capability module to redis failed: key=%s", redis_key)
        return ToolResult.error("保存模块到 Redis 失败")

    action = "更新" if is_update else "创建"
    return ToolResult.success(
        f"模块 '{name}' 已{action}（{len(content)} 字符）。\n"
        f"租户配置 capability_modules 加上 \"{name}\" 即可自动注入 system prompt。\n"
        f"也可以对话中用 load_capability_module(\"{name}\") 按需加载。"
    )


# ── 工具定义（Anthropic 格式）──

TOOL_DEFINITIONS = [
    {
        "name": "list_capability_modules",
        "description": "列出所有可用的能力模块（社媒调研/代码开发/数据分析等领域知识）",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "load_capability_module",
        "description": "加载指定能力模块获取领域知识和工作流指引。当用户需求涉及特定领域（社媒调研/数据分析等）时，先加载对应模块再开始工作",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "模块名称（如 social_media_research）",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "save_capability_module",
        "description": (
            "创建或更新能力模块，将领域知识和工作流沉淀为可复用的模块。"
            "当你在某个领域（社媒调研/数据分析/内容创作等）积累了有效的工作流后，"
            "用此工具保存为模块，下次遇到同类任务直接加载。"
            "模块内容应包含：角色定位、工作流步骤、工具组合技巧、常见坑。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "模块名称（英文+下划线，如 competitor_analysis）",
                },
                "content": {
                    "type": "string",
                    "description": "模块内容（markdown 格式），包含领域知识、工作流、工具使用技巧等",
                },
            },
            "required": ["name", "content"],
        },
    },
]

TOOL_MAP = {
    "list_capability_modules": list_capability_modules,
    "load_capability_module": load_capability_module,
    "save_capability_module": save_capability_module,
}
