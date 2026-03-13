"""Skills 引擎 — 统一 SKILL.md 格式的解析、存储、加载

将 OpenClaw 风格的 SKILL.md（知识 + 工具 + 触发器 三合一）适配到多租户架构。

SKILL.md 格式:
```markdown
---
name: skill-name
description: 一句话描述
triggers:
  - 关键词1
  - 关键词2
tools:                    # 可选，纯知识型 skill 不需要
  - name: tool_name
    description: 工具描述
    parameters:
      param1: { type: string, required: true, description: "参数描述" }
---

## 使用指南
（Markdown 正文 = 注入到 system prompt 的指令）
```

存储结构（Redis per-tenant）:
  Hash:  skill:{tenant_id}:{skill_name}
    Fields: name, description, triggers (JSON), instructions (str),
            tool_defs (JSON), tool_code (str), source, created_at, updated_at, version
  Set:   skill:{tenant_id}:_index  (所有 skill 名)
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from app.services.redis_client import execute as redis_exec, pipeline as redis_pipeline
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── Redis key helpers ──

def _skill_key(tenant_id: str, name: str) -> str:
    return f"skill:{tenant_id}:{name}"


def _index_key(tenant_id: str) -> str:
    return f"skill:{tenant_id}:_index"


# ── SKILL.md 解析 ──

def parse_skill_md(content: str) -> dict[str, Any] | str:
    """解析 SKILL.md 格式文件。

    Returns:
        成功: dict with keys: name, description, triggers, instructions, tools
        失败: str 错误信息
    """
    content = content.strip()

    # 提取 YAML frontmatter
    if not content.startswith("---"):
        # 没有 frontmatter，整个内容作为 instructions
        return {
            "name": "",
            "description": "",
            "triggers": [],
            "instructions": content,
            "tools": [],
        }

    # 找到第二个 ---
    end = content.find("---", 3)
    if end == -1:
        return "SKILL.md 格式错误：缺少 frontmatter 结束标记 ---"

    frontmatter_str = content[3:end].strip()
    instructions = content[end + 3:].strip()

    # 简单的 YAML 解析（不依赖 PyYAML，手动解析关键字段）
    frontmatter = _parse_simple_yaml(frontmatter_str)
    if isinstance(frontmatter, str):
        return frontmatter  # error message

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")
    triggers = frontmatter.get("triggers", [])
    tools = frontmatter.get("tools", [])

    # 规范化 triggers
    if isinstance(triggers, str):
        triggers = [t.strip() for t in triggers.split(",") if t.strip()]

    # 规范化 tools
    normalized_tools = []
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                normalized_tools.append(_normalize_tool_def(tool))

    return {
        "name": name,
        "description": description,
        "triggers": triggers,
        "instructions": instructions,
        "tools": normalized_tools,
    }


def _normalize_tool_def(tool: dict) -> dict:
    """将 SKILL.md 简化格式转换为 Anthropic 标准 tool definition。"""
    name = tool.get("name", "")
    description = tool.get("description", "")
    params = tool.get("parameters", {})

    # 简易 YAML 解析器可能把 parameters 的子 key 提升到 tool 层级
    # 检测: 如果 params 不是 dict，从 tool 中提取非标准 key 作为参数
    if not isinstance(params, dict) or not params:
        known_keys = {"name", "description", "parameters"}
        extracted: dict[str, Any] = {}
        for k, v in tool.items():
            if k not in known_keys and isinstance(v, str) and v.startswith("{"):
                # 像 url: { type: string, required: true, description: "竞品URL" }
                try:
                    json_str = v.replace("'", '"')
                    json_str = re.sub(r'(\w+)\s*:', r'"\1":', json_str)
                    extracted[k] = json.loads(json_str)
                except (json.JSONDecodeError, Exception):
                    extracted[k] = {"type": "string", "description": v}
        if extracted:
            params = extracted

    # 转换简化参数格式为标准 JSON Schema
    properties = {}
    required = []
    for param_name, param_spec in params.items():
        if isinstance(param_spec, dict):
            prop: dict[str, Any] = {"type": param_spec.get("type", "string")}
            if "description" in param_spec:
                prop["description"] = param_spec["description"]
            if "enum" in param_spec:
                prop["enum"] = param_spec["enum"]
            if "default" in param_spec:
                prop["default"] = param_spec["default"]
            properties[param_name] = prop
            if param_spec.get("required"):
                required.append(param_name)
        elif isinstance(param_spec, str):
            # 简写: param_name: "description text"
            properties[param_name] = {"type": "string", "description": param_spec}

    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            **({"required": required} if required else {}),
        },
    }


def _parse_simple_yaml(text: str) -> dict[str, Any] | str:
    """极简 YAML 解析器 — 只支持 SKILL.md frontmatter 需要的子集。

    支持:
    - key: value（标量）
    - key:\n  - item1\n  - item2（列表）
    - key:\n  - name: xxx\n    description: yyy\n    parameters:\n      p1: {...}（嵌套对象列表）

    不支持完整 YAML spec，够用就行。
    """
    result: dict[str, Any] = {}
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # key: value
        colon_idx = stripped.find(":")
        if colon_idx == -1:
            i += 1
            continue

        key = stripped[:colon_idx].strip()
        value_part = stripped[colon_idx + 1:].strip()

        if value_part:
            # 内联值: key: some value
            # 去掉引号
            if (value_part.startswith('"') and value_part.endswith('"')) or \
               (value_part.startswith("'") and value_part.endswith("'")):
                value_part = value_part[1:-1]
            result[key] = value_part
            i += 1
        else:
            # 可能是列表或嵌套对象
            items, new_i = _parse_yaml_block(lines, i + 1)
            result[key] = items
            i = new_i

    return result


def _parse_yaml_block(lines: list[str], start: int) -> tuple[list[Any], int]:
    """解析 YAML 缩进块（列表或对象列表）。"""
    items: list[Any] = []
    i = start
    base_indent = -1

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        # 计算缩进
        indent = len(line) - len(line.lstrip())
        if base_indent == -1:
            base_indent = indent
        elif indent < base_indent:
            break  # 回到上层

        stripped = line.strip()

        if stripped.startswith("- "):
            # 列表项
            item_content = stripped[2:].strip()
            # 检查是否是 - key: value（对象列表项的开始）
            if ":" in item_content:
                obj, new_i = _parse_yaml_list_item(lines, i, indent)
                items.append(obj)
                i = new_i
            else:
                # 简单列表项
                if (item_content.startswith('"') and item_content.endswith('"')) or \
                   (item_content.startswith("'") and item_content.endswith("'")):
                    item_content = item_content[1:-1]
                items.append(item_content)
                i += 1
        else:
            break  # 不是列表项

    return items, i


def _parse_yaml_list_item(lines: list[str], start: int, list_indent: int) -> tuple[dict, int]:
    """解析 YAML 列表项中的对象（- key: value\n  key2: value2）。"""
    obj: dict[str, Any] = {}
    i = start
    stripped = lines[i].strip()
    # 第一行: - key: value
    first_content = stripped[2:].strip()
    colon_idx = first_content.find(":")
    if colon_idx != -1:
        k = first_content[:colon_idx].strip()
        v = first_content[colon_idx + 1:].strip()
        if v:
            _strip_quotes = lambda s: s[1:-1] if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")) else s
            obj[k] = _strip_quotes(v)
        else:
            # 嵌套块
            sub_items, new_i = _parse_yaml_block(lines, i + 1)
            obj[k] = sub_items if sub_items else ""
            i = new_i
            # 继续看同级别属性
            while i < len(lines):
                line = lines[i]
                if not line.strip():
                    i += 1
                    continue
                ind = len(line) - len(line.lstrip())
                if ind <= list_indent:
                    break
                s = line.strip()
                ci = s.find(":")
                if ci != -1 and not s.startswith("-"):
                    kk = s[:ci].strip()
                    vv = s[ci + 1:].strip()
                    if vv:
                        obj[kk] = _strip_quotes(vv) if callable(locals().get('_strip_quotes')) else vv
                    else:
                        sub, new_i2 = _parse_yaml_block(lines, i + 1)
                        obj[kk] = sub if sub else ""
                        i = new_i2
                        continue
                i += 1
            return obj, i

    i += 1
    # 后续行: key: value（缩进大于 list_indent）
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= list_indent:
            break
        s = line.strip()
        if s.startswith("- "):
            break
        ci = s.find(":")
        if ci != -1:
            k = s[:ci].strip()
            v = s[ci + 1:].strip()
            if v:
                if (v.startswith('"') and v.endswith('"')) or \
                   (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                # 尝试解析 JSON 内联对象 { type: string, required: true }
                if v.startswith("{") and v.endswith("}"):
                    try:
                        # 修正简写 YAML 为 JSON
                        json_str = v.replace("'", '"')
                        # 给没有引号的 key 加引号
                        json_str = re.sub(r'(\w+)\s*:', r'"\1":', json_str)
                        # true/false
                        json_str = json_str.replace(": true", ": true").replace(": false", ": false")
                        obj[k] = json.loads(json_str)
                    except (json.JSONDecodeError, Exception):
                        obj[k] = v
                else:
                    obj[k] = v
            else:
                sub, new_i = _parse_yaml_block(lines, i + 1)
                obj[k] = sub if sub else ""
                i = new_i
                continue
        i += 1
    return obj, i


# ── 触发匹配 ──

def match_skill_triggers(user_text: str, triggers: list[str]) -> int:
    """计算用户消息与 skill 触发关键词的匹配分数。0 = 不匹配。"""
    if not user_text or not triggers:
        return 0
    text_lower = user_text.lower()
    return sum(1 for t in triggers if t.lower() in text_lower)


# ── Redis CRUD ──

def save_skill(
    tenant_id: str,
    name: str,
    description: str,
    triggers: list[str],
    instructions: str,
    tool_defs: list[dict] | None = None,
    tool_code: str = "",
    source: str = "manual",
) -> ToolResult:
    """保存 skill 到 Redis。"""
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")
    if not name:
        return ToolResult.invalid_param("缺少 skill name")

    # 规范化 name
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_").lower()
    if not name:
        return ToolResult.invalid_param("skill name 格式无效")

    # 验证 tool_code（如果有）
    if tool_code:
        from app.tools.sandbox import validate_code, compile_tool
        violations = validate_code(tool_code)
        if violations:
            return ToolResult.blocked(
                "工具代码安全检查未通过:\n" + "\n".join(f"- {v}" for v in violations)
            )
        _, errors = compile_tool(tool_code)
        if errors:
            return ToolResult.error(
                "工具代码编译失败:\n" + "\n".join(f"- {e}" for e in errors)
            )

    now = str(int(time.time()))
    key = _skill_key(tenant_id, name)
    idx = _index_key(tenant_id)

    # 检查是否更新
    existing_version = redis_exec("HGET", key, "version")
    version = str(int(existing_version) + 1) if existing_version else "1"

    cmds = [
        ["HSET", key, "name", name],
        ["HSET", key, "description", description or ""],
        ["HSET", key, "triggers", json.dumps(triggers or [], ensure_ascii=False)],
        ["HSET", key, "instructions", instructions or ""],
        ["HSET", key, "tool_defs", json.dumps(tool_defs or [], ensure_ascii=False)],
        ["HSET", key, "tool_code", tool_code or ""],
        ["HSET", key, "source", source],
        ["HSET", key, "updated_at", now],
        ["HSET", key, "version", version],
        ["SADD", idx, name],
    ]
    if not existing_version:
        cmds.append(["HSET", key, "created_at", now])

    results = redis_pipeline(cmds)
    if any(r is None for r in results):
        return ToolResult.api_error("Redis 写入失败")

    action = "更新" if existing_version else "创建"
    tool_count = len(tool_defs) if tool_defs else 0
    trigger_str = ", ".join(triggers[:5]) if triggers else "无"

    return ToolResult.success(
        f"Skill '{name}' {action}成功 (v{version})\n"
        f"描述: {description}\n"
        f"触发关键词: {trigger_str}\n"
        f"工具数: {tool_count}\n"
        f"指令长度: {len(instructions or '')} 字符\n"
        f"来源: {source}"
    )


def get_skill(tenant_id: str, name: str) -> dict[str, Any] | None:
    """从 Redis 读取单个 skill 的完整数据。"""
    key = _skill_key(tenant_id, name)
    data = redis_exec("HGETALL", key)
    if not data or not isinstance(data, dict):
        return None
    # 反序列化 JSON 字段
    for json_field in ("triggers", "tool_defs"):
        if json_field in data and isinstance(data[json_field], str):
            try:
                data[json_field] = json.loads(data[json_field])
            except json.JSONDecodeError:
                data[json_field] = []
    return data


def list_skills(tenant_id: str) -> list[dict[str, Any]]:
    """列出租户所有 skills 的摘要信息。"""
    idx = _index_key(tenant_id)
    names = redis_exec("SMEMBERS", idx)
    if not names:
        return []

    skills = []
    for name in sorted(names):
        key = _skill_key(tenant_id, name)
        # 只读摘要字段
        desc = redis_exec("HGET", key, "description") or ""
        triggers_str = redis_exec("HGET", key, "triggers") or "[]"
        source = redis_exec("HGET", key, "source") or "manual"
        version = redis_exec("HGET", key, "version") or "1"
        tool_defs_str = redis_exec("HGET", key, "tool_defs") or "[]"

        try:
            triggers = json.loads(triggers_str)
        except json.JSONDecodeError:
            triggers = []
        try:
            tool_defs = json.loads(tool_defs_str)
        except json.JSONDecodeError:
            tool_defs = []

        skills.append({
            "name": name,
            "description": desc,
            "triggers": triggers,
            "source": source,
            "version": version,
            "tool_count": len(tool_defs),
        })
    return skills


def delete_skill(tenant_id: str, name: str) -> ToolResult:
    """删除 skill。"""
    if not tenant_id or not name:
        return ToolResult.invalid_param("缺少 tenant_id 或 name")
    key = _skill_key(tenant_id, name)
    idx = _index_key(tenant_id)
    exists = redis_exec("EXISTS", key)
    if not exists:
        return ToolResult.not_found(f"Skill '{name}' 不存在")
    redis_pipeline([["DEL", key], ["SREM", idx, name]])
    return ToolResult.success(f"Skill '{name}' 已删除")


def export_skill_md(tenant_id: str, name: str) -> str | None:
    """将 skill 导出为 SKILL.md 格式字符串。"""
    data = get_skill(tenant_id, name)
    if not data:
        return None

    lines = ["---"]
    lines.append(f"name: {data.get('name', name)}")
    if data.get("description"):
        lines.append(f"description: \"{data['description']}\"")

    triggers = data.get("triggers", [])
    if triggers:
        lines.append("triggers:")
        for t in triggers:
            lines.append(f"  - {t}")

    tool_defs = data.get("tool_defs", [])
    if tool_defs:
        lines.append("tools:")
        for td in tool_defs:
            lines.append(f"  - name: {td.get('name', '')}")
            if td.get("description"):
                lines.append(f"    description: \"{td['description']}\"")
            params = td.get("input_schema", {}).get("properties", {})
            req = set(td.get("input_schema", {}).get("required", []))
            if params:
                lines.append("    parameters:")
                for pname, pspec in params.items():
                    parts = [f"type: {pspec.get('type', 'string')}"]
                    if pname in req:
                        parts.append("required: true")
                    if pspec.get("description"):
                        parts.append(f"description: \"{pspec['description']}\"")
                    lines.append(f"      {pname}: {{ {', '.join(parts)} }}")

    lines.append("---")
    lines.append("")

    instructions = data.get("instructions", "")
    if instructions:
        lines.append(instructions)

    return "\n".join(lines)


# ── 触发加载（给 base_agent 调用） ──

def load_triggered_skills(
    tenant_id: str, user_text: str
) -> tuple[str, list[dict], dict]:
    """根据用户消息触发匹配 skill，返回需要注入的内容。

    Returns:
        (instructions_text, tool_definitions, tool_handlers)
        - instructions_text: 匹配的 skill 的 instructions 拼接（注入 system prompt）
        - tool_definitions: 匹配的 skill 的工具定义列表
        - tool_handlers: 匹配的 skill 的工具 handler 映射
    """
    if not tenant_id or not user_text:
        return "", [], {}

    idx = _index_key(tenant_id)
    names = redis_exec("SMEMBERS", idx)
    if not names:
        return "", [], {}

    matched: list[tuple[int, str]] = []  # (score, name)
    for name in names:
        key = _skill_key(tenant_id, name)
        triggers_str = redis_exec("HGET", key, "triggers") or "[]"
        try:
            triggers = json.loads(triggers_str)
        except json.JSONDecodeError:
            continue
        score = match_skill_triggers(user_text, triggers)
        if score > 0:
            matched.append((score, name))

    if not matched:
        return "", [], {}

    # 按分数排序，取 top 3
    matched.sort(reverse=True)
    top_skills = matched[:3]

    all_instructions: list[str] = []
    all_tool_defs: list[dict] = []
    all_tool_map: dict[str, Any] = {}
    total_instruction_len = 0
    _SKILL_INSTRUCTION_BUDGET = 8000  # 总预算

    for _, name in top_skills:
        data = get_skill(tenant_id, name)
        if not data:
            continue

        # 注入 instructions
        instructions = data.get("instructions", "")
        if instructions and total_instruction_len + len(instructions) <= _SKILL_INSTRUCTION_BUDGET:
            all_instructions.append(
                f"\n<skill name=\"{name}\">\n{instructions}\n</skill>"
            )
            total_instruction_len += len(instructions)

        # 注入 tool definitions
        tool_defs = data.get("tool_defs", [])
        if tool_defs:
            all_tool_defs.extend(tool_defs)

        # 编译 tool code（如果有）
        tool_code = data.get("tool_code", "")
        if tool_code and tool_defs:
            try:
                from app.tools.sandbox import compile_tool, execute_tool
                module_dict, errors = compile_tool(tool_code)
                if not errors:
                    for tool_name, handler in module_dict.get("TOOL_MAP", {}).items():
                        _h = handler
                        all_tool_map[tool_name] = lambda args, _handler=_h: execute_tool(
                            _handler, args
                        )
            except Exception:
                logger.warning("skill %s tool_code compile failed", name, exc_info=True)

        logger.info(
            "skill triggered: %s (instructions=%d, tools=%d)",
            name, len(instructions), len(tool_defs),
        )

    instructions_text = ""
    if all_instructions:
        instructions_text = "\n\n## 已激活技能\n" + "\n".join(all_instructions)

    return instructions_text, all_tool_defs, all_tool_map
