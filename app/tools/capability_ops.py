"""能力获取元工具 —— 打通「边界墙」

三个工具协作，让 bot 具备自主获取新能力的"元能力"：

1. assess_capability — 评估当前能力是否满足任务需求，识别 gap
2. request_infra_change — 当需要修改基础设施层代码时，生成变更方案并请求管理员审批
3. guide_human — 需要人工操作时，生成引导清单并跟踪完成状态

这三个工具构成了 Capability Acquisition Layer 的核心：
- bot 先 assess → 发现 gap → 尝试自己解决（install_package / create_custom_tool）
- 解决不了 → request_infra_change（需要改基础设施）或 guide_human（需要人工操作）
"""

from __future__ import annotations

import json
import logging
import time

from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)


# ── 辅助函数 ──

def _get_tenant_id() -> str:
    try:
        from app.tenant.context import get_current_tenant
        return get_current_tenant().tenant_id or ""
    except Exception:
        return ""


def _redis_exec(cmd: str, *args):
    """安全调用 Redis（fail-open）"""
    try:
        from app.services.redis_client import execute
        return execute(cmd, *args)
    except Exception as e:
        logger.warning("capability_ops: Redis 操作失败: %s", e)
        return None


def _get_available_tools() -> list[str]:
    """获取当前租户可用的工具名列表"""
    try:
        tool_defs = _get_tool_defs()
        return [t["function"]["name"] for t in tool_defs]
    except Exception:
        return []


def _get_tool_defs() -> list[dict]:
    """获取当前租户可用的工具定义列表（含 name + description）"""
    try:
        from app.services.base_agent import _get_tenant_tools
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        tool_defs, _ = _get_tenant_tools(tenant)
        logger.info("capability_ops: _get_tool_defs 获取到 %d 个工具", len(tool_defs))
        return tool_defs
    except Exception as e:
        logger.error("capability_ops: _get_tool_defs 失败: %s", e, exc_info=True)
        return []


def _match_tools_for_task(task: str, tool_defs: list[dict]) -> list[tuple[str, str]]:
    """从任务描述中提取关键词，在所有工具的 name+description 中搜索匹配。

    中文没有空格分词，所以用 n-gram 滑窗（2~4 字符）提取所有可能的关键片段，
    双向匹配：任务片段在工具描述中出现 + 工具描述片段在任务中出现。

    Returns:
        [(tool_name, tool_description), ...] 按匹配度排序
    """
    import re

    task_lower = task.lower()

    # 生成 n-gram 关键片段（长度 2~4）
    def _ngrams(text: str, min_n: int = 2, max_n: int = 4) -> set[str]:
        # 先去标点，只保留有意义的字符
        clean = re.sub(r'[\s,，。！？、/\\()\[\]{}""''：:；;·\-—\d]+', ' ', text)
        grams: set[str] = set()
        for word in clean.split():
            for n in range(min_n, max_n + 1):
                for i in range(len(word) - n + 1):
                    grams.add(word[i:i + n])
        return grams

    task_grams = _ngrams(task_lower)

    scored: list[tuple[int, str, str]] = []
    for td in tool_defs:
        func = td.get("function", {})
        name = func.get("name", "")
        desc = func.get("description", "")
        # 跳过元工具自己
        if name in ("assess_capability", "think"):
            continue

        searchable = f"{name} {desc}".lower()
        tool_grams = _ngrams(searchable)

        # 双向匹配：任务片段在工具中 + 工具片段在任务中
        forward = task_grams & tool_grams  # 共有的 n-gram
        score = sum(len(g) for g in forward)

        if score > 0:
            scored.append((score, name, desc))

    # 按匹配度降序，取 top 10
    scored.sort(key=lambda x: -x[0])
    return [(name, desc) for _, name, desc in scored[:10]]


def _get_dynamic_packages(tenant_id: str) -> list[str]:
    """获取动态安装的包列表"""
    result = _redis_exec("SMEMBERS", f"sandbox:dynamic_modules:{tenant_id}")
    return sorted(result) if result else []


# ── 能力评估 ──


def _handle_assess_capability(args: dict) -> ToolResult:
    """评估 bot 是否有能力完成指定任务。

    核心改进：先做动态工具匹配，从实际可用的工具列表中搜索与任务相关的工具。
    如果找到匹配的现成工具，直接告诉 LLM 用哪个，不需要绕路。
    """
    task = args.get("task_description", "").strip()
    if not task:
        return ToolResult.invalid_param("task_description 不能为空")

    tenant_id = _get_tenant_id()
    tool_defs = _get_tool_defs()
    available_tools = [t["function"]["name"] for t in tool_defs]
    dynamic_packages = _get_dynamic_packages(tenant_id) if tenant_id else []

    # ── 第一步：动态工具匹配（最重要！）──
    matched_tools = _match_tools_for_task(task, tool_defs)

    lines = [f"任务: {task}", ""]

    if matched_tools:
        lines.append("★★★ 你已经有现成工具可以直接完成这个任务！★★★")
        lines.append("")
        lines.append("直接可用的相关工具：")
        for name, desc in matched_tools:
            # 截断过长的描述
            short_desc = desc[:80] + "..." if len(desc) > 80 else desc
            lines.append(f"  → {name} — {short_desc}")
        lines.append("")
        lines.append("请直接调用上面的工具，不需要 create_custom_tool 或 browser_* 等间接方式。")
        lines.append("")

    # ── 第二步：补充环境信息 ──
    # 检查 Playwright 是否可用
    playwright_available = False
    try:
        import playwright  # noqa: F401
        playwright_available = True
    except ImportError:
        pass

    lines.extend([
        "── 环境信息 ──",
        f"可用工具总数: {len(available_tools)}",
        f"动态安装的包: {', '.join(dynamic_packages) if dynamic_packages else '无'}",
        f"浏览器引擎: {'✓ 已安装' if playwright_available else '✗ 未安装'}",
    ])

    # ── 第三步：仅在没有匹配工具时，才给出获取新能力的路径 ──
    if not matched_tools:
        lines.extend([
            "",
            "── 没有找到直接匹配的现成工具 ──",
            "",
            "可以通过以下方式获取能力：",
            "1. create_custom_tool — 自己写代码创建新工具",
            "2. install_package — 安装 Python 包扩展能力",
            "3. browser_* — 浏览器自动化访问网页（需要 playwright）",
            "4. install_github_skill — 从 GitHub 安装现成工具",
            "5. request_infra_change — 申请修改基础设施",
            "6. guide_human — 引导用户完成人工操作",
        ])

    return ToolResult.success("\n".join(lines))


# ── 基础设施变更请求 ──

def _handle_request_infra_change(args: dict) -> ToolResult:
    """申请修改基础设施层文件"""
    file_path = args.get("file_path", "").strip()
    change_desc = args.get("change_description", "").strip()
    reason = args.get("reason", "").strip()
    proposed_diff = args.get("proposed_diff", "").strip()

    if not file_path or not change_desc or not reason:
        return ToolResult.invalid_param("file_path, change_description, reason 均为必填")

    tenant_id = _get_tenant_id()

    # 构建变更请求
    request_id = f"infra-{int(time.time())}"
    request_data = {
        "id": request_id,
        "tenant_id": tenant_id,
        "file_path": file_path,
        "change_description": change_desc,
        "reason": reason,
        "proposed_diff": proposed_diff,
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 存入 Redis
    key = f"infra_change_requests:{request_id}"
    _redis_exec(
        "SET", key,
        json.dumps(request_data, ensure_ascii=False),
    )
    # 添加到待处理列表
    _redis_exec("LPUSH", "infra_change_requests:pending", request_id)
    # 24 小时过期
    _redis_exec("EXPIRE", key, 86400)

    return ToolResult.success(
        f"基础设施变更请求已创建\n\n"
        f"请求 ID: {request_id}\n"
        f"目标文件: {file_path}\n"
        f"变更内容: {change_desc}\n"
        f"原因: {reason}\n"
        f"状态: 待审批\n\n"
        f"已通知管理员。在审批通过前，请考虑是否有替代方案：\n"
        f"- 能否用 create_custom_tool 实现同等功能？\n"
        f"- 能否用 install_package 安装现有的包来解决？"
    )


# ── 人工引导 ──

def _handle_guide_human(args: dict) -> ToolResult:
    """创建人工操作引导清单"""
    task = args.get("task", "").strip()
    steps = args.get("steps", [])
    reason = args.get("reason", "").strip()

    if not task:
        return ToolResult.invalid_param("task 不能为空")
    if not steps or not isinstance(steps, list):
        return ToolResult.invalid_param("steps 必须是非空列表")

    tenant_id = _get_tenant_id()

    # 构建引导清单
    guide_id = f"guide-{int(time.time())}"
    guide_data = {
        "id": guide_id,
        "tenant_id": tenant_id,
        "task": task,
        "steps": steps,
        "reason": reason,
        "completed_steps": [],
        "status": "active",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 存入 Redis（7 天过期）
    key = f"human_guide:{tenant_id}:{guide_id}"
    _redis_exec(
        "SET", key,
        json.dumps(guide_data, ensure_ascii=False),
    )
    _redis_exec("EXPIRE", key, 604800)

    # 格式化输出
    lines = [
        f"需要你的帮助完成以下操作：",
        "",
        f"任务: {task}",
    ]
    if reason:
        lines.append(f"原因: {reason}")
    lines.append("")

    for i, step in enumerate(steps, 1):
        lines.append(f"  {i}. {step}")

    lines.extend([
        "",
        "完成每一步后请告诉我，我会继续下一步。",
        "如果某一步遇到问题，也请告诉我具体情况。",
    ])

    return ToolResult.success("\n".join(lines))


# ── 工具注册（标准接口）──

TOOL_DEFINITIONS = [
    {
        "name": "assess_capability",
        "description": (
            "评估 bot 能力缺口（仅在 tools 列表里完全找不到相关工具时才用）。"
            "不要用这个工具查找已有工具——直接看 tools 列表更快。"
            "典型场景：需要安装新 Python 包、需要浏览器自动化、需要创建自定义工具。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "要评估的任务描述（如 '在小红书上发布图文帖子'）",
                },
            },
            "required": ["task_description"],
        },
    },
    {
        "name": "request_infra_change",
        "description": (
            "申请修改基础设施层文件（超出 auto-fix 白名单范围）。"
            "当 bot 需要修改 app/services/、app/config.py、Dockerfile 等核心文件时使用。"
            "生成变更方案存入 Redis，等待管理员审批后执行。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要修改的文件路径（如 'requirements.txt', 'Dockerfile'）",
                },
                "change_description": {
                    "type": "string",
                    "description": "变更内容描述",
                },
                "reason": {
                    "type": "string",
                    "description": "为什么需要这个变更",
                },
                "proposed_diff": {
                    "type": "string",
                    "description": "建议的代码变更（diff 格式或完整新内容）",
                },
            },
            "required": ["file_path", "change_description", "reason"],
        },
    },
    {
        "name": "guide_human",
        "description": (
            "当任务中有些步骤必须由人工完成时（如注册账号、扫码登录、授权 OAuth 等），"
            "用此工具生成清晰的操作引导清单，逐步指导用户完成。"
            "bot 会跟踪每一步的完成状态。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "需要人工完成的任务（如 '为 bot 注册小红书账号'）",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "步骤列表（按顺序）",
                },
                "reason": {
                    "type": "string",
                    "description": "为什么需要人工操作（解释 bot 为什么不能自己做）",
                },
            },
            "required": ["task", "steps"],
        },
    },
]

TOOL_MAP = {
    "assess_capability": _handle_assess_capability,
    "request_infra_change": _handle_request_infra_change,
    "guide_human": _handle_guide_human,
}
