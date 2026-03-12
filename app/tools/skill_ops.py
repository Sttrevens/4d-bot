"""GitHub Skill 安装工具

从 GitHub 仓库安装自定义工具和知识模块。
支持两种格式：
- Python 工具（.py + TOOL_DEFINITIONS/TOOL_MAP）→ 编译校验后存入 Redis
- 知识模块（.md，如 SKILL.md）→ 存为 capability module，注入 system prompt

支持的 URL 格式:
- https://github.com/user/repo/blob/main/tools/my_tool.py
- https://github.com/user/repo/blob/main/skills/tdd/SKILL.md
- https://raw.githubusercontent.com/user/repo/main/path/file.py
- github.com/user/repo (会列出可安装的工具和知识模块)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import httpx

from app.services.redis_client import execute as redis_exec, pipeline as redis_pipeline
from app.tools.sandbox import compile_tool, validate_code
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── GitHub URL 模式 ──
# 支持任意文件扩展名（不再限制 .py）
_GITHUB_BLOB_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/blob/(?P<branch>[^/]+)/(?P<path>.+)"
)
_GITHUB_RAW_RE = re.compile(
    r"raw\.githubusercontent\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<branch>[^/]+)/(?P<path>.+)"
)
_GITHUB_REPO_RE = re.compile(
    r"(?:https?://)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+)/?$"
)

# 知识模块文件名模式（大小写不敏感）
_KNOWLEDGE_FILE_PATTERNS = re.compile(
    r"(?i)(skill|readme|guide|workflow|runbook|playbook|prompt|instructions?)\.md$"
)
# 要排除的 .md 文件（仓库根 README 等非技能文档）
_SKIP_MD_PATTERNS = re.compile(
    r"(?i)^readme\.md$|^contributing\.md$|^changelog\.md$|^license"
    r"|^\.github/|^docs/api|node_modules/"
)

# capability module 存储目录
_MODULES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "knowledge", "modules"
)

# Redis key helpers (复用 custom_tool_ops 的结构)
def _tool_key(tenant_id: str, tool_name: str) -> str:
    return f"custom_tools:{tenant_id}:{tool_name}"

def _index_key(tenant_id: str) -> str:
    return f"custom_tools:{tenant_id}:_index"


def _to_raw_url(url: str) -> str | None:
    """将 GitHub blob/raw URL 转换为 raw content URL。支持任意文件类型。"""
    url = url.strip()
    # 已经是 raw URL
    if _GITHUB_RAW_RE.search(url):
        return url
    # blob URL → raw URL
    m = _GITHUB_BLOB_RE.search(url)
    if m:
        return (
            f"https://raw.githubusercontent.com/{m.group('owner')}/"
            f"{m.group('repo')}/{m.group('branch')}/{m.group('path')}"
        )
    return None


def _extract_repo_info(url: str) -> tuple[str, str] | None:
    """从 URL 提取 (owner, repo)。"""
    for pat in (_GITHUB_REPO_RE, _GITHUB_BLOB_RE, _GITHUB_RAW_RE):
        m = pat.search(url)
        if m:
            return m.group("owner"), m.group("repo")
    return None


def _derive_module_name(path: str) -> str:
    """从文件路径推导 capability module 名称。

    策略：
    - skills/brainstorming/SKILL.md → brainstorming
    - guides/tdd-workflow.md → tdd-workflow
    - prompts/code_review.md → code_review
    - SKILL.md (根目录) → 用仓库名（调用方处理）
    """
    parts = path.replace("\\", "/").split("/")
    filename = parts[-1]
    stem = filename.rsplit(".", 1)[0]  # 去掉 .md

    # 如果文件名是通用名（SKILL, README 等），用父目录名
    if re.match(r"(?i)^(skill|readme|guide|instructions?)$", stem):
        if len(parts) >= 2:
            return parts[-2]  # 父目录名
    return stem


def _download_file(url: str) -> tuple[str | None, str | None]:
    """下载 GitHub 文件，返回 (content, error_message)。"""
    raw_url = _to_raw_url(url)
    if not raw_url:
        return None, (
            "URL 格式不支持。请提供以下格式之一:\n"
            "- https://github.com/user/repo/blob/main/path/file.py\n"
            "- https://github.com/user/repo/blob/main/path/SKILL.md\n"
            "- https://raw.githubusercontent.com/user/repo/main/path/file"
        )
    try:
        resp = httpx.get(raw_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        content = resp.text
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None, f"文件不存在: {raw_url}"
        return None, f"下载失败 (HTTP {e.response.status_code}): {raw_url}"
    except Exception as e:
        return None, f"下载失败: {e}"

    if not content.strip():
        return None, "下载的文件为空"
    return content, None


# ═══════════════════════════════════════════════════════
#  核心安装逻辑
# ═══════════════════════════════════════════════════════

def install_skill(tenant_id: str, url: str, risk_level: str = "yellow") -> ToolResult:
    """从 GitHub URL 安装 skill。

    自动检测文件类型：
    - .py → Python 工具（编译校验 → Redis）
    - .md → 知识模块（存为 capability module）
    - 仓库 URL → 列出可安装的文件
    """
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")
    if not url:
        return ToolResult.invalid_param("请提供 GitHub URL")

    # 仓库 URL（没有具体文件路径）→ 浏览仓库
    if _GITHUB_REPO_RE.search(url):
        return _browse_repo(url)

    # 检测文件类型
    url_lower = url.lower()
    if url_lower.endswith(".md"):
        return _install_as_module(url)
    elif url_lower.endswith(".py"):
        return _install_as_tool(tenant_id, url, risk_level)
    else:
        # 尝试作为 .md 或 .py 处理
        # 先下载看内容
        content, err = _download_file(url)
        if err:
            return ToolResult.error(err)
        # 如果看起来像 Python 代码
        if "TOOL_DEFINITIONS" in content or "TOOL_MAP" in content:
            return _install_as_tool(tenant_id, url, risk_level)
        # 如果看起来像 Markdown
        if content.lstrip().startswith("#") or content.lstrip().startswith("---"):
            return _install_as_module(url, prefetched_content=content)
        return ToolResult.invalid_param(
            f"无法识别文件类型。支持 .py（Python 工具）和 .md（知识模块）。\n"
            f"URL: {url}"
        )


def _install_as_tool(tenant_id: str, url: str, risk_level: str = "yellow") -> ToolResult:
    """安装 Python 工具（.py 文件）。原有逻辑不变。"""
    content, err = _download_file(url)
    if err:
        return ToolResult.error(err)

    if len(content) > 50000:
        return ToolResult.error(f"代码文件太大（{len(content)}字符），最大 50000")

    # 安全校验
    violations = validate_code(content)
    if violations:
        return ToolResult.blocked(
            f"代码安全检查未通过（使用了不允许的模块或函数）:\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n\n允许的模块: json, re, time, datetime, math, hashlib, httpx, bs4 等"
        )

    # 编译验证
    module_dict, errors = compile_tool(content)
    if errors:
        return ToolResult.error(
            f"代码编译失败:\n" + "\n".join(f"- {e}" for e in errors)
        )

    tool_defs = module_dict.get("TOOL_DEFINITIONS", [])
    tool_map = module_dict.get("TOOL_MAP", {})

    if not tool_defs or not tool_map:
        return ToolResult.error(
            "代码中缺少 TOOL_DEFINITIONS 或 TOOL_MAP。\n"
            "Skill 文件必须导出这两个变量，格式参考项目内 app/tools/ 下的工具文件。"
        )

    # 安装每个工具
    installed: list[str] = []
    from app.tools.custom_tool_ops import _BUILTIN_TOOL_NAMES  # noqa

    for td in tool_defs:
        name = td.get("name", "")
        desc = td.get("description", "")
        schema = td.get("input_schema", {})

        if not name:
            continue
        if name in _BUILTIN_TOOL_NAMES:
            return ToolResult.blocked(f"'{name}' 是内置工具名，不能覆盖")
        if name not in tool_map:
            continue

        now = str(int(time.time()))
        schema_json = json.dumps(schema, ensure_ascii=False)
        key = _tool_key(tenant_id, name)
        idx = _index_key(tenant_id)

        repo_info = _extract_repo_info(url)
        source = f"github:{repo_info[0]}/{repo_info[1]}" if repo_info else f"github:{url}"

        results = redis_pipeline([
            ["HSET", key, "name", name],
            ["HSET", key, "description", desc],
            ["HSET", key, "code", content],
            ["HSET", key, "input_schema", schema_json],
            ["HSET", key, "risk_level", risk_level],
            ["HSET", key, "created_at", now],
            ["HSET", key, "updated_at", now],
            ["HSET", key, "version", "1"],
            ["HSET", key, "source", source],
            ["SADD", idx, name],
        ])

        if any(r is None for r in results):
            return ToolResult.api_error(f"Redis 写入 '{name}' 失败")

        installed.append(f"- **{name}**: {desc}")

    if not installed:
        return ToolResult.error("代码中没有找到有效的工具定义")

    tools_list = "\n".join(installed)
    return ToolResult.success(
        f"✅ 已安装 {len(installed)} 个工具 skill:\n{tools_list}\n\n"
        f"来源: {url}\n"
        f"风险级别: {risk_level}\n"
        f"下次对话中将自动可用。"
    )


def _install_as_module(url: str, *, prefetched_content: str | None = None) -> ToolResult:
    """安装知识模块（.md 文件）。

    将 Markdown 内容存为 capability module（app/knowledge/modules/），
    供 system prompt 注入或 load_capability_module 按需加载。
    """
    if prefetched_content:
        content = prefetched_content
    else:
        content, err = _download_file(url)
        if err:
            return ToolResult.error(err)

    if len(content) > 30000:
        return ToolResult.error(
            f"文件太大（{len(content)} 字符），最大 30000。\n"
            "知识模块应精简为核心工作流和关键指令。"
        )

    # 从 URL 推导模块名
    m = _GITHUB_BLOB_RE.search(url) or _GITHUB_RAW_RE.search(url)
    if m:
        path = m.group("path")
        module_name = _derive_module_name(path)
    else:
        module_name = "imported_skill"

    # 清理模块名（只保留字母数字下划线横杠）
    module_name = re.sub(r"[^a-zA-Z0-9_-]", "_", module_name).strip("_")
    if not module_name:
        module_name = "imported_skill"

    # 写入 Redis per-tenant capability module
    from app.tenant.context import get_current_tenant
    tenant = get_current_tenant()
    tenant_id = tenant.tenant_id
    redis_key = f"modules:{tenant_id}:{module_name}"
    _MODULE_TTL = 365 * 86400  # 1 year

    is_update = redis_exec("EXISTS", redis_key) == 1
    result_set = redis_exec("SET", redis_key, content, "EX", str(_MODULE_TTL))
    if result_set != "OK":
        logger.error("save module to redis failed: key=%s", redis_key)
        return ToolResult.error("保存模块到 Redis 失败")

    repo_info = _extract_repo_info(url)
    source_str = f"github:{repo_info[0]}/{repo_info[1]}" if repo_info else url
    action = "更新" if is_update else "安装"

    # 提取描述（取 markdown 第一个标题或前 100 字符）
    desc_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    description = desc_match.group(1).strip() if desc_match else content[:100].strip()

    return ToolResult.success(
        f"✅ 已{action}知识模块 **{module_name}**（{len(content)} 字符）\n\n"
        f"描述: {description}\n"
        f"来源: {source_str}\n"
        f"存储: Redis modules:{tenant_id}:{module_name}\n\n"
        f"使用方式：\n"
        f"- 对话中调用 `load_capability_module(\"{module_name}\")` 按需加载\n"
        f"- 或在 tenants.json 的 `capability_modules` 中添加 `\"{module_name}\"` 自动注入 system prompt"
    )


# ═══════════════════════════════════════════════════════
#  仓库浏览
# ═══════════════════════════════════════════════════════

def _browse_repo(url: str) -> ToolResult:
    """浏览 GitHub 仓库，列出可安装的工具（.py）和知识模块（.md）。"""
    info = _extract_repo_info(url)
    if not info:
        return ToolResult.invalid_param("无法解析仓库 URL")

    owner, repo = info

    # 用 GitHub API 获取仓库文件树
    api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/main?recursive=1"
    try:
        resp = httpx.get(api_url, timeout=15, headers={"Accept": "application/vnd.github.v3+json"})
        if resp.status_code == 404:
            api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/master?recursive=1"
            resp = httpx.get(api_url, timeout=15, headers={"Accept": "application/vnd.github.v3+json"})
        resp.raise_for_status()
    except Exception as e:
        return ToolResult.api_error(f"获取仓库目录失败: {e}")

    tree = resp.json().get("tree", [])

    # 分类：Python 工具 和 知识模块
    py_files: list[str] = []
    md_files: list[str] = []

    for item in tree:
        path = item.get("path", "")
        if item.get("type") != "blob":
            continue

        # Python 工具文件
        if (
            path.endswith(".py")
            and "__init__" not in path
            and "setup.py" not in path
            and "test" not in path.lower()
            and "conftest" not in path
        ):
            py_files.append(path)

        # 知识模块文件
        elif (
            path.endswith(".md")
            and not _SKIP_MD_PATTERNS.search(path)
            and (
                _KNOWLEDGE_FILE_PATTERNS.search(path)
                or "/skills/" in path.lower()
                or "/prompts/" in path.lower()
                or "/guides/" in path.lower()
            )
        ):
            md_files.append(path)

    if not py_files and not md_files:
        return ToolResult.error(
            f"仓库 {owner}/{repo} 中没有找到可安装的文件。\n"
            f"支持的格式：.py（Python 工具）和 .md（知识模块，如 SKILL.md）"
        )

    # 检测分支名（从 API URL 推断）
    branch = "main" if "main" in api_url else "master"

    parts: list[str] = [f"仓库 **{owner}/{repo}** 中找到的可安装内容：\n"]

    if md_files:
        parts.append(f"### 📚 知识模块（{len(md_files)} 个 .md 文件）\n")
        for p in md_files[:15]:
            module_name = _derive_module_name(p)
            parts.append(
                f"- `{p}` → 模块名: **{module_name}**\n"
                f"  安装: `install_github_skill(url=\"https://github.com/{owner}/{repo}/blob/{branch}/{p}\")`"
            )
        if len(md_files) > 15:
            parts.append(f"  ... 还有 {len(md_files) - 15} 个模块")
        parts.append("")

    if py_files:
        parts.append(f"### 🔧 Python 工具（{len(py_files)} 个 .py 文件）\n")
        for p in py_files[:15]:
            parts.append(
                f"- `{p}`\n"
                f"  安装: `install_github_skill(url=\"https://github.com/{owner}/{repo}/blob/{branch}/{p}\")`"
            )
        if len(py_files) > 15:
            parts.append(f"  ... 还有 {len(py_files) - 15} 个文件")
        parts.append("")

    parts.append("选择要安装的文件，我会自动检测格式并安装。")

    return ToolResult.success("\n".join(parts))


def list_installed_skills(tenant_id: str) -> ToolResult:
    """列出当前租户已安装的所有 skill（含来源信息）。"""
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")

    idx = _index_key(tenant_id)
    names = redis_exec("SMEMBERS", idx)
    if not names:
        return ToolResult.success("当前没有安装任何 skill。")

    skills: list[str] = []
    for name in sorted(names):
        key = _tool_key(tenant_id, name)
        desc = redis_exec("HGET", key, "description") or ""
        source = redis_exec("HGET", key, "source") or "inline"
        risk = redis_exec("HGET", key, "risk_level") or "green"
        version = redis_exec("HGET", key, "version") or "1"
        skills.append(f"- **{name}** v{version} [{risk}] — {desc}\n  来源: {source}")

    return ToolResult.success(
        f"已安装 {len(skills)} 个 skill:\n\n" + "\n".join(skills)
    )


def uninstall_skill(tenant_id: str, name: str) -> ToolResult:
    """卸载指定 skill。"""
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")
    if not name:
        return ToolResult.invalid_param("请指定要卸载的 skill 名称")

    key = _tool_key(tenant_id, name)
    idx = _index_key(tenant_id)

    exists = redis_exec("EXISTS", key)
    if not exists:
        return ToolResult.not_found(f"未找到 skill '{name}'")

    redis_pipeline([
        ["DEL", key],
        ["SREM", idx, name],
    ])

    return ToolResult.success(f"已卸载 skill '{name}'，下次对话生效。")


# ═══════════════════════════════════════════════════════
#  Tool definitions & map
# ═══════════════════════════════════════════════════════

TOOL_DEFINITIONS = [
    {
        "name": "install_github_skill",
        "description": (
            "从 GitHub 安装 skill（自定义工具或知识模块）。"
            "支持两种格式：\n"
            "- .py 文件（Python 工具）：自动下载、安全校验、安装为可调用工具\n"
            "- .md 文件（知识模块，如 SKILL.md）：安装为 capability module，可通过 load_capability_module 加载\n"
            "如果只传仓库 URL（不含文件路径），会列出可安装的工具和知识模块。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "GitHub URL。可以是文件 URL（直接安装）或仓库 URL（浏览可安装文件）。\n"
                        "例: https://github.com/user/repo/blob/main/tools/my_tool.py\n"
                        "例: https://github.com/user/repo/blob/main/skills/tdd/SKILL.md\n"
                        "例: https://github.com/user/repo（浏览仓库）"
                    ),
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["green", "yellow", "red"],
                    "description": "风险级别（仅对 .py 工具有效）: green=只读, yellow=写操作需确认, red=高风险需确认。默认 yellow。",
                    "default": "yellow",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "list_installed_skills",
        "description": "列出当前已安装的所有 skill（自定义工具），包括来源、版本、风险级别。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "uninstall_skill",
        "description": "卸载指定的 skill（自定义工具）。卸载后下次对话生效。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要卸载的 skill 名称",
                },
            },
            "required": ["name"],
        },
    },
]

TOOL_MAP = {
    "install_github_skill": lambda args: install_skill(
        tenant_id=args.get("tenant_id", ""),
        url=args["url"],
        risk_level=args.get("risk_level", "yellow"),
    ),
    "list_installed_skills": lambda args: list_installed_skills(
        tenant_id=args.get("tenant_id", ""),
    ),
    "uninstall_skill": lambda args: uninstall_skill(
        tenant_id=args.get("tenant_id", ""),
        name=args["name"],
    ),
}
