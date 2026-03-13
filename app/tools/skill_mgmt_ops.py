"""Skill 管理工具 — Bot 可用的 5 个 skill 管理元工具

learn_skill:   从对话中学习新 skill（LLM 自主提炼）
install_skill_md: 从 GitHub 安装 SKILL.md 格式的 skill
list_skills:   列出当前租户所有 skill
remove_skill:  删除 skill
share_skill:   导出 skill 为 SKILL.md 格式（可分享）

与 custom_tool_ops / skill_ops 的区别:
- custom_tool_ops: 底层自定义工具 CRUD（代码级别）
- skill_ops: 从 GitHub 安装 .py/.md 文件
- skill_mgmt_ops: 统一的 SKILL.md 格式 skill 管理（知识+工具+触发器三合一）
"""

from __future__ import annotations

import json
import logging

from app.tools.skill_engine import (
    delete_skill,
    export_skill_md,
    list_skills as engine_list_skills,
    parse_skill_md,
    save_skill,
)
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)


# ── Tool handlers ──

def handle_learn_skill(args: dict) -> ToolResult:
    """Bot 从对话中学习新 skill。

    LLM 调用时提供: name, description, triggers, instructions, tool_code(可选)
    """
    tenant_id = args.get("tenant_id", "")
    name = args.get("name", "").strip()
    description = args.get("description", "").strip()
    triggers = args.get("triggers", [])
    instructions = args.get("instructions", "").strip()
    tool_code = args.get("tool_code", "").strip()

    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")
    if not name:
        return ToolResult.invalid_param("缺少 skill 名称 (name)")
    if not instructions:
        return ToolResult.invalid_param("缺少 skill 指令 (instructions)")

    # 如果 triggers 是字符串（逗号分隔），转为列表
    if isinstance(triggers, str):
        triggers = [t.strip() for t in triggers.split(",") if t.strip()]

    # 如果有 tool_code，尝试提取 tool_defs
    tool_defs = None
    if tool_code:
        try:
            from app.tools.sandbox import compile_tool
            module_dict, errors = compile_tool(tool_code)
            if errors:
                return ToolResult.error(
                    "工具代码编译失败:\n" + "\n".join(f"- {e}" for e in errors)
                )
            tool_defs = module_dict.get("TOOL_DEFINITIONS", [])
        except Exception as e:
            return ToolResult.error(f"工具代码处理失败: {e}")

    return save_skill(
        tenant_id=tenant_id,
        name=name,
        description=description,
        triggers=triggers,
        instructions=instructions,
        tool_defs=tool_defs,
        tool_code=tool_code,
        source="learned",
    )


def handle_install_skill_md(args: dict) -> ToolResult:
    """从 GitHub URL 安装 SKILL.md 格式的 skill。"""
    import httpx

    tenant_id = args.get("tenant_id", "")
    url = args.get("url", "").strip()

    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")
    if not url:
        return ToolResult.invalid_param("请提供 SKILL.md 的 GitHub URL")

    # 下载文件
    from app.tools.skill_ops import _to_raw_url, _extract_repo_info
    raw_url = _to_raw_url(url)
    if not raw_url:
        # 尝试直接用 URL
        raw_url = url

    try:
        resp = httpx.get(raw_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        return ToolResult.error(f"下载失败: {e}")

    if not content.strip():
        return ToolResult.error("下载的文件为空")

    # 解析 SKILL.md
    parsed = parse_skill_md(content)
    if isinstance(parsed, str):
        return ToolResult.error(f"SKILL.md 解析失败: {parsed}")

    name = parsed.get("name", "")
    if not name:
        # 从 URL 推导名称
        from app.tools.skill_ops import _derive_module_name
        import re
        m = re.search(r"/([^/]+)$", url.rstrip("/"))
        if m:
            name = _derive_module_name(m.group(1))
        if not name:
            name = "imported_skill"

    # 来源追踪
    repo_info = _extract_repo_info(url)
    source = f"github:{repo_info[0]}/{repo_info[1]}" if repo_info else f"github:{url}"

    # 检查是否有内联 tool_code
    # SKILL.md 可以在 ```python ... ``` 代码块中定义工具代码
    tool_code = ""
    tool_defs = parsed.get("tools", [])
    import re
    code_blocks = re.findall(r"```python\n(.*?)```", parsed.get("instructions", ""), re.DOTALL)
    if code_blocks:
        # 找包含 TOOL_DEFINITIONS 的代码块
        for block in code_blocks:
            if "TOOL_DEFINITIONS" in block or "TOOL_MAP" in block:
                tool_code = block.strip()
                break

    return save_skill(
        tenant_id=tenant_id,
        name=name,
        description=parsed.get("description", ""),
        triggers=parsed.get("triggers", []),
        instructions=parsed.get("instructions", ""),
        tool_defs=tool_defs if tool_defs else None,
        tool_code=tool_code,
        source=source,
    )


def handle_list_skills(args: dict) -> ToolResult:
    """列出当前租户所有 skill。"""
    tenant_id = args.get("tenant_id", "")
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")

    skills = engine_list_skills(tenant_id)
    if not skills:
        return ToolResult.success(
            "当前没有安装任何 skill。\n\n"
            "你可以:\n"
            "- 通过 learn_skill 从对话中学习新技能\n"
            "- 通过 install_skill_md 从 GitHub 安装 SKILL.md"
        )

    lines = [f"共 {len(skills)} 个 skill:\n"]
    for s in skills:
        triggers = ", ".join(s["triggers"][:5]) if s["triggers"] else "无触发词"
        tools_info = f", {s['tool_count']} 个工具" if s["tool_count"] else ""
        lines.append(
            f"- **{s['name']}** (v{s['version']}) — {s['description']}\n"
            f"  触发: [{triggers}]{tools_info} | 来源: {s['source']}"
        )

    return ToolResult.success("\n".join(lines))


def handle_remove_skill(args: dict) -> ToolResult:
    """删除 skill。"""
    tenant_id = args.get("tenant_id", "")
    name = args.get("name", "").strip()
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")
    if not name:
        return ToolResult.invalid_param("请指定要删除的 skill 名称")
    return delete_skill(tenant_id, name)


def handle_share_skill(args: dict) -> ToolResult:
    """导出 skill 为 SKILL.md 格式。"""
    tenant_id = args.get("tenant_id", "")
    name = args.get("name", "").strip()
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")
    if not name:
        return ToolResult.invalid_param("请指定要导出的 skill 名称")

    md = export_skill_md(tenant_id, name)
    if md is None:
        return ToolResult.not_found(f"Skill '{name}' 不存在")

    return ToolResult.success(
        f"Skill '{name}' 的 SKILL.md 内容：\n\n```markdown\n{md}\n```\n\n"
        "你可以将这个文件分享给其他用户，他们可以通过 install_skill_md 安装。"
    )


# ── Tool definitions ──

TOOL_DEFINITIONS = [
    {
        "name": "learn_skill",
        "description": (
            "从对话中学习新技能。当你发现一种可复用的工作模式时（如特定平台的调研流程、"
            "特定格式的报告生成、特定 API 的调用方式），可以将其提炼为 skill 保存。\n\n"
            "skill = 知识指令 + 触发关键词 + 可选工具代码。\n"
            "下次用户消息匹配触发词时，skill 的指令会自动注入到你的上下文中。\n\n"
            "示例：用户教你如何分析竞品 → learn_skill(name='competitor_analysis', "
            "triggers=['竞品','竞争对手'], instructions='分析步骤...')"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "租户 ID（系统自动填充）"},
                "name": {
                    "type": "string",
                    "description": "skill 名称（英文+下划线，如 competitor_analysis）",
                },
                "description": {
                    "type": "string",
                    "description": "一句话描述这个 skill 做什么",
                },
                "triggers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "触发关键词列表。用户消息包含这些词时自动激活此 skill",
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "skill 的核心指令（Markdown 格式）。被触发时注入到你的 system prompt 中，"
                        "指导你如何完成此类任务。应包含：工作流步骤、注意事项、输出格式等"
                    ),
                },
                "tool_code": {
                    "type": "string",
                    "description": (
                        "可选。Python 工具代码。必须导出 TOOL_DEFINITIONS 和 TOOL_MAP。"
                        "格式与 create_custom_tool 相同。skill 被触发时工具自动可用"
                    ),
                },
            },
            "required": ["tenant_id", "name", "triggers", "instructions"],
        },
    },
    {
        "name": "install_skill_md",
        "description": (
            "从 GitHub 安装 SKILL.md 格式的技能。"
            "SKILL.md 是 OpenClaw/AgentSkills 规范的技能文件，"
            "包含 YAML frontmatter（name、triggers、tools）和 Markdown 指令正文。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "租户 ID（系统自动填充）"},
                "url": {
                    "type": "string",
                    "description": "SKILL.md 文件的 GitHub URL（blob 或 raw 格式均可）",
                },
            },
            "required": ["tenant_id", "url"],
        },
    },
    {
        "name": "list_learned_skills",
        "description": "列出当前已安装的所有技能（skill），包括触发关键词、工具数、来源。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "租户 ID（系统自动填充）"},
            },
            "required": ["tenant_id"],
        },
    },
    {
        "name": "remove_skill",
        "description": "删除一个已安装的技能。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "租户 ID（系统自动填充）"},
                "name": {"type": "string", "description": "要删除的 skill 名称"},
            },
            "required": ["tenant_id", "name"],
        },
    },
    {
        "name": "share_skill",
        "description": "将一个 skill 导出为 SKILL.md 格式的 Markdown 文本，方便分享给其他用户或仓库。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "租户 ID（系统自动填充）"},
                "name": {"type": "string", "description": "要导出的 skill 名称"},
            },
            "required": ["tenant_id", "name"],
        },
    },
]

TOOL_MAP = {
    "learn_skill": handle_learn_skill,
    "install_skill_md": handle_install_skill_md,
    "list_learned_skills": handle_list_skills,
    "remove_skill": handle_remove_skill,
    "share_skill": handle_share_skill,
}
