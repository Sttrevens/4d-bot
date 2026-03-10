"""仓库搜索工具（通过 GitHub Search API）

类似 CC 的 Glob / Grep 能力：
- search_files: 按文件名搜索（找到 ConeDetection.cs 在哪）
- search_code: 按代码内容搜索（找到哪些文件包含某个类/方法）
- list_tree: 列出目录结构（浏览项目结构）
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

_GH_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"


def _headers() -> dict:
    from app.tools.github_api import _get_github_config
    hdrs: dict = {"Accept": "application/vnd.github.v3+json"}
    token, _, _ = _get_github_config()
    token = (token or "").strip()
    if token:
        hdrs["Authorization"] = f"token {token}"
    return hdrs


def _repo_slug() -> str:
    from app.tools.github_api import _get_github_config
    _, owner, name = _get_github_config()
    return f"{owner}/{name}"


def search_files(filename: str, path: str = "") -> ToolResult:
    """按文件名搜索仓库中的文件"""
    query = f"filename:{filename} repo:{_repo_slug()}"
    if path:
        query += f" path:{path}"

    try:
        with httpx.Client(timeout=_GH_TIMEOUT) as client:
            resp = client.get(
                f"{_API_BASE}/search/code",
                headers=_headers(),
                params={"q": query, "per_page": 20},
            )
        if resp.status_code >= 400:
            return ToolResult.api_error(f"{resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        items = data.get("items", [])
        if not items:
            return ToolResult.success(f"没有找到名为 '{filename}' 的文件。")

        total = data.get("total_count", len(items))
        lines = [f"找到 {total} 个匹配文件：\n"]
        for item in items:
            lines.append(f"  {item['path']}")
        return ToolResult.success("\n".join(lines))

    except Exception as exc:
        logger.exception("search_files failed")
        return ToolResult.error(str(exc), code="internal")


def search_code(query: str, extension: str = "", path: str = "") -> ToolResult:
    """按代码内容搜索仓库（类似 grep）"""
    q = f"{query} repo:{_repo_slug()}"
    if extension:
        q += f" extension:{extension}"
    if path:
        q += f" path:{path}"

    try:
        with httpx.Client(timeout=_GH_TIMEOUT) as client:
            resp = client.get(
                f"{_API_BASE}/search/code",
                headers={
                    **_headers(),
                    "Accept": "application/vnd.github.v3.text-match+json",
                },
                params={"q": q, "per_page": 15},
            )
        if resp.status_code >= 400:
            return ToolResult.api_error(f"{resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        items = data.get("items", [])
        if not items:
            return ToolResult.success(f"没有找到包含 '{query}' 的代码。")

        total = data.get("total_count", len(items))
        lines = [f"找到 {total} 处匹配（显示前 {len(items)} 个）：\n"]
        for item in items:
            lines.append(f"📄 {item['path']}")
            # 显示匹配的代码片段
            for match in item.get("text_matches", []):
                fragment = match.get("fragment", "").strip()
                if fragment:
                    # 只取前 200 字符避免太长
                    lines.append(f"   > {fragment[:200]}")
            lines.append("")
        return ToolResult.success("\n".join(lines))

    except Exception as exc:
        logger.exception("search_code failed")
        return ToolResult.error(str(exc), code="internal")


def list_tree(path: str = "", branch: str = "main") -> ToolResult:
    """列出仓库指定目录下的文件和子目录"""
    from app.tools.github_api import gh_get

    data = gh_get(f"/contents/{path}", params={"ref": branch})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if not isinstance(data, list):
        return ToolResult.success(f"'{path}' 不是目录。")

    dirs = []
    files = []
    for item in data:
        if item["type"] == "dir":
            dirs.append(f"📁 {item['name']}/")
        else:
            size = item.get("size", 0)
            if size > 1024:
                size_str = f" ({size // 1024}KB)"
            else:
                size_str = ""
            files.append(f"📄 {item['name']}{size_str}")

    dirs.sort()
    files.sort()
    result_lines = dirs + files
    if not result_lines:
        return ToolResult.success(f"目录 '{path}' 为空。")

    header = f"目录 '{path or '/'}' (branch: {branch}):\n"
    return ToolResult.success(header + "\n".join(result_lines))


TOOL_DEFINITIONS = [
    {
        "name": "search_files",
        "description": "按文件名搜索仓库。用于查找某个文件的实际路径，比如找到 ConeDetection.cs 在哪个目录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "要搜索的文件名（支持部分匹配），如 'ConeDetection.cs'",
                },
                "path": {
                    "type": "string",
                    "description": "限定搜索范围到某个目录，如 'Assets/Scripts'",
                    "default": "",
                },
            },
            "required": ["filename"],
        },
    },
    {
        "name": "search_code",
        "description": "按代码内容搜索仓库（类似 grep）。用于查找某个类、方法、变量在哪些文件中出现。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的代码内容，如 'class ConeDetection' 或 'GetTargetState'",
                },
                "extension": {
                    "type": "string",
                    "description": "限定文件类型，如 'cs'、'py'、'json'",
                    "default": "",
                },
                "path": {
                    "type": "string",
                    "description": "限定搜索范围到某个目录",
                    "default": "",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_tree",
        "description": "列出仓库中某个目录下的所有文件和子目录。用于浏览项目结构。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径，留空表示根目录，如 'Assets/Scripts'",
                    "default": "",
                },
                "branch": {
                    "type": "string",
                    "description": "分支名，默认 main",
                    "default": "main",
                },
            },
        },
    },
]

TOOL_MAP = {
    "search_files": lambda args: search_files(
        args["filename"], args.get("path", "")
    ),
    "search_code": lambda args: search_code(
        args["query"], args.get("extension", ""), args.get("path", "")
    ),
    "list_tree": lambda args: list_tree(
        args.get("path", ""), args.get("branch", "main")
    ),
}
