"""GitHub Skill 安装工具

从 GitHub 仓库安装自定义工具（skill）。
代码经过沙箱安全校验后存入 Redis，下次对话自动加载。

支持的 URL 格式:
- https://github.com/user/repo/blob/main/tools/my_tool.py
- https://raw.githubusercontent.com/user/repo/main/tools/my_tool.py
- github.com/user/repo (会列出可安装的工具)
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from app.services.redis_client import execute as redis_exec, pipeline as redis_pipeline
from app.tools.sandbox import compile_tool, validate_code
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# GitHub raw content URL 模式
_GITHUB_BLOB_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/blob/(?P<branch>[^/]+)/(?P<path>.+\.py)"
)
_GITHUB_RAW_RE = re.compile(
    r"raw\.githubusercontent\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<branch>[^/]+)/(?P<path>.+\.py)"
)
_GITHUB_REPO_RE = re.compile(
    r"(?:https?://)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+)/?$"
)

# Redis key helpers (复用 custom_tool_ops 的结构)
def _tool_key(tenant_id: str, tool_name: str) -> str:
    return f"custom_tools:{tenant_id}:{tool_name}"

def _index_key(tenant_id: str) -> str:
    return f"custom_tools:{tenant_id}:_index"


def _to_raw_url(url: str) -> str | None:
    """将 GitHub blob URL 转换为 raw URL。"""
    url = url.strip()
    # 已经是 raw URL
    m = _GITHUB_RAW_RE.search(url)
    if m:
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
    m = _GITHUB_REPO_RE.search(url)
    if m:
        return m.group("owner"), m.group("repo")
    m = _GITHUB_BLOB_RE.search(url)
    if m:
        return m.group("owner"), m.group("repo")
    m = _GITHUB_RAW_RE.search(url)
    if m:
        return m.group("owner"), m.group("repo")
    return None


def install_skill(tenant_id: str, url: str, risk_level: str = "yellow") -> ToolResult:
    """从 GitHub URL 安装 skill（自定义工具）。

    流程: 下载代码 → 安全校验 → 编译验证 → 存入 Redis
    """
    if not tenant_id:
        return ToolResult.invalid_param("缺少 tenant_id")
    if not url:
        return ToolResult.invalid_param("请提供 GitHub URL")

    # 检查是否是仓库 URL（没有具体文件路径）
    if _GITHUB_REPO_RE.search(url):
        return _browse_repo(url)

    # 转换为 raw URL
    raw_url = _to_raw_url(url)
    if not raw_url:
        return ToolResult.invalid_param(
            "URL 格式不支持。请提供以下格式之一:\n"
            "- https://github.com/user/repo/blob/main/path/tool.py\n"
            "- https://raw.githubusercontent.com/user/repo/main/path/tool.py"
        )

    # 1. 下载代码
    try:
        resp = httpx.get(raw_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        code = resp.text
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return ToolResult.not_found(f"文件不存在: {raw_url}")
        return ToolResult.api_error(f"下载失败 (HTTP {e.response.status_code}): {raw_url}")
    except Exception as e:
        return ToolResult.api_error(f"下载失败: {e}")

    if not code.strip():
        return ToolResult.error("下载的文件为空")

    # 文件太大拒绝
    if len(code) > 50000:
        return ToolResult.error(f"代码文件太大（{len(code)}字符），最大 50000")

    # 2. 安全校验
    violations = validate_code(code)
    if violations:
        return ToolResult.blocked(
            f"代码安全检查未通过（使用了不允许的模块或函数）:\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n\n允许的模块: json, re, time, datetime, math, hashlib, httpx, bs4 等"
        )

    # 3. 编译验证
    module_dict, errors = compile_tool(code)
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

    # 4. 安装每个工具
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

        # 写入 Redis
        now = str(int(time.time()))
        schema_json = json.dumps(schema, ensure_ascii=False)
        key = _tool_key(tenant_id, name)
        idx = _index_key(tenant_id)

        # 提取来源信息
        repo_info = _extract_repo_info(url)
        source = f"github:{repo_info[0]}/{repo_info[1]}" if repo_info else f"github:{url}"

        results = redis_pipeline([
            ["HSET", key, "name", name],
            ["HSET", key, "description", desc],
            ["HSET", key, "code", code],
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
        f"已安装 {len(installed)} 个 skill:\n{tools_list}\n\n"
        f"来源: {url}\n"
        f"风险级别: {risk_level}\n"
        f"下次对话中将自动可用。"
    )


def _browse_repo(url: str) -> ToolResult:
    """浏览 GitHub 仓库，列出可安装的 .py 文件。"""
    info = _extract_repo_info(url)
    if not info:
        return ToolResult.invalid_param("无法解析仓库 URL")

    owner, repo = info

    # 用 GitHub API 获取仓库文件树
    api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/main?recursive=1"
    try:
        resp = httpx.get(api_url, timeout=15, headers={"Accept": "application/vnd.github.v3+json"})
        if resp.status_code == 404:
            # 尝试 master 分支
            api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/master?recursive=1"
            resp = httpx.get(api_url, timeout=15, headers={"Accept": "application/vnd.github.v3+json"})
        resp.raise_for_status()
    except Exception as e:
        return ToolResult.api_error(f"获取仓库目录失败: {e}")

    tree = resp.json().get("tree", [])

    # 筛选 .py 文件（排除 __init__.py, setup.py, test 文件等）
    py_files = []
    for item in tree:
        path = item.get("path", "")
        if (
            item.get("type") == "blob"
            and path.endswith(".py")
            and "__init__" not in path
            and "setup.py" not in path
            and "test" not in path.lower()
            and "conftest" not in path
        ):
            py_files.append(path)

    if not py_files:
        return ToolResult.error(f"仓库 {owner}/{repo} 中没有找到可安装的 .py 文件")

    # 构建结果
    files_list = "\n".join(
        f"- `{p}` → 安装命令: install_github_skill(url=\"https://github.com/{owner}/{repo}/blob/main/{p}\")"
        for p in py_files[:20]
    )
    if len(py_files) > 20:
        files_list += f"\n  ... 还有 {len(py_files) - 20} 个文件"

    return ToolResult.success(
        f"仓库 **{owner}/{repo}** 中找到 {len(py_files)} 个 Python 文件:\n\n{files_list}\n\n"
        f"选择要安装的文件，我会下载并校验后安装。"
    )


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

    # 检查是否存在
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
            "从 GitHub 安装 skill（自定义工具）。"
            "传入 GitHub 文件 URL，自动下载代码、安全校验、安装。"
            "如果只传仓库 URL（不含文件路径），会列出可安装的工具文件。"
            "安装后下次对话自动可用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "GitHub URL。可以是文件 URL（直接安装）或仓库 URL（浏览可安装文件）。"
                        "例: https://github.com/user/repo/blob/main/tools/my_tool.py"
                    ),
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["green", "yellow", "red"],
                    "description": "风险级别: green=只读, yellow=写操作需确认, red=高风险需确认。默认 yellow。",
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
