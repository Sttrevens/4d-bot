"""文件读写工具（通过 GitHub Contents API，无需本地 clone）

- read_file: 通过 API 读取仓库中的文件
- write_file: 通过 API 创建或更新文件（自动 commit）
"""

from __future__ import annotations

import base64
import logging

from app.tools.github_api import gh_get, gh_put
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# GitHub Contents API 文件大小限制（1MB）
_MAX_FILE_SIZE = 1_000_000


def read_file(path: str, branch: str = "main") -> ToolResult:
    """通过 GitHub API 读取文件内容"""
    data = gh_get(f"/contents/{path}", params={"ref": branch})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    # 如果是目录
    if isinstance(data, list):
        items = [f"{'dir' if f['type'] == 'dir' else 'file'}: {f['name']}" for f in data]
        return ToolResult.success("\n".join(items))

    file_size = data.get("size", 0)
    # 文件过大时提前警告
    if file_size > _MAX_FILE_SIZE:
        return ToolResult.error(
            f"文件 {path} 大小为 {file_size:,} 字节（{file_size // 1024}KB），"
            f"超过 GitHub API 限制（1MB），无法通过 API 读取完整内容。"
            f"建议：告诉用户需要在本地编辑此文件。",
            code="blocked",
        )

    content_b64 = data.get("content", "")
    try:
        content = base64.b64decode(content_b64).decode("utf-8")
    except Exception:
        return ToolResult.error("failed to decode file content (might be binary)")

    line_count = content.count("\n") + 1
    # 给模型一个文件大小提示
    if line_count > 300:
        header = f"[INFO] 文件 {path} 共 {line_count} 行（{file_size:,} 字节）。如需修改，建议只描述改动点，不要重写整个文件。\n\n"
        return ToolResult.success(header + content)

    return ToolResult.success(content)


def write_file(path: str, content: str, branch: str = "", message: str = "") -> ToolResult:
    """通过 GitHub API 创建或更新文件"""
    if not branch:
        branch = "main"
    if not message:
        message = f"bot: update {path}"

    content_bytes = content.encode("utf-8")
    if len(content_bytes) > _MAX_FILE_SIZE:
        return ToolResult.error(
            f"要写入的内容为 {len(content_bytes):,} 字节，"
            f"超过 GitHub API 限制（1MB）。"
            f"无法通过 API 写入此文件。请告诉用户需要在本地编辑。",
            code="blocked",
        )

    # 先检查文件是否已存在（需要 sha 来更新）
    existing = gh_get(f"/contents/{path}", params={"ref": branch})
    sha = None
    if isinstance(existing, dict) and "sha" in existing:
        sha = existing["sha"]

    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    result = gh_put(f"/contents/{path}", json=payload)
    if isinstance(result, str):
        return ToolResult.api_error(result)

    msg = f"wrote {len(content)} chars to {path} on branch {branch}"

    # ── 变更审查提醒：提取新增/修改的标识符，提醒模型搜索关联引用 ──
    if sha and isinstance(existing, dict):
        # 文件是更新（非新建），分析变更
        try:
            old_content = base64.b64decode(existing.get("content", "")).decode("utf-8")
            hints = _diff_review_hints(old_content, content, path)
            if hints:
                msg += "\n\n" + hints
        except Exception:
            pass  # diff 分析失败不影响写入结果

    return ToolResult.success(msg)


def _diff_review_hints(old: str, new: str, path: str) -> str:
    """分析新旧文件差异，提取可能需要搜索关联引用的标识符。"""
    import re

    old_lines = set(old.splitlines())
    new_lines = set(new.splitlines())
    added_lines = new_lines - old_lines

    if not added_lines:
        return ""

    # 提取新增行中的标识符（变量名、方法名等）
    # 匹配常见编程语言的标识符模式
    identifiers: set[str] = set()
    for line in added_lines:
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#") or line.startswith("*"):
            continue
        # 提取 GetComponent<XXX>、typeof(XXX) 等类型引用
        for m in re.finditer(r'GetComponent(?:InChildren)?<(\w+)>', line):
            identifiers.add(m.group(1))
        # 提取字段/变量引用（如 moveUIList、pushTaskUI）
        for m in re.finditer(r'\b([a-z][a-zA-Z0-9]{4,}(?:List\d*|Array|Map|Dict)?)\b', line):
            identifiers.add(m.group(1))

    # 过滤掉语言关键词
    _KEYWORDS = {
        "public", "private", "protected", "static", "void", "class", "struct",
        "return", "break", "continue", "false", "true", "null", "this",
        "string", "float", "double", "boolean", "override", "virtual",
        "async", "await", "const", "readonly", "foreach", "while",
        "import", "export", "function", "interface", "extends", "implements",
    }
    identifiers -= _KEYWORDS

    if not identifiers:
        return ""

    # 只保留在原文件中出现过的标识符（过滤掉新定义的局部变量）
    relevant = [name for name in identifiers if name in old]
    if not relevant:
        return ""

    names = ", ".join(sorted(relevant)[:8])
    return (
        f"⚠️ 变更审查提醒：你的修改涉及 {names}。\n"
        f"请用 search_code 搜索这些标识符，确认仓库中没有其他地方也需要同步修改。\n"
        f"特别注意：名字相似的变量（如 listA 和 listB）通常需要一起改。"
    )


TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "读取仓库中指定文件的内容。也可以传目录路径查看目录列表。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件相对路径，如 src/main.py",
                },
                "branch": {
                    "type": "string",
                    "description": "分支名，默认 main",
                    "default": "main",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "在仓库中创建或更新文件。会自动创建 commit。应在 feature 分支上操作，不要直接写 main。文件过大（>1MB）时会失败。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件相对路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文件内容",
                },
                "branch": {
                    "type": "string",
                    "description": "写入哪个分支，应使用 feature 分支",
                },
                "message": {
                    "type": "string",
                    "description": "commit message，留空自动生成",
                    "default": "",
                },
            },
            "required": ["path", "content", "branch"],
        },
    },
]

TOOL_MAP = {
    "read_file": lambda args: read_file(args["path"], args.get("branch", "main")),
    "write_file": lambda args: write_file(
        args["path"], args["content"], args.get("branch", ""), args.get("message", "")
    ),
}
