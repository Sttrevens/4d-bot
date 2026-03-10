"""自定义工具管理 —— 元工具（meta-tools）

让 bot 在运行时创建、测试、更新、删除自定义工具。
工具代码存储在 Redis，按租户隔离，无需重启即可热加载。

存储结构（Redis）：
  Hash key:  custom_tools:{tenant_id}:{tool_name}
  Fields:    name, description, code, input_schema (JSON), risk_level,
             created_at, updated_at, version

  Set key:   custom_tools:{tenant_id}:_index
  Members:   所有工具名（用于快速列举）
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.services.redis_client import execute as redis_exec, pipeline as redis_pipeline
from app.tools.sandbox import compile_tool, execute_tool, validate_code
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── Redis key helpers ──

def _tool_key(tenant_id: str, tool_name: str) -> str:
    return f"custom_tools:{tenant_id}:{tool_name}"


def _index_key(tenant_id: str) -> str:
    return f"custom_tools:{tenant_id}:_index"


# ── 内置工具名保护 ──

_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset({
    "think", "read_file", "write_file", "search_files", "search_code",
    "git_create_branch", "git_diff", "create_pull_request", "list_pull_requests",
    "web_search", "save_memory", "recall_memory", "create_plan",
    "activate_plan", "update_plan_step", "list_plans", "get_plan_detail",
    "cancel_plan", "schedule_step", "list_calendars", "list_events",
    "create_event", "update_event", "delete_event", "check_availability",
    "create_feishu_doc", "get_doc_content", "list_docs", "search_docs",
    "create_feishu_task", "list_feishu_tasks", "update_feishu_task",
    "list_feishu_tasklists", "list_tasklist_tasks", "list_minutes",
    "get_minutes_detail", "lookup_user", "list_known_users",
    "send_message_to_user", "send_message_to_group", "list_bot_groups",
    "fetch_chat_history", "list_github_issues", "get_issue_detail",
    "create_github_issue", "comment_on_issue", "get_bot_errors",
    "self_search_code", "self_read_file", "self_edit_file",
    "railway_redeploy", "list_bitable_tables", "list_bitable_fields",
    "list_bitable_records", "create_bitable_record", "update_bitable_record",
    # meta-tools 自身也不能被覆盖
    "create_custom_tool", "list_custom_tools", "update_custom_tool",
    "delete_custom_tool", "test_custom_tool",
    # 能力获取层工具
    "install_package", "list_dynamic_packages", "uninstall_package",
    "browser_open", "browser_do", "browser_read", "browser_close",
    "assess_capability", "request_infra_change", "guide_human",
})

# 风险级别
RISK_LEVELS = ("green", "yellow", "red")


# ── 工具 CRUD ──

def create_custom_tool(args: dict) -> ToolResult:
    """创建自定义工具。bot 生成代码后调用此工具注册。"""
    tenant_id = args.get("tenant_id", "")
    name = args.get("name", "").strip()
    description = args.get("description", "").strip()
    code = args.get("code", "").strip()
    input_schema = args.get("input_schema", {})
    risk_level = args.get("risk_level", "green")

    # 参数校验
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")
    if not name:
        return ToolResult.invalid_param("缺少工具名 name")
    if not code:
        return ToolResult.invalid_param("缺少工具代码 code")
    if not description:
        return ToolResult.invalid_param("缺少工具描述 description")
    if name in _BUILTIN_TOOL_NAMES:
        return ToolResult.blocked(f"'{name}' 是内置工具名，不能覆盖")
    if risk_level not in RISK_LEVELS:
        return ToolResult.invalid_param(f"risk_level 必须是 {RISK_LEVELS} 之一")

    # 静态安全检查
    violations = validate_code(code)
    if violations:
        return ToolResult.blocked(f"代码安全检查未通过:\n" + "\n".join(f"- {v}" for v in violations))

    # 编译验证
    module_dict, errors = compile_tool(code)
    if errors:
        return ToolResult.error(f"代码编译/验证失败:\n" + "\n".join(f"- {e}" for e in errors))

    # 验证工具名匹配
    defined_names = [td["name"] for td in module_dict["TOOL_DEFINITIONS"]]
    if name not in defined_names:
        # 如果只定义了一个工具，强制要求 name 与之匹配，防止混淆
        if len(defined_names) == 1:
            actual_name = defined_names[0]
            return ToolResult.invalid_param(
                f"工具名 '{name}' 与代码中定义的工具名 '{actual_name}' 不匹配。请将参数 name 改为 '{actual_name}' 以保持一致。"
            )
        # 如果定义了多个工具，允许使用一个自定义的组名（如 feishu_mail_ops）作为存储键
        logger.info("creating multi-tool custom tool '%s' with functions %s", name, defined_names)

    # 写入 Redis
    now = str(int(time.time()))
    schema_json = json.dumps(input_schema, ensure_ascii=False) if isinstance(input_schema, dict) else str(input_schema)

    key = _tool_key(tenant_id, name)
    idx = _index_key(tenant_id)

    results = redis_pipeline([
        ["HSET", key, "name", name],
        ["HSET", key, "description", description],
        ["HSET", key, "code", code],
        ["HSET", key, "input_schema", schema_json],
        ["HSET", key, "risk_level", risk_level],
        ["HSET", key, "created_at", now],
        ["HSET", key, "updated_at", now],
        ["HSET", key, "version", "1"],
        ["SADD", idx, name],
    ])

    if any(r is None for r in results):
        return ToolResult.api_error("Redis 写入失败")

    return ToolResult.success(
        f"自定义工具 '{name}' 创建成功！\n"
        f"描述: {description}\n"
        f"风险级别: {risk_level}\n"
        f"下次对话中将自动可用。"
    )


def list_custom_tools(args: dict) -> ToolResult:
    """列出当前租户的所有自定义工具。"""
    tenant_id = args.get("tenant_id", "")
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")

    idx = _index_key(tenant_id)
    names = redis_exec("SMEMBERS", idx)

    if not names:
        return ToolResult.success("当前没有自定义工具。")

    lines = [f"共 {len(names)} 个自定义工具:\n"]
    for name in sorted(names):
        key = _tool_key(tenant_id, name)
        desc = redis_exec("HGET", key, "description") or ""
        risk = redis_exec("HGET", key, "risk_level") or "green"
        version = redis_exec("HGET", key, "version") or "1"
        lines.append(f"- **{name}** (v{version}, {risk}): {desc}")

    return ToolResult.success("\n".join(lines))


def update_custom_tool(args: dict) -> ToolResult:
    """更新自定义工具的代码或描述。"""
    tenant_id = args.get("tenant_id", "")
    name = args.get("name", "").strip()
    if not tenant_id or not name:
        return ToolResult.invalid_param("缺少 tenant_id 或 name")

    key = _tool_key(tenant_id, name)
    existing = redis_exec("HGET", key, "name")
    if not existing:
        return ToolResult.not_found(f"自定义工具 '{name}' 不存在")

    code = args.get("code", "").strip()
    description = args.get("description", "").strip()

    if code:
        violations = validate_code(code)
        if violations:
            return ToolResult.blocked(f"代码安全检查未通过:\n" + "\n".join(f"- {v}" for v in violations))

        module_dict, errors = compile_tool(code)
        if errors:
            return ToolResult.error(f"代码编译/验证失败:\n" + "\n".join(f"- {e}" for e in errors))

    now = str(int(time.time()))
    version = redis_exec("HGET", key, "version") or "1"
    new_version = str(int(version) + 1)

    cmds: list[list[str | int]] = [
        ["HSET", key, "updated_at", now],
        ["HSET", key, "version", new_version],
    ]
    if code:
        cmds.append(["HSET", key, "code", code])
    if description:
        cmds.append(["HSET", key, "description", description])
    if "input_schema" in args:
        schema_json = json.dumps(args["input_schema"], ensure_ascii=False)
        cmds.append(["HSET", key, "input_schema", schema_json])
    if "risk_level" in args and args["risk_level"] in RISK_LEVELS:
        cmds.append(["HSET", key, "risk_level", args["risk_level"]])

    redis_pipeline(cmds)
    return ToolResult.success(f"自定义工具 '{name}' 已更新到 v{new_version}")


def delete_custom_tool(args: dict) -> ToolResult:
    """删除自定义工具。"""
    tenant_id = args.get("tenant_id", "")
    name = args.get("name", "").strip()
    if not tenant_id or not name:
        return ToolResult.invalid_param("缺少 tenant_id 或 name")

    key = _tool_key(tenant_id, name)
    idx = _index_key(tenant_id)

    existing = redis_exec("HGET", key, "name")
    if not existing:
        return ToolResult.not_found(f"自定义工具 '{name}' 不存在")

    redis_pipeline([
        ["DEL", key],
        ["SREM", idx, name],
    ])
    return ToolResult.success(f"自定义工具 '{name}' 已删除。")


def test_custom_tool(args: dict) -> ToolResult:
    """测试自定义工具（dry run）。编译代码并用测试参数执行 handler。"""
    code = args.get("code", "").strip()
    tool_name = args.get("name", "").strip()
    test_args = args.get("test_args", {})

    if not code:
        return ToolResult.invalid_param("缺少 code")
    if not tool_name:
        return ToolResult.invalid_param("缺少 name（要测试的工具名）")

    # 编译
    module_dict, errors = compile_tool(code)
    if errors:
        return ToolResult.error(f"编译失败:\n" + "\n".join(f"- {e}" for e in errors))

    # 查找 handler
    tool_map = module_dict["TOOL_MAP"]
    handler = tool_map.get(tool_name)
    if handler is None:
        return ToolResult.error(f"TOOL_MAP 中找不到 '{tool_name}'，可用: {list(tool_map.keys())}")

    # 执行
    result = execute_tool(handler, test_args)

    status = "成功" if result.ok else f"失败 [{result.code}]"
    return ToolResult.success(
        f"测试结果 ({status}):\n{result.content}"
    )


# ── 加载租户自定义工具（供 kimi_coder._get_tenant_tools 调用）──

def load_tenant_tools(tenant_id: str) -> tuple[list[dict], dict]:
    """从 Redis 加载租户的所有自定义工具。

    Returns:
        (tool_definitions, tool_map) — 与内置工具同格式，可直接合并。
        加载失败的工具会被跳过并记录日志。
    """
    idx = _index_key(tenant_id)
    names = redis_exec("SMEMBERS", idx)

    if not names:
        return [], {}

    all_defs_map: dict[str, dict] = {}
    all_map: dict[str, Any] = {}

    for name in names:
        key = _tool_key(tenant_id, name)
        code = redis_exec("HGET", key, "code")
        if not code:
            logger.warning("custom tool %s has no code, skipping", name)
            continue

        module_dict, errors = compile_tool(code)
        if errors:
            logger.warning("custom tool %s compile failed: %s", name, errors)
            continue

        # 用沙箱版 handler 包装，确保执行也带超时保护
        for td in module_dict["TOOL_DEFINITIONS"]:
            all_defs_map[td["name"]] = td

        # 检测是否使用了 sandbox_caps（需要更长超时）
        uses_caps = "sandbox_caps" in code

        for tool_name, handler in module_dict["TOOL_MAP"].items():
            # 包一层 execute_tool 保护
            _h = handler  # 闭包捕获
            _ext = uses_caps
            all_map[tool_name] = lambda args, _handler=_h, _extended=_ext: execute_tool(
                _handler, args, extended_timeout=_extended
            )

    logger.info("loaded %d custom tools for tenant %s", len(all_map), tenant_id)
    return list(all_defs_map.values()), all_map


# ── 工具定义（Anthropic 格式）──

TOOL_DEFINITIONS = [
    {
        "name": "create_custom_tool",
        "description": (
            "创建一个新的自定义工具。当用户需要你做某件事但现有工具不支持时，"
            "你可以编写 Python 代码创建新工具。代码必须定义 TOOL_DEFINITIONS 列表和 TOOL_MAP 字典。"
            "一个代码块可以定义多个工具（函数），此时 name 参数可以作为工具组名（如 feishu_ops）。"
            "handler 函数接收 dict 参数，返回 ToolResult。可以使用 httpx 发网络请求、bs4 解析 HTML。"
            "\n\nsandbox_caps 提供的图片处理能力（沙箱内可用）:\n"
            "- read_user_image(path) → bytes: 安全读取 /tmp/user_img_* 图片文件\n"
            "- slice_image_grid(data, rows, cols, target_row=N) → list[bytes] | bytes: "
            "将图片按网格切片（适合 sprite sheet 等密集图逐行分析）\n"
            "- gemini_analyze_image(data, prompt) → AnalysisResult: 用 Gemini 分析图片\n"
            "用法: read_user_image 读文件 → slice_image_grid 切片 → gemini_analyze_image 逐行识别\n"
            "\n代码模板:\n"
            "```\n"
            "import httpx\n"
            "from app.tools.tool_result import ToolResult\n\n"
            "def my_handler(args: dict) -> ToolResult:\n"
            "    query = args.get('query', '')\n"
            "    # ... 做事 ...\n"
            "    return ToolResult.success('结果')\n\n"
            "TOOL_DEFINITIONS = [{\n"
            "    'name': 'my_tool',\n"
            "    'description': '工具描述',\n"
            "    'input_schema': {\n"
            "        'type': 'object',\n"
            "        'properties': {'query': {'type': 'string', 'description': '参数描述'}},\n"
            "        'required': ['query']\n"
            "    }\n"
            "}]\n\n"
            "TOOL_MAP = {'my_tool': my_handler}\n"
            "```"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "租户 ID（系统自动填充）"},
                "name": {"type": "string", "description": "工具名（英文下划线，如 batch_message_1688）"},
                "description": {"type": "string", "description": "工具功能描述（给 LLM 看的）"},
                "code": {"type": "string", "description": "完整的 Python 工具代码"},
                "input_schema": {
                    "type": "object",
                    "description": "工具参数的 JSON Schema（和 TOOL_DEFINITIONS 中的一致）",
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["green", "yellow", "red"],
                    "description": "风险级别: green=只读, yellow=写操作需确认, red=批量写入/第三方账号",
                    "default": "green",
                },
            },
            "required": ["tenant_id", "name", "description", "code", "input_schema"],
        },
    },
    {
        "name": "list_custom_tools",
        "description": "列出当前租户已创建的所有自定义工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "租户 ID（系统自动填充）"},
            },
            "required": ["tenant_id"],
        },
    },
    {
        "name": "update_custom_tool",
        "description": "更新已有自定义工具的代码、描述或参数 schema。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "租户 ID（系统自动填充）"},
                "name": {"type": "string", "description": "工具名"},
                "code": {"type": "string", "description": "新的完整代码（可选，不填则不更新）"},
                "description": {"type": "string", "description": "新的描述（可选）"},
                "input_schema": {"type": "object", "description": "新的参数 schema（可选）"},
                "risk_level": {
                    "type": "string",
                    "enum": ["green", "yellow", "red"],
                    "description": "新的风险级别（可选）",
                },
            },
            "required": ["tenant_id", "name"],
        },
    },
    {
        "name": "delete_custom_tool",
        "description": "删除一个自定义工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "租户 ID（系统自动填充）"},
                "name": {"type": "string", "description": "要删除的工具名"},
            },
            "required": ["tenant_id", "name"],
        },
    },
    {
        "name": "test_custom_tool",
        "description": (
            "测试自定义工具代码（不保存）。编译代码并用测试参数执行一次。"
            "建议在 create_custom_tool 之前先测试。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要测试的工具名（TOOL_MAP 中的 key）"},
                "code": {"type": "string", "description": "完整的 Python 工具代码"},
                "test_args": {
                    "type": "object",
                    "description": "测试参数（传给 handler 的 dict）",
                },
            },
            "required": ["name", "code"],
        },
    },
]

TOOL_MAP = {
    "create_custom_tool": create_custom_tool,
    "list_custom_tools": list_custom_tools,
    "update_custom_tool": update_custom_tool,
    "delete_custom_tool": delete_custom_tool,
    "test_custom_tool": test_custom_tool,
}
