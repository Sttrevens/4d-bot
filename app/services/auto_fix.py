"""全自动自我修复引擎

当 bot 运行时遇到错误，自动触发 LLM 诊断 → 定位代码 → 修复 → 推送 main → CI/CD 自动部署。

错误分类策略（不需要穷举错误模式，LLM 就是万能分类器）：
- unhandled / tool_exception / startup_check → 直接触发修复
- tool_error / api_error / timeout → 先用轻量 LLM（Flash）分类：
  是代码 bug → 触发修复；是用户传参或外部故障 → 跳过
- self_fix_error → 绝不触发（防递归）

支持两种 LLM 后端（通过 SELFFIX_PROVIDER 配置）：
- "gemini": 使用 Gemini 原生 SDK（推荐，可用 2.5 Pro 等强模型）
- ""（空）: 使用 Kimi / OpenAI 兼容 API（默认，向后兼容）

防护机制（防止无限循环）：
- 去抖：错误发生后等 10 秒再触发（合并连续错误）
- 冷却：两次修复之间至少间隔 10 分钟
- 守卫：同一时间只允许一个修复流程运行
- 上限：每小时最多 3 次自动修复
- 分类冷却：5 分钟内不重复调用 LLM 分类
- 过滤：不对自身修复过程产生的错误再次触发修复
- 白名单：只允许修改应用层代码（app/tools/、app/knowledge/），基础设施层由人工迭代
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from openai import AsyncOpenAI, RateLimitError

from app.config import settings
from app.services.kimi import _is_k2_model, _extra_body

logger = logging.getLogger(__name__)

# ── 防护参数 ──

_fix_lock = asyncio.Lock()
_last_fix_time: float = 0
_COOLDOWN_SECONDS = 600  # 两次修复之间最少间隔（秒）
_hourly_fix_count = 0
_hourly_reset_time: float = 0
_MAX_FIXES_PER_HOUR = 3
_debounce_handle: asyncio.Task | None = None
_DEBOUNCE_SECONDS = 10  # 最后一个错误后等多久再触发

# 绝不触发修复的错误类别（硬跳过，防止递归）
_SKIP_CATEGORIES = frozenset({"self_fix_error"})

# 数据质量告警：不自动修复，只生成诊断报告通知管理员审批
_DIAGNOSTIC_ONLY_CATEGORIES = frozenset({"data_quality"})

# 需要 LLM 分类后才决定是否修复的类别
# 这些错误可能是用户传参问题（不修），也可能是代码/配置 bug（要修）
_CLASSIFY_CATEGORIES = frozenset({"tool_error", "api_error", "timeout"})

# 当 bot 服务的 repo (GITHUB_REPO) 与自身 repo (SELF_REPO) 不同时，
# 只有这些类别才值得触发自我修复（真正的代码 bug），其余属于业务层/外部错误。
_CODE_BUG_CATEGORIES = frozenset({"unhandled", "tool_exception", "startup_check"})

_TRANSIENT_ERROR_PATTERNS = (
    "page.goto: timeout",
    "timeout 30000ms exceeded",
    "timed out after",
    "httpx.connecttimeout",
    "httpcore.connecttimeout",
    "connecttimeout",
    "readtimeout",
    "remoteprotocolerror",
    "server disconnected without sending a response",
    "proxyerror",
    "web_search 连续失败",
    "auto-lesson for web_search",
    "network is unreachable",
    "temporary failure",
    "connection reset",
    "connection aborted",
    "send msg session status invalid",
    "conversation end",
    "errcode=95018",
    "errcode=95013",
    "login wall",
)
_TRANSIENT_TOOL_NAMES = frozenset({
    "browser_open",
    "browser_read",
    "web_search",
    "fetch_url",
    "search_social_media",
    "xhs_search",
    "xhs_playwright_search",
    "send_text",
    "reply_text",
})


def _is_serving_other_repo() -> bool:
    """判断 bot 当前服务的 repo 是否不是自身 repo（即 GITHUB_REPO != SELF_REPO）"""
    gh_owner = settings.github.repo_owner.lower()
    gh_name = settings.github.repo_name.lower()
    self_owner = settings.self_repo_owner.lower()
    self_name = settings.self_repo_name.lower()
    return (gh_owner, gh_name) != (self_owner, self_name)


def _is_framework_error(error: "ErrorRecord") -> bool:
    """判断错误是否属于 bot 框架自身的 bug（而非业务逻辑/外部 API 错误）。"""
    return error.category in _CODE_BUG_CATEGORIES


def _errors_are_transient_only(errors: list) -> bool:
    """Return True when all triage errors are external/runtime transients.

    These should not reach the self-fix LLM because it may overfit a temporary
    network failure into risky source edits and deployments.
    """
    if not errors:
        return False

    for err in errors:
        category = str(getattr(err, "category", "") or "")
        if category not in _CLASSIFY_CATEGORIES:
            return False

        text = "\n".join([
            str(getattr(err, "summary", "") or ""),
            str(getattr(err, "detail", "") or ""),
            str(getattr(err, "tool_name", "") or ""),
            str(getattr(err, "tool_args", "") or ""),
        ]).lower()

        if "unknown tool" in text:
            return False
        if "syntaxerror" in text or "modulenotfounderror" in text or "importerror" in text:
            return False

        tool_name = str(getattr(err, "tool_name", "") or "")
        has_transient_pattern = any(pattern in text for pattern in _TRANSIENT_ERROR_PATTERNS)
        has_transient_tool = tool_name in _TRANSIENT_TOOL_NAMES
        if not (has_transient_pattern or (category == "timeout" and has_transient_tool)):
            return False

    return True

# ── 自我修复专用工具集（比正常对话少很多，只保留诊断和修复相关）──

_THINK_TOOL_DEF = {
    "name": "think",
    "description": "整理思路，规划诊断和修复步骤。",
    "input_schema": {
        "type": "object",
        "properties": {
            "thought": {"type": "string", "description": "你的思考过程"},
        },
        "required": ["thought"],
    },
}


def _build_fix_tools():
    """延迟导入，避免循环依赖"""
    from app.tools.self_ops import (
        TOOL_DEFINITIONS as SELF_TOOLS, TOOL_MAP as SELF_MAP,
    )
    from app.tools.server_ops import (
        TOOL_DEFINITIONS as SERVER_TOOLS, TOOL_MAP as SERVER_MAP,
    )
    from app.tools.web_search import (
        TOOL_DEFINITIONS as WEB_TOOLS, TOOL_MAP as WEB_MAP,
    )

    tool_map = {
        "think": lambda args: "OK",
        **SELF_MAP,
        **SERVER_MAP,
        **WEB_MAP,
    }

    all_defs = [_THINK_TOOL_DEF] + SELF_TOOLS + SERVER_TOOLS + WEB_TOOLS
    openai_tools = []
    for t in all_defs:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        })
    return tool_map, openai_tools


_FIX_SYSTEM_PROMPT = """\
你是一个全自动自我修复系统。你的 bot 代码在运行中遇到了错误，你需要诊断并修复。

目标：
1. 分析错误信息，理解 root cause
2. 在 bot 源代码中找到出问题的代码
3. 修复代码（直接推到 main），CI/CD 自动部署
4. 如果问题不在代码中（如配置错误、外部 API 临时故障、参数错误），则不需要修改代码，直接说明原因

你有以下工具：
- think: 整理思路
- get_bot_errors / clear_bot_errors: 查看/清空运行时错误
- get_deploy_logs / search_logs / get_deploy_status: 查看完整运行日志、搜索日志、查看进程状态
- self_read_file: 读源代码（支持 start_line/end_line 行范围读取）
- self_edit_file / self_write_file: 修改源代码（直接推到 main，CI/CD 自动部署）
- self_validate_file: 深度验证 Python 文件（语法+导入+危险模式），部署前必查
- self_list_tree / self_search_code / self_git_log: 浏览/搜索代码
- self_safe_deploy: 确认部署（记录回滚点，保存任务上下文以便重启后继续）
- self_rollback: 手动回滚到部署前状态
- web_search: 搜索技术文档、API 用法、错误解决方案（遇到不确定的外部 API 格式或行为时务必搜索）

注意：错误涉及的源代码片段已自动提取并提供给你（如有）。你不需要重复读取这些文件，可以直接分析。

诊断流程（推荐）：
1. 先用 get_bot_errors 查看结构化错误
2. 用 get_deploy_logs 或 search_logs 查看完整运行上下文（请求流程、API 返回值等）
3. 如果涉及外部 API 行为不确定（如支持什么格式、参数要求），用 web_search 查文档
4. 定位到代码后，用 self_read_file 确认上下文

部署流程：
1. 用 self_edit_file 或 self_write_file 修改代码 → 直接推到 main 分支
2. 推送到 main 后 GitHub Actions 会自动构建并重启服务
3. 修改完成后，先用 self_validate_file 验证修改的文件
4. 验证通过后，调用 self_safe_deploy 确认部署（记录回滚点）
5. 修复后用 clear_bot_errors 清空错误缓冲区

修复原则：
- 先用 think 分析错误，判断是否需要改代码
- 如果是代码 bug：定位 → 读代码 → 最小化修改 → 优先用 self_edit_file 做精确替换
- 如果是外部因素（API 临时 500、用户传参错误）：不改代码，直接说明
- commit message 要清晰描述修复了什么
- 如果不确定怎么修，用 web_search 搜索解决方案
- 不要大规模重构，只做针对性修复

⚠️ 你的修复范围有严格边界 — 只能修改「应用层」代码：
  ✅ 可以修改：app/tools/（工具函数）、app/knowledge/（知识库）
  ❌ 不能修改：其他所有文件（基础设施层）

不能修改的文件包括但不限于：
  - app/services/（所有核心服务：LLM provider、记忆、历史、平台 API 等）
  - app/main.py、app/config.py（入口和配置）
  - app/tenant/、app/channels/、app/router/、app/webhook/（多租户/路由/平台接入）
  - scripts/、templates/、.github/、Dockerfile 等（部署基础设施）

如果诊断发现 bug 在基础设施层（如 gemini_provider.py、kimi_coder.py 等），
请在修复报告中详细描述问题和建议的修复方案，但不要尝试修改这些文件。
管理员会人工处理基础设施层的问题。

部署影响评估（修改代码前必做）：
- 部署 = 重启服务 = 所有用户连接中断约 10-15 秒，期间的消息可能丢失
- 在决定修改代码前，用 think 评估：这个 bug 的影响是否大于重启造成的中断？
  * 严重（必须立即修复）：服务崩溃、核心功能完全不可用、安全漏洞
  * 中等（可以修复）：特定功能报错但不影响其他功能、用户可见的错误信息
  * 轻微（不要修复）：DeprecationWarning、偶发性超时、美化性问题、非核心功能的边缘 case
- 轻微问题：不要修改代码，在报告中说明问题和建议方案即可

重要：你修复的是 bot 框架自身的代码（self repo），不是 bot 正在服务的业务仓库。
只有 bot 框架的 Python 代码 bug 才需要修复，业务仓库的问题不在你的修复范围内。
"""

_MAX_FIX_ROUNDS = 25


def _extract_source_context(errors: list) -> str:
    """从错误的 traceback 中提取涉及的源代码文件和行号，预加载相关代码片段。

    这模拟了 Claude Code 的优势：修 bug 前先看到相关代码，而不是要 LLM 自己去找。
    """
    import re

    # 收集所有 traceback 中提到的 app/ 文件和行号
    file_lines: dict[str, set[int]] = {}
    for err in errors:
        detail = err.detail or ""
        # 匹配 Python traceback 格式：File "xxx/app/yyy.py", line 123
        matches = re.findall(r'File ".*?/(app/[^"]+\.py)", line (\d+)', detail)
        for fpath, line_no in matches:
            file_lines.setdefault(fpath, set()).add(int(line_no))

    if not file_lines:
        return ""

    # 对每个文件，读取错误行附近的代码（±15 行上下文）
    from app.tools.self_ops import self_read_file

    snippets = []
    context_radius = 15  # 错误行上下各 15 行

    for fpath, line_nos in list(file_lines.items())[:5]:  # 最多 5 个文件
        for line_no in sorted(line_nos)[:3]:  # 每个文件最多 3 个位置
            start = max(1, line_no - context_radius)
            end = line_no + context_radius
            result = self_read_file(fpath, "main", start, end)
            content = result.content if hasattr(result, 'content') else str(result)
            ok = result.ok if hasattr(result, 'ok') else not content.startswith("[ERROR]")
            if ok:
                snippets.append(f"── {fpath} (行 {start}-{end}，错误在行 {line_no}) ──\n{content}")

    return "\n\n".join(snippets) if snippets else ""


# ── 工具执行辅助（OpenAI 和 Gemini 共用）──

# ── 自动修复边界：allowlist（白名单）策略 ──
# 设计哲学：auto-fix 只能修改「应用层」代码，「基础设施层」必须由人工迭代。
#
# 应用层（auto-fix 可修改）：
#   - app/tools/     → 40+ 工具函数，运行时 bug 最常发生的地方
#   - app/knowledge/ → 知识库，bot 自我学习和经验积累
#
# 基础设施层（auto-fix 不可修改，allowlist 之外的一切）：
#   - app/services/  → LLM provider、记忆、历史、平台 API 封装等核心服务
#   - app/main.py, app/config.py → 入口和全局配置
#   - app/tenant/, app/channels/, app/router/, app/webhook/ → 多租户/路由/平台接入
#   - scripts/, templates/, .github/, Dockerfile 等 → 部署基础设施
#
# 为什么用 allowlist 而非 blocklist：
#   blocklist 容易遗漏新增的基础设施文件，一旦漏掉就相当于没有保护。
#   allowlist 是 fail-closed 的：新文件默认受保护，必须显式加入白名单才能被 auto-fix 修改。
#
# 注意：读取（self_read_file / self_search_code 等）不受限制，auto-fix 可以读任何文件来诊断问题。
#       限制的只是写入操作（self_write_file / self_edit_file）。

# 全局默认 allowlist（向后兼容）
_DEFAULT_ALLOWED_WRITE_PATHS = (
    "app/tools/",      # 工具函数 — auto-fix 的核心修复范围
    "app/knowledge/",  # 知识库 — bot 自我学习
)

# 向后兼容旧测试/旧调用方
_ALLOWED_WRITE_PATHS = _DEFAULT_ALLOWED_WRITE_PATHS


def _get_allowed_write_paths() -> tuple[str, ...]:
    """获取当前租户的 auto-fix 可写路径列表（per-tenant 策略引擎）。

    GTC OpenShell 借鉴：不同 bot 实例可以有不同的 auto-fix 权限边界。
    优先级：tenant.autofix_allowed_paths（列表）> 全局默认
    """
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        paths = getattr(tenant, "autofix_allowed_paths", None)
        if paths and isinstance(paths, (list, tuple)):
            return tuple(paths)
    except Exception:
        pass
    return _DEFAULT_ALLOWED_WRITE_PATHS


def _execute_tool(func_name: str, func_args: dict, tool_map: dict) -> str:
    """执行工具调用并返回结果字符串（含安全检查）"""
    from app.tools.tool_result import ToolResult

    # 安全检查：auto-fix 只能修改应用层文件（per-tenant allowlist 策略）
    if func_name in ("self_write_file", "self_edit_file"):
        path = func_args.get("path", "")
        allowed = _get_allowed_write_paths()
        if not any(path.startswith(a) for a in allowed):
            return (
                f"不允许修改 {path}（超出 auto-fix 修复范围）。\n"
                f"auto-fix 只能修改应用层代码：{', '.join(allowed)}\n"
                f"如果 bug 在基础设施层，请在修复报告中描述问题和建议方案，管理员会人工处理。"
            )

    handler = tool_map.get(func_name)
    if handler is None:
        return f"unknown tool: {func_name}"

    try:
        result = handler(func_args)
    except Exception as exc:
        return f"工具执行异常: {exc}"

    return result.content if isinstance(result, ToolResult) else str(result)


# ── 核心修复逻辑 ──


async def _run_self_fix(error_context: str) -> str:
    """根据配置选择 LLM 后端执行自我修复"""
    provider = settings.selffix.provider.lower()
    if provider == "gemini":
        return await _run_self_fix_gemini(error_context)
    return await _run_self_fix_openai(error_context)


async def _run_self_fix_openai(error_context: str) -> str:
    """用 OpenAI 兼容 API（Kimi 等）执行自我修复 agent 循环"""
    tool_map, openai_tools = _build_fix_tools()

    api_key = settings.selffix.api_key or settings.kimi.api_key
    base_url = settings.selffix.base_url or settings.kimi.base_url
    model = settings.selffix.model or settings.kimi.model

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    messages: list[dict] = [
        {"role": "system", "content": _FIX_SYSTEM_PROMPT},
        {"role": "user", "content": error_context},
    ]

    # 重复调用检测
    call_counter: dict[str, int] = {}
    _REPEAT_LIMIT = 3

    for round_num in range(_MAX_FIX_ROUNDS):
        logger.info("auto_fix openai round %d (model=%s)", round_num + 1, model)

        kwargs: dict = dict(
            model=model,
            messages=messages,
            tools=openai_tools,
            temperature=1 if _is_k2_model() else 0,
            max_tokens=8192,
        )
        extra = _extra_body()
        if extra:
            kwargs["extra_body"] = extra

        try:
            resp = await client.chat.completions.create(**kwargs)
        except RateLimitError:
            return "自我修复中止：API 额度耗尽，等明天再试"
        except Exception as exc:
            return f"自我修复中止：LLM 调用异常 - {exc}"

        choice = resp.choices[0]
        msg = choice.message

        # 无工具调用 → 最终回复
        if not msg.tool_calls:
            return msg.content or "自我修复完成（无需操作）"

        messages.append(msg.model_dump())

        # 执行工具（带重复检测）
        abort = False
        for tc in msg.tool_calls:
            func_name = tc.function.name
            try:
                func_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                func_args = {}

            call_key = f"{func_name}:{json.dumps(func_args, sort_keys=True, ensure_ascii=False)[:200]}"
            call_counter[call_key] = call_counter.get(call_key, 0) + 1
            if call_counter[call_key] > _REPEAT_LIMIT:
                logger.warning("auto_fix: repeated call detected (%dx): %s — aborting",
                               call_counter[call_key], call_key[:120])
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": "中止：你已重复调用同一工具超过 3 次，说明这个方向走不通。"
                               "请直接给出结论，不要再调工具。",
                })
                abort = True
                continue

            logger.info("auto_fix tool: %s(%s)", func_name, str(func_args)[:100])
            content = _execute_tool(func_name, func_args, tool_map)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": content,
            })

        if abort:
            try:
                kwargs_final = dict(model=model, messages=messages,
                                    temperature=0, max_tokens=4096)
                extra_f = _extra_body()
                if extra_f:
                    kwargs_final["extra_body"] = extra_f
                final = await client.chat.completions.create(**kwargs_final)
                return final.choices[0].message.content or "自我修复中止：重复调用"
            except Exception:
                pass
            return "自我修复中止：agent 陷入重复调用循环"

    # 轮次耗尽 → 不带 tools 的最终调用
    try:
        kwargs = dict(
            model=model,
            messages=messages,
            temperature=1 if _is_k2_model() else 0,
            max_tokens=8192,
        )
        extra = _extra_body()
        if extra:
            kwargs["extra_body"] = extra
        resp = await client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or "自我修复完成"
    except Exception:
        return "自我修复过程复杂，已执行部分步骤"


async def _run_self_fix_gemini(error_context: str) -> str:
    """用 Gemini 原生 SDK 执行自我修复 agent 循环（支持更强的推理模型）"""
    from google import genai
    from google.genai import types
    from app.services.gemini_provider import _openai_tools_to_gemini

    tool_map, openai_tools = _build_fix_tools()

    # ── 构建 Gemini Client ──
    api_key = settings.selffix.api_key
    if not api_key:
        return "自我修复中止：未配置 SELFFIX_API_KEY（Gemini 模式需要）"

    http_options: dict = {"timeout": 180_000}  # selffix 可能需要更长超时
    if settings.selffix.base_url:
        http_options["base_url"] = settings.selffix.base_url
    elif os.getenv("GOOGLE_GEMINI_BASE_URL"):
        http_options["base_url"] = os.getenv("GOOGLE_GEMINI_BASE_URL")
    proxy_url = os.getenv("GEMINI_PROXY", "")
    if proxy_url:
        http_options["async_client_args"] = {"proxy": proxy_url}

    client = genai.Client(api_key=api_key, http_options=http_options)
    model_name = settings.selffix.model or "gemini-3.1-pro-preview"

    logger.info("auto_fix gemini: model=%s base_url=%s",
                model_name, http_options.get("base_url", "(direct)"))

    # ── 工具转换 ──
    gemini_decls = _openai_tools_to_gemini(openai_tools)
    config = types.GenerateContentConfig(
        system_instruction=_FIX_SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=gemini_decls)],
        temperature=0,
        max_output_tokens=16384,
    )

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=error_context)])
    ]

    # 重复调用检测：同一工具+参数调用 3 次就强制中止
    call_counter: dict[str, int] = {}
    _REPEAT_LIMIT = 3

    for round_num in range(_MAX_FIX_ROUNDS):
        logger.info("auto_fix gemini round %d", round_num + 1)

        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            logger.exception("auto_fix gemini API call failed (round %d)", round_num + 1)
            return f"自我修复中止：Gemini API 调用异常 - {type(exc).__name__}: {exc}"

        if not response.candidates:
            return "自我修复中止：Gemini 返回空结果"

        candidate = response.candidates[0]
        content_obj = candidate.content
        if not content_obj or not content_obj.parts:
            return "自我修复中止：Gemini 返回空内容"

        # 分离 function_call 和文本
        function_calls = []
        text_parts = []
        for part in content_obj.parts:
            if part.function_call:
                function_calls.append(part)
            elif part.text:
                text_parts.append(part.text)

        # 没有工具调用 → 最终回复
        if not function_calls:
            return "\n".join(text_parts) or "自我修复完成（无需操作）"

        # 将 model 响应加入 contents
        contents.append(content_obj)

        # ── 执行工具调用（带重复检测）──
        response_parts: list[types.Part] = []
        abort = False

        for fc_part in function_calls:
            fc = fc_part.function_call
            func_name = fc.name
            func_args = dict(fc.args) if fc.args else {}

            # 重复调用检测
            call_key = f"{func_name}:{json.dumps(func_args, sort_keys=True, ensure_ascii=False)[:200]}"
            call_counter[call_key] = call_counter.get(call_key, 0) + 1
            if call_counter[call_key] > _REPEAT_LIMIT:
                logger.warning("auto_fix: repeated call detected (%dx): %s — aborting",
                               call_counter[call_key], call_key[:120])
                response_parts.append(types.Part(
                    function_response=types.FunctionResponse(
                        name=func_name,
                        response={"result": "中止：你已重复调用同一工具超过 3 次，说明这个方向走不通。"
                                            "请直接给出结论（这个问题是否可修复、根因是什么），不要再调工具。"},
                    )
                ))
                abort = True
                continue

            logger.info("auto_fix tool: %s(%s)", func_name, str(func_args)[:100])
            result_str = _execute_tool(func_name, func_args, tool_map)

            response_parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=func_name,
                    response={"result": result_str},
                )
            ))

        contents.append(types.Content(role="user", parts=response_parts))

        # 重复过多 → 再给一轮让 LLM 输出结论，然后结束
        if abort:
            try:
                final = await client.aio.models.generate_content(
                    model=model_name, contents=contents, config=config)
                if final.candidates and final.candidates[0].content:
                    parts = final.candidates[0].content.parts or []
                    return "\n".join(p.text for p in parts if p.text) or "自我修复中止：重复调用"
            except Exception:
                pass
            return "自我修复中止：agent 陷入重复调用循环"

    # 轮次耗尽
    return "自我修复过程复杂，已执行部分步骤"


# ── 管理员通知 ──


async def _notify_admins(fix_result: str) -> None:
    """通过飞书 DM 通知管理员修复结果"""
    if not settings.admin_open_ids:
        logger.info("auto_fix: no admin_open_ids configured, skipping notification")
        return

    if len(fix_result) > 3000:
        fix_result = fix_result[:3000] + "\n... (已截断)"

    text = f"[自动修复报告]\n\n{fix_result}"

    # 必须切换到飞书租户上下文才能获取 token（当前可能是 wecom_kf 租户）
    from app.tenant.context import get_current_tenant, set_current_tenant
    from app.tenant.registry import tenant_registry
    original_tenant = get_current_tenant()
    # 找一个飞书平台的租户来发通知
    feishu_tenant = None
    for t in tenant_registry.all_tenants().values():
        if t.platform == "feishu" and t.app_id:
            feishu_tenant = t
            break
    if not feishu_tenant:
        logger.warning("auto_fix: no feishu tenant available for notification")
        return
    set_current_tenant(feishu_tenant)

    from app.services.feishu import FeishuClient
    client = FeishuClient()
    try:
        token = await client._get_token()
    except Exception:
        logger.warning("auto_fix: failed to get feishu token for notification")
        return
    finally:
        set_current_tenant(original_tenant)

    import httpx
    for admin_id in settings.admin_open_ids:
        if not admin_id:
            continue
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
                await http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    json={
                        "receive_id": admin_id,
                        "content": json.dumps({"text": text}),
                        "msg_type": "text",
                    },
                    params={"receive_id_type": "open_id"},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception:
            logger.warning("auto_fix: failed to notify admin %s", admin_id[:10])


# ── 数据质量诊断报告（不自动修复，通知管理员审批）──

_diag_cooldown: float = 0
_DIAG_COOLDOWN_SECONDS = 300  # 同类诊断 5 分钟内只发一次


async def _generate_diagnostic_report(category: str) -> None:
    """生成数据质量诊断报告并通知管理员。不自动修复。

    报告内容：
    1. 触发告警的错误详情（工具名、参数、错误摘要）
    2. 相关源代码片段（自动从 traceback 提取）
    3. 诊断 ID（用于管理员审批触发修复）
    """
    global _diag_cooldown

    now = time.time()
    if now - _diag_cooldown < _DIAG_COOLDOWN_SECONDS:
        return
    _diag_cooldown = now

    from app.services.error_log import get_recent_errors

    errors = [e for e in get_recent_errors(10) if e.category == category]
    if not errors:
        return

    # 生成诊断 ID 并存入 Redis
    diag_id = f"diag_{int(now)}_{category}"
    report_lines = [
        f"[数据质量告警] {category}",
        f"诊断 ID: {diag_id}",
        f"时间: {errors[-1].time}",
        "",
    ]
    for e in errors[-3:]:  # 最多 3 条最新的同类错误
        report_lines.append(f"工具: {e.tool_name}")
        if e.tool_args:
            report_lines.append(f"参数: {e.tool_args}")
        report_lines.append(f"摘要: {e.summary}")
        if e.detail:
            report_lines.append(f"详情: {e.detail[:500]}")
        report_lines.append("")

    # 预加载相关代码
    source_ctx = _extract_source_context(errors[-3:])
    if source_ctx:
        report_lines.append("── 相关源代码 ──")
        report_lines.append(source_ctx[:1500])

    report_lines.extend([
        "",
        "── 操作 ──",
        f"审批修复: POST /admin/api/diagnostic/{diag_id}/fix",
        "忽略: 无需操作，5 分钟后可再次报告",
    ])

    report_text = "\n".join(report_lines)

    # 存入 Redis（24h TTL），供管理员审批
    try:
        from app.services import redis_client as redis
        if redis.available():
            redis.execute("SET", f"diagnostic:{diag_id}", json.dumps({
                "category": category,
                "report": report_text,
                "errors": [
                    {"tool_name": e.tool_name, "tool_args": e.tool_args,
                     "summary": e.summary, "detail": e.detail[:1000]}
                    for e in errors[-3:]
                ],
                "source_context": source_ctx[:2000] if source_ctx else "",
                "created_at": now,
            }, ensure_ascii=False), "EX", 86400)
            # 添加到待处理列表
            redis.execute("LPUSH", "diagnostic:pending", diag_id)
            redis.execute("LTRIM", "diagnostic:pending", 0, 49)  # 保留最近 50 条
    except Exception:
        logger.warning("auto_fix: failed to store diagnostic report in Redis")

    logger.info("auto_fix: diagnostic report generated: %s", diag_id)
    await _notify_admins(report_text)


async def admin_trigger_fix(diag_id: str) -> str:
    """管理员审批后触发修复。从 Redis 读取诊断报告，执行自修复流程。

    Returns: 修复结果文本
    """
    from app.services import redis_client as redis

    if not redis.available():
        return "Redis 不可用，无法读取诊断报告"

    raw = redis.execute("GET", f"diagnostic:{diag_id}")
    if not raw:
        return f"诊断报告 {diag_id} 不存在或已过期"

    try:
        diag = json.loads(raw)
    except json.JSONDecodeError:
        return "诊断报告格式错误"

    # 构造修复上下文
    error_text = diag.get("report", "")
    source_context = diag.get("source_context", "")

    error_context = (
        f"管理员审批了以下数据质量问题的修复请求：\n\n{error_text}\n\n"
    )
    if source_context:
        error_context += f"相关源代码：\n{source_context}\n\n"
    error_context += (
        "请诊断问题根因并修复。\n"
        "提示：如果是浏览器工具数据提取问题，先用 web_search 搜索目标网站的 DOM 结构或 API 方案。\n"
        "修复后用 self_validate_file 验证，然后 self_safe_deploy 部署。"
    )

    logger.info("auto_fix: admin-triggered fix for %s", diag_id)

    try:
        result = await _run_self_fix(error_context)
        # 清理已处理的诊断
        redis.execute("DEL", f"diagnostic:{diag_id}")
        redis.execute("LREM", "diagnostic:pending", 0, diag_id)
        await _notify_admins(f"[管理员审批修复完成] {diag_id}\n\n{result}")
        return result
    except Exception as exc:
        logger.exception("auto_fix: admin-triggered fix failed for %s", diag_id)
        return f"修复失败: {exc}"


# ── 错误智能分类（LLM triage）──
#
# 对 tool_error / api_error / timeout 类别的错误，用轻量 LLM 判断是否属于
# bot 自身代码/配置 bug。如果是 → 触发自修复；如果是用户传参或外部故障 → 跳过。
# 这样不需要穷举错误模式，LLM 本身就是万能分类器。

_last_classify_time: float = 0
_CLASSIFY_COOLDOWN = 300  # 分类冷却：5 分钟内不重复调用 LLM

_CLASSIFY_SYSTEM = (
    "你是错误分类器。判断 bot 运行错误是否属于 bot 自身代码/配置的 bug"
    "（可通过修改源代码修复）。只回复 YES 或 NO。\n"
    "注意：每条错误会附带工具名和调用参数，请结合参数判断。"
    "例如时间范围过大导致 API 报错，这是代码该做分片/校验的 bug。"
)

_CLASSIFY_USER = """\
以下是 bot 运行时遇到的错误。判断其中是否有 bot 自身代码/配置的 bug。

可修复的例子：
- OAuth scope 名写错、API 路径过时、参数格式与文档不符
- 缺少错误处理、逻辑 bug
- 传给 API 的参数超出限制（如时间范围过大、分页数超限）——代码应该加校验/分片
- 同一工具反复报同样的错——说明代码有系统性问题

不可修复的例子：
- LLM 一次性传了格式不对的参数（如日期写错）且不是代码逻辑导致
- 外部 API 临时 500/限流、网络超时（偶发，非系统性）
- browser_open / web_search / xhs_search / 企微发送出现偶发 timeout、login wall、会话窗口过期
- 用户权限不足（99991679）— 需要用户重新授权，不是代码 bug
- 错误消息中包含「不需要自我修复」「这不是代码bug」的提示 — 直接 NO

错误信息：
{error_text}

是否有可通过修改 bot 代码修复的？只回复 YES 或 NO。"""


@dataclass(frozen=True)
class TransientClassification:
    is_all_transient: bool
    reasons: str
    matched_count: int


_TRANSIENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("browser_open", re.compile(r"Page\.goto: Timeout|Timeout \d+ms exceeded", re.I)),
    ("httpx", re.compile(r"httpx\\.ConnectTimeout|httpcore\\.ConnectTimeout|ConnectTimeout", re.I)),
    ("remote_protocol", re.compile(r"RemoteProtocolError|Server disconnected without sending a response|ProxyError", re.I)),
    ("web_search", re.compile(r"web_search 连续失败|auto-lesson for web_search|web_search.*(timeout|timed out|failed|失败)", re.I)),
    ("xhs_search", re.compile(r"xhs_search timed out|xhs_ops: search .* timed out|login wall|验证码|CAPTCHA", re.I)),
    ("search_social_media", re.compile(r"search_social_media.*timeout|social_media.*timeout", re.I)),
    ("wecom_kf", re.compile(r"95018|send msg session status invalid|95007|invalid msg token|95013|conversation end", re.I)),
    ("third_party_api", re.compile(r"\b(429|500|502|503|504)\b|rate limit|temporar", re.I)),
)


def _classify_transient_errors(errors: list) -> TransientClassification:
    """Deterministically identify non-fixable transient/platform failures."""
    if not errors:
        return TransientClassification(False, "", 0)

    reasons: list[str] = []
    matched = 0
    for e in errors:
        blob = "\n".join([
            str(getattr(e, "category", "")),
            str(getattr(e, "tool_name", "")),
            str(getattr(e, "summary", "")),
            str(getattr(e, "detail", "")),
        ])
        hit = ""
        for label, pattern in _TRANSIENT_PATTERNS:
            if pattern.search(blob):
                hit = label
                break
        if hit:
            matched += 1
            reasons.append(hit)
        else:
            reasons.append(f"unmatched:{getattr(e, 'tool_name', '') or getattr(e, 'category', '')}")

    return TransientClassification(
        is_all_transient=matched == len(errors),
        reasons=", ".join(dict.fromkeys(reasons)),
        matched_count=matched,
    )


async def _classify_errors_fixable(errors: list) -> bool:
    """用轻量 LLM 判断 tool_error/api_error 是否包含可修复的代码 bug。

    fail-closed: 分类失败或超时 → 返回 False（不触发修复，保守策略）。
    """
    global _last_classify_time

    transient = _classify_transient_errors(errors)
    if transient.is_all_transient:
        logger.info(
            "auto_fix classify: %d errors are transient/platform failures (%s), skipping",
            len(errors), transient.reasons,
        )
        return False

    now = time.time()
    if now - _last_classify_time < _CLASSIFY_COOLDOWN:
        logger.debug("auto_fix classify: cooldown active")
        return False
    _last_classify_time = now

    lines: list[str] = []
    for e in errors:
        lines.append(f"[{e.category}] {e.summary}")
        if e.tool_name:
            args_info = f"  参数: {e.tool_args}" if e.tool_args else ""
            lines.append(f"  工具: {e.tool_name}{args_info}")
        if e.detail:
            lines.append(e.detail[:300])
        lines.append("")

    error_text = "\n".join(lines)
    user_msg = _CLASSIFY_USER.format(error_text=error_text)

    provider = settings.selffix.provider.lower()
    try:
        if provider == "gemini":
            result = await _classify_gemini(user_msg)
        else:
            result = await _classify_openai(user_msg)

        is_fixable = "YES" in result.upper()
        logger.info(
            "auto_fix classify: %d errors → %s (raw: %s)",
            len(errors), "FIXABLE" if is_fixable else "not fixable",
            result.strip()[:20],
        )
        return is_fixable
    except Exception as exc:
        logger.warning("auto_fix classify failed: %s", exc)
        return False


async def _classify_gemini(user_msg: str) -> str:
    """用 Gemini 做轻量分类（复用 selffix 模型配置，确保走同一个代理）"""
    from google import genai
    from google.genai import types

    api_key = settings.selffix.api_key
    if not api_key:
        return "NO"

    http_options: dict = {"timeout": 30_000}
    if settings.selffix.base_url:
        http_options["base_url"] = settings.selffix.base_url
    elif os.getenv("GOOGLE_GEMINI_BASE_URL"):
        http_options["base_url"] = os.getenv("GOOGLE_GEMINI_BASE_URL")
    proxy_url = os.getenv("GEMINI_PROXY", "")
    if proxy_url:
        http_options["async_client_args"] = {"proxy": proxy_url}

    client = genai.Client(api_key=api_key, http_options=http_options)
    # 复用 selffix 模型（已确认能通过代理），只输出 YES/NO 几乎不费 token
    model = settings.selffix.model or "gemini-2.0-flash"

    response = await client.aio.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text=user_msg)])],
        config=types.GenerateContentConfig(
            system_instruction=_CLASSIFY_SYSTEM,
            temperature=0,
            max_output_tokens=10,
        ),
    )
    return response.text or "NO"


async def _classify_openai(user_msg: str) -> str:
    """用 OpenAI 兼容 API 做轻量分类"""
    api_key = settings.selffix.api_key or settings.kimi.api_key
    base_url = settings.selffix.base_url or settings.kimi.base_url
    model = settings.kimi.model

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    kwargs: dict = dict(
        model=model,
        messages=[
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=1 if _is_k2_model() else 0,
        max_tokens=10,
    )
    extra = _extra_body()
    if extra:
        kwargs["extra_body"] = extra
    resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or "NO"


# ── 触发入口 ──


def maybe_trigger_fix(error_category: str = "") -> None:
    """由 error_log.record_error 回调调用。检查防护条件后调度去抖修复。"""
    global _debounce_handle, _hourly_fix_count, _hourly_reset_time

    # 基本前置条件
    if not settings.github.token or not settings.self_repo_owner:
        return
    if error_category in _SKIP_CATEGORIES:
        return

    # 数据质量告警：不自动修复，走诊断报告路径
    if error_category in _DIAGNOSTIC_ONLY_CATEGORIES:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_generate_diagnostic_report(error_category))
        except RuntimeError:
            pass
        return

    # 多租户安全：仅允许开启自我迭代的租户（平台管理员）触发修复
    try:
        from app.tenant.context import get_current_tenant
        tenant = get_current_tenant()
        if not tenant.self_iteration_enabled:
            logger.debug("auto_fix: skipped for tenant %s (self_iteration disabled)", tenant.tenant_id)
            return
    except Exception:
        # 无法确定租户上下文，安全起见不触发修复
        logger.warning("auto_fix: skipped (tenant context unavailable)")
        return
    # 服务别的 repo 时：确定的 bug 类别直接通过，待分类的让 _debounced_fix 处理
    if _is_serving_other_repo() and error_category not in _CODE_BUG_CATEGORIES and error_category not in _CLASSIFY_CATEGORIES:
        logger.debug("auto_fix: skip non-framework error '%s' (serving other repo)", error_category)
        return
    if _fix_lock.locked():
        return

    # 每小时上限
    now = time.time()
    if now > _hourly_reset_time:
        _hourly_fix_count = 0
        _hourly_reset_time = now + 3600
    if _hourly_fix_count >= _MAX_FIXES_PER_HOUR:
        logger.warning("auto_fix: hourly limit reached (%d/%d)", _hourly_fix_count, _MAX_FIXES_PER_HOUR)
        return

    # 冷却期
    if now - _last_fix_time < _COOLDOWN_SECONDS:
        remaining = int(_COOLDOWN_SECONDS - (now - _last_fix_time))
        logger.info("auto_fix: cooldown active (%ds remaining)", remaining)
        return

    # 取消前一个去抖计时器
    if _debounce_handle and not _debounce_handle.done():
        _debounce_handle.cancel()

    try:
        loop = asyncio.get_running_loop()
        _debounce_handle = loop.create_task(_debounced_fix())
    except RuntimeError:
        # 没有运行中的事件循环（不应该发生，但安全起见）
        logger.debug("auto_fix: no running event loop, skip")


async def _debounced_fix() -> None:
    """去抖等待结束后，执行自我修复流程。

    使用 asyncio.Lock 保证同一时间只有一个修复流程运行，
    消除 bool flag 在 await 间隙的 TOCTOU 竞争。
    """
    global _last_fix_time, _hourly_fix_count

    await asyncio.sleep(_DEBOUNCE_SECONDS)

    # 等待结束后再次检查：如果另一个修复正在进行，放弃
    if _fix_lock.locked():
        return

    # 用 Lock 保护整个修复流程（包括 LLM 分类 await），防止并发
    async with _fix_lock:
        from app.services.error_log import get_recent_errors, format_errors
        errors = get_recent_errors(10)

        # Phase 1: 确定需要修复的错误（unhandled / tool_exception / startup_check）
        definite = [e for e in errors if e.category not in _SKIP_CATEGORIES
                    and e.category not in _CLASSIFY_CATEGORIES]

        # Phase 2: 需要 LLM 分类的错误（tool_error / api_error / timeout）
        needs_triage = [e for e in errors if e.category in _CLASSIFY_CATEGORIES]

        # 多 repo 安全过滤（只对 definite 类别，triage 由分类器决定）
        if _is_serving_other_repo():
            definite = [e for e in definite if _is_framework_error(e)]

        # 决策逻辑：
        # - 有确定的 bug → 直接修（把 triage 错误也带上作为上下文）
        # - 只有 triage 错误 → 先用 LLM 分类，确认是代码 bug 才修
        if definite:
            relevant = definite + needs_triage
        elif needs_triage:
            is_fixable = await _classify_errors_fixable(needs_triage)
            if not is_fixable:
                logger.info(
                    "auto_fix: %d tool/api errors classified as non-fixable, skipping",
                    len(needs_triage),
                )
                return
            logger.info("auto_fix: %d errors classified as FIXABLE, proceeding",
                         len(needs_triage))
            relevant = needs_triage
        else:
            return

        if not relevant:
            return

        _last_fix_time = time.time()
        _hourly_fix_count += 1

        logger.info(
            "auto_fix: starting (attempt %d/%d this hour, %d errors to analyze, provider=%s)",
            _hourly_fix_count, _MAX_FIXES_PER_HOUR, len(relevant),
            settings.selffix.provider or "kimi",
        )

        try:
            error_text = format_errors(10)
            # 从 traceback 中提取相关源代码文件和行号，预加载给 LLM
            source_context = _extract_source_context(relevant)
            error_context = (
                f"bot 运行中检测到以下错误，请诊断并修复：\n\n{error_text}\n\n"
            )
            if source_context:
                error_context += f"以下是错误涉及的源代码片段（已自动提取）：\n\n{source_context}\n\n"
            error_context += (
                "请按修复流程操作：think 分析 → get_deploy_logs 查看完整日志 → 定位代码 → 修复 → self_validate_file 验证 → self_safe_deploy 部署。\n"
                "如果是外部临时故障（不是代码 bug），直接说明原因即可。\n"
                "如果涉及外部 API 行为不确定，请用 web_search 查阅官方文档。"
            )

            fix_result = await _run_self_fix(error_context)
            logger.info("auto_fix: completed — %s", fix_result[:200])

            await _notify_admins(fix_result)
        except Exception as exc:
            logger.exception("auto_fix: failed")
            from app.services.error_log import record_error
            record_error("self_fix_error", f"自我修复流程异常: {exc}", exc=exc)


# ── 启动健康检查 ──


async def startup_health_check() -> None:
    """启动时检查：1) 自动回滚坏的 self-fix  2) 检查日志触发修复。"""
    # ── 第一步：检查是否需要回滚上次坏的 self-fix ──
    try:
        from app.tools.self_ops import startup_rollback_check
        rollback_result = startup_rollback_check()
        if rollback_result:
            logger.warning("auto_fix: startup rollback triggered — %s", rollback_result[:200])
            await _notify_admins(f"[启动自动回滚]\n\n{rollback_result}")
            # 回滚后不再触发修复，等新部署生效
            return
    except Exception:
        logger.warning("auto_fix: startup rollback check failed", exc_info=True)

    # ── 第二步：检查本地日志文件是否有上次运行的错误 ──
    log_file = os.getenv("BOT_LOG_FILE", "/app/logs/bot.log")
    if os.path.exists(log_file):
        logger.info("auto_fix: checking local logs for previous errors...")
        try:
            # 只检查上次修复之后的日志，防止反复修已修过的问题
            from app.services import redis_client as redis
            last_fix_pos = 0
            if redis.available():
                try:
                    pos_str = redis.execute("GET", "autofix:last_log_pos")
                    if pos_str:
                        last_fix_pos = int(pos_str)
                except Exception:
                    pass

            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(max(0, last_fix_pos))
                logs = f.read()
                current_pos = f.tell()

            if not logs.strip():
                logger.info("auto_fix: no new logs since last check (pos=%d)", last_fix_pos)
                return

            # 只关注真正的代码错误，忽略 DeprecationWarning、已知权限问题等
            _REAL_ERROR_SIGNALS = ["Traceback (most recent call last)", "SyntaxError", "ImportError", "ModuleNotFoundError"]
            _FALSE_POSITIVE_PATTERNS = [
                "DeprecationWarning",
                "no dept authority error",    # 飞书通讯录权限不足（已知，非 bug）
                "auto_fix",                   # autofix 自身的日志不算
                "self_fix_error",
            ]

            # 过滤掉误报行
            log_lines = logs.split("\n")
            filtered = []
            for line in log_lines:
                if any(fp in line for fp in _FALSE_POSITIVE_PATTERNS):
                    continue
                filtered.append(line)
            filtered_logs = "\n".join(filtered)

            has_errors = any(sig in filtered_logs for sig in _REAL_ERROR_SIGNALS)

            if has_errors:
                logger.warning("auto_fix: old-log errors detected (pos %d→%d), notify only (bot started OK)",
                               last_fix_pos, current_pos)
                # 更新位置，防止下次重复扫描
                if redis.available():
                    try:
                        redis.execute("SET", "autofix:last_log_pos", str(current_pos), "EX", "86400")
                    except Exception:
                        pass
                # bot 已正常启动 → 旧日志错误不紧急，只通知管理员
                # 如果错误仍存在于代码中，会在运行时重新触发并走正常 autofix 流程
                # 不再自动部署，避免中断正在处理的用户请求
                snippet = filtered_logs[-800:]
                await _notify_admins(
                    f"[启动检查] 旧日志中发现错误（pos {last_fix_pos}→{current_pos}），"
                    f"但 bot 已正常启动，未触发自动修复。\n\n"
                    f"如需修复，请手动 /selffix 或等运行时再次触发。\n\n"
                    f"错误片段:\n{snippet}"
                )
            else:
                logger.info("auto_fix: startup check passed, no actionable errors in new logs")
                # 即使没错误也更新位置，避免下次重新扫描
                if redis.available():
                    try:
                        redis.execute("SET", "autofix:last_log_pos", str(current_pos), "EX", "86400")
                    except Exception:
                        pass
        except Exception:
            logger.warning("auto_fix: local log check failed", exc_info=True)
        return

    # ── 第三步：回退到 Railway 日志检查（向后兼容）──
    if settings.railway.api_token:
        logger.info("auto_fix: falling back to Railway log check...")
        try:
            from app.tools.railway_ops import get_deploy_logs
            from app.tools.tool_result import ToolResult
            result = get_deploy_logs(50)

            if isinstance(result, ToolResult):
                if not result.ok:
                    return
                logs = result.content
            else:
                logs = str(result)
                if logs.startswith("[ERROR]"):
                    return

            error_signals = ["Traceback", "Exception", "CRASHED", "FAILED", "SyntaxError", "ImportError"]
            if any(sig in logs for sig in error_signals):
                logger.warning("auto_fix: errors detected in Railway logs, scheduling fix")
                from app.services.error_log import record_error
                record_error("startup_check", "启动健康检查：日志中检测到错误", detail=logs[-1500:])
                await asyncio.sleep(5)
                maybe_trigger_fix("startup_check")
            else:
                logger.info("auto_fix: startup check passed, logs look clean")
        except Exception:
            logger.warning("auto_fix: Railway health check failed", exc_info=True)
    else:
        logger.info("auto_fix: no log file or Railway config, skip startup check")
