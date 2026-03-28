"""文件读写工具（通过 GitHub Contents API + Git Trees API）

- read_file: 读取仓库中的文件
- edit_file: 精确替换文件中的文本片段（不用重写整个文件）
- write_file: 创建或完整覆写文件（保留向后兼容）
- commit_batch: 原子提交多个文件变更（一次 commit 改多个文件）
"""

from __future__ import annotations

import base64
import logging
import re

from app.tools.github_api import gh_get, gh_put, gh_post
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE = 1_000_000  # GitHub Contents API 1MB limit


# ── read_file ──

def read_file(path: str, branch: str = "main") -> ToolResult:
    """通过 GitHub API 读取文件内容"""
    data = gh_get(f"/contents/{path}", params={"ref": branch})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if isinstance(data, list):
        items = [f"{'dir' if f['type'] == 'dir' else 'file'}: {f['name']}" for f in data]
        return ToolResult.success("\n".join(items))

    file_size = data.get("size", 0)
    if file_size > _MAX_FILE_SIZE:
        return ToolResult.error(
            f"文件 {path} 大小 {file_size // 1024}KB，超过 GitHub API 限制（1MB）。需要在本地编辑。",
            code="blocked",
        )

    content_b64 = data.get("content", "")
    try:
        content = base64.b64decode(content_b64).decode("utf-8")
    except Exception:
        return ToolResult.error("failed to decode file content (might be binary)")

    line_count = content.count("\n") + 1
    if line_count > 300:
        header = f"[INFO] {path}: {line_count} 行, {file_size:,} 字节。修改时用 edit_file 做精确替换，不要用 write_file 重写整个文件。\n\n"
        return ToolResult.success(header + content)

    return ToolResult.success(content)


# ── edit_file（精确替换，不用重写整个文件） ──

def edit_file(path: str, old_string: str, new_string: str, branch: str = "", message: str = "") -> ToolResult:
    """在文件中精确替换一段文本。比 write_file 高效：只需提供要替换的片段。"""
    if not branch:
        branch = "main"
    if not old_string:
        return ToolResult.invalid_param("old_string 不能为空")
    if old_string == new_string:
        return ToolResult.invalid_param("old_string 和 new_string 相同，没有变更")

    # 读取当前文件
    data = gh_get(f"/contents/{path}", params={"ref": branch})
    if isinstance(data, str):
        return ToolResult.api_error(f"读取失败: {data}")
    if isinstance(data, list):
        return ToolResult.error(f"{path} 是目录不是文件")

    sha = data.get("sha", "")
    try:
        content = base64.b64decode(data.get("content", "")).decode("utf-8")
    except Exception:
        return ToolResult.error("无法解码文件内容（可能是二进制文件）")

    # 检查 old_string 是否存在且唯一
    count = content.count(old_string)
    if count == 0:
        # 给出上下文帮助定位
        lines = content.splitlines()
        preview = "\n".join(lines[:20]) if len(lines) > 20 else content[:500]
        return ToolResult.error(
            f"在 {path} 中找不到要替换的文本。文件开头预览:\n{preview}",
            retry_hint="确认 old_string 与文件中的文本完全一致（包括空格和缩进）",
        )
    if count > 1:
        return ToolResult.error(
            f"old_string 在文件中出现 {count} 次，无法确定替换哪一个。请提供更长的上下文使其唯一。",
            retry_hint="在 old_string 前后多包含几行代码",
        )

    # 执行替换
    new_content = content.replace(old_string, new_string, 1)
    if not message:
        message = f"bot: edit {path}"

    payload = {
        "message": message,
        "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": branch,
        "sha": sha,
    }
    result = gh_put(f"/contents/{path}", json=payload)
    if isinstance(result, str):
        return ToolResult.api_error(result)

    # 变更审查：检查是否有关联引用需要同步修改
    hints = _diff_review_hints(content, new_content, path)
    msg = f"已替换 {path} (branch: {branch})"
    if hints:
        msg += "\n\n" + hints
    return ToolResult.success(msg)


# ── write_file（完整覆写，保留兼容） ──

def write_file(path: str, content: str, branch: str = "", message: str = "") -> ToolResult:
    """通过 GitHub API 创建或覆写文件。大文件修改建议用 edit_file 代替。"""
    if not branch:
        branch = "main"
    if not message:
        message = f"bot: update {path}"

    content_bytes = content.encode("utf-8")
    if len(content_bytes) > _MAX_FILE_SIZE:
        return ToolResult.error(f"内容 {len(content_bytes) // 1024}KB 超过 1MB 限制", code="blocked")

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
    if sha and isinstance(existing, dict):
        try:
            old_content = base64.b64decode(existing.get("content", "")).decode("utf-8")
            hints = _diff_review_hints(old_content, content, path)
            if hints:
                msg += "\n\n" + hints
        except Exception:
            pass
    return ToolResult.success(msg)


# ── commit_batch（原子多文件提交） ──

def commit_batch(changes: list[dict], branch: str = "", message: str = "") -> ToolResult:
    """用 Git Trees API 原子提交多个文件变更。一次 commit 改多个文件。

    changes: [{"path": "src/foo.cs", "content": "..."}, ...]
    """
    if not changes:
        return ToolResult.invalid_param("changes 不能为空")
    if not branch:
        branch = "main"
    if not message:
        paths = [c["path"] for c in changes[:3]]
        message = f"bot: batch update {', '.join(paths)}" + (" ..." if len(changes) > 3 else "")

    # 1. 获取 branch 最新 commit SHA
    ref_data = gh_get(f"/git/ref/heads/{branch}")
    if isinstance(ref_data, str):
        return ToolResult.api_error(f"获取分支失败: {ref_data}")
    base_commit_sha = ref_data["object"]["sha"]

    # 2. 获取 base tree SHA
    commit_data = gh_get(f"/git/commits/{base_commit_sha}")
    if isinstance(commit_data, str):
        return ToolResult.api_error(f"获取 commit 失败: {commit_data}")
    base_tree_sha = commit_data["tree"]["sha"]

    # 3. 构建 tree 对象
    tree_items = []
    for change in changes:
        path = change.get("path", "")
        content = change.get("content", "")
        if not path:
            continue
        tree_items.append({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "content": content,
        })

    tree_result = gh_post("/git/trees", json={"base_tree": base_tree_sha, "tree": tree_items})
    if isinstance(tree_result, str):
        return ToolResult.api_error(f"创建 tree 失败: {tree_result}")
    new_tree_sha = tree_result["sha"]

    # 4. 创建 commit
    commit_result = gh_post("/git/commits", json={
        "message": message,
        "tree": new_tree_sha,
        "parents": [base_commit_sha],
    })
    if isinstance(commit_result, str):
        return ToolResult.api_error(f"创建 commit 失败: {commit_result}")
    new_commit_sha = commit_result["sha"]

    # 5. 更新 ref
    from app.tools.github_api import gh_patch
    ref_result = gh_patch(f"/git/refs/heads/{branch}", json={"sha": new_commit_sha})
    if isinstance(ref_result, str):
        return ToolResult.api_error(f"更新分支失败: {ref_result}")

    return ToolResult.success(
        f"原子提交成功: {len(changes)} 个文件 → {branch}\n"
        f"commit: {new_commit_sha[:10]}\n"
        f"文件: {', '.join(c['path'] for c in changes)}"
    )


# ── 变更审查提醒 ──

def _diff_review_hints(old: str, new: str, path: str) -> str:
    """分析差异，提取可能需要搜索关联引用的标识符。"""
    old_lines = set(old.splitlines())
    new_lines = set(new.splitlines())
    added_lines = new_lines - old_lines

    if not added_lines:
        return ""

    identifiers: set[str] = set()
    for line in added_lines:
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#") or line.startswith("*"):
            continue
        for m in re.finditer(r'GetComponent(?:InChildren)?<(\w+)>', line):
            identifiers.add(m.group(1))
        for m in re.finditer(r'\b([a-z][a-zA-Z0-9]{4,}(?:List\d*|Array|Map|Dict)?)\b', line):
            identifiers.add(m.group(1))

    _KEYWORDS = {
        "public", "private", "protected", "static", "void", "class", "struct",
        "return", "break", "continue", "false", "true", "null", "this",
        "string", "float", "double", "boolean", "override", "virtual",
        "async", "await", "const", "readonly", "foreach", "while",
        "import", "export", "function", "interface", "extends", "implements",
    }
    identifiers -= _KEYWORDS

    relevant = [name for name in identifiers if name in old]
    if not relevant:
        return ""

    names = ", ".join(sorted(relevant)[:8])
    return (
        f"⚠️ 变更涉及 {names}。"
        f"用 search_code 搜索这些标识符，确认没有其他地方需要同步修改。"
    )


# ── 工具定义 ──

TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "读取仓库中指定文件的内容。也可以传目录路径查看目录列表。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件相对路径，如 Assets/Scripts/Foo.cs"},
                "branch": {"type": "string", "description": "分支名，默认 main", "default": "main"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "精确替换文件中的一段文本。比 write_file 高效——只需提供要替换的旧文本和新文本，"
            "不用输出整个文件。old_string 必须在文件中唯一匹配。修改大文件时优先用这个。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件相对路径"},
                "old_string": {"type": "string", "description": "要替换的原始文本（必须在文件中唯一出现）"},
                "new_string": {"type": "string", "description": "替换后的新文本"},
                "branch": {"type": "string", "description": "分支名，默认 main"},
                "message": {"type": "string", "description": "commit message，留空自动生成"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "write_file",
        "description": "创建新文件或完整覆写文件内容。修改已有文件时优先用 edit_file 代替。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件相对路径"},
                "content": {"type": "string", "description": "完整文件内容"},
                "branch": {"type": "string", "description": "分支名"},
                "message": {"type": "string", "description": "commit message"},
            },
            "required": ["path", "content", "branch"],
        },
    },
    {
        "name": "commit_batch",
        "description": (
            "原子提交多个文件变更（一次 commit 改多个文件）。"
            "适用于重构、重命名等需要同时改多个文件的场景。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "description": "文件变更列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "文件相对路径"},
                            "content": {"type": "string", "description": "完整文件内容"},
                        },
                        "required": ["path", "content"],
                    },
                },
                "branch": {"type": "string", "description": "分支名"},
                "message": {"type": "string", "description": "commit message"},
            },
            "required": ["changes", "branch"],
        },
    },
]

TOOL_MAP = {
    "read_file": lambda args: read_file(args["path"], args.get("branch", "main")),
    "edit_file": lambda args: edit_file(
        args["path"], args["old_string"], args["new_string"],
        args.get("branch", ""), args.get("message", ""),
    ),
    "write_file": lambda args: write_file(
        args["path"], args["content"], args.get("branch", ""), args.get("message", ""),
    ),
    "commit_batch": lambda args: commit_batch(
        args["changes"], args.get("branch", ""), args.get("message", ""),
    ),
}
