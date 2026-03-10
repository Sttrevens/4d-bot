"""Git 操作工具（通过 GitHub REST API，无需本地 clone）

安全策略:
- 禁止删除 main/master/develop/release
- 创建分支基于最新 main
"""

from __future__ import annotations

import logging

from app.tools.github_api import gh_get, gh_post, gh_delete
from app.tools._fuzzy import fuzzy_filter, fuzzy_match
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

_PROTECTED = {"main", "master", "develop", "release"}


def git_list_branches(per_page: int = 30, keyword: str = "") -> ToolResult:
    """列出仓库所有分支，支持 keyword 模糊过滤分支名"""
    data = gh_get("/branches", params={"per_page": per_page})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if keyword:
        data = [b for b in data if fuzzy_match(b.get("name", ""), keyword)]
    if not data:
        msg = f"没有找到匹配「{keyword}」的分支。" if keyword else "(no branches)"
        return ToolResult.success(msg)
    lines = [f"- {b['name']}" for b in data]
    return ToolResult.success("\n".join(lines))


def _find_similar_branches(branch_name: str) -> list[str]:
    """找到与目标分支名主题相似的已有分支。

    提取分支名中的关键词（去掉 fix/feat/等前缀和 v2/v3 等后缀），
    与所有远程分支做模糊匹配。
    """
    # 提取关键词：fix/taskui-not-moving → ["taskui", "not", "moving"]
    import re
    stem = re.sub(r"^(fix|feat|feature|bugfix|hotfix|refactor|chore)[/-]", "", branch_name)
    stem = re.sub(r"[-_]v\d+$", "", stem)  # 去掉 -v2, -v3 后缀
    keywords = [w.lower() for w in re.split(r"[-_/]", stem) if len(w) >= 3]
    if not keywords:
        return []

    data = gh_get("/branches", params={"per_page": 100})
    if isinstance(data, str):
        return []

    similar = []
    for b in data:
        name = b.get("name", "")
        if name in _PROTECTED or name == branch_name:
            continue
        name_lower = name.lower()
        # 任意一个关键词命中就算相似
        if any(kw in name_lower for kw in keywords):
            similar.append(name)
    return similar


def git_create_branch(branch_name: str, base: str = "main") -> ToolResult:
    """基于 base 分支创建新分支"""
    if branch_name in _PROTECTED:
        return ToolResult.invalid_param(f"不允许直接在 {branch_name} 上操作")

    # ── 自动检测相似分支，防止重复创建 ──
    similar = _find_similar_branches(branch_name)
    if similar:
        branch_list = "\n".join(f"  - {b}" for b in similar)
        return ToolResult.error(
            f"⚠️ 发现以下已有分支与「{branch_name}」主题相似：\n{branch_list}\n\n"
            f"请优先复用已有分支（用已有分支名作为 write_file 的 branch 参数）。\n"
            f"如果确实需要新分支，请用完全不同的名字重试。",
            code="similar_branch_exists",
        )

    ref_data = gh_get(f"/git/ref/heads/{base}")
    if isinstance(ref_data, str):
        return ToolResult.api_error(ref_data)
    sha = ref_data["object"]["sha"]

    result = gh_post("/git/refs", json={
        "ref": f"refs/heads/{branch_name}",
        "sha": sha,
    })
    if isinstance(result, str):
        return ToolResult.api_error(result)
    return ToolResult.success(f"created branch {branch_name} from {base}")


def git_delete_branch(branch_name: str) -> ToolResult:
    """删除远程分支"""
    if branch_name in _PROTECTED:
        return ToolResult.invalid_param(f"禁止删除受保护分支 {branch_name}")
    result = gh_delete(f"/git/refs/heads/{branch_name}")
    if isinstance(result, str) and result.startswith("[ERROR]"):
        return ToolResult.api_error(result)
    return ToolResult.success(f"deleted branch {branch_name}")


def git_log(branch: str = "main", count: int = 10) -> ToolResult:
    """查看指定分支的最近 commit"""
    data = gh_get("/commits", params={"sha": branch, "per_page": count})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    lines = []
    for c in data:
        sha_short = c["sha"][:7]
        msg = c["commit"]["message"].split("\n")[0]
        author = c["commit"]["author"]["name"]
        date = c["commit"]["author"]["date"][:10]
        lines.append(f"{sha_short} {date} {author}: {msg}")
    return ToolResult.success("\n".join(lines) or "(no commits)")


def git_diff(base: str = "main", head: str = "") -> ToolResult:
    """比较两个分支/commit 的差异"""
    if not head:
        return ToolResult.invalid_param("需要指定 head 分支名，如: git_diff(base='main', head='feat/xxx')")
    data = gh_get(f"/compare/{base}...{head}")
    if isinstance(data, str):
        return ToolResult.api_error(data)
    files = data.get("files", [])
    if not files:
        return ToolResult.success("no differences")
    lines = [f"total: {len(files)} files changed\n"]
    for f in files[:30]:
        lines.append(f"  {f['status']:10s} {f['filename']} (+{f['additions']}/-{f['deletions']})")
    return ToolResult.success("\n".join(lines))


TOOL_DEFINITIONS = [
    {
        "name": "git_list_branches",
        "description": "列出仓库所有分支。支持 keyword 模糊过滤分支名。",
        "input_schema": {
            "type": "object",
            "properties": {
                "per_page": {
                    "type": "integer",
                    "description": "返回分支数量，默认30",
                    "default": 30,
                },
                "keyword": {
                    "type": "string",
                    "description": "按分支名模糊过滤",
                    "default": "",
                },
            },
        },
    },
    {
        "name": "git_create_branch",
        "description": "基于指定分支（默认 main）创建新分支。",
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_name": {
                    "type": "string",
                    "description": "新分支名，如 feat/add-login",
                },
                "base": {
                    "type": "string",
                    "description": "基于哪个分支创建，默认 main",
                    "default": "main",
                },
            },
            "required": ["branch_name"],
        },
    },
    {
        "name": "git_delete_branch",
        "description": "删除远程分支。不会删除受保护分支(main/master/develop/release)。",
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_name": {
                    "type": "string",
                    "description": "要删除的分支名",
                },
            },
            "required": ["branch_name"],
        },
    },
    {
        "name": "git_log",
        "description": "查看指定分支的最近 commit 记录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "分支名，默认 main",
                    "default": "main",
                },
                "count": {
                    "type": "integer",
                    "description": "显示多少条，默认10",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "git_diff",
        "description": "比较两个分支的代码差异。",
        "input_schema": {
            "type": "object",
            "properties": {
                "base": {
                    "type": "string",
                    "description": "基准分支，默认 main",
                    "default": "main",
                },
                "head": {
                    "type": "string",
                    "description": "对比分支",
                },
            },
            "required": ["head"],
        },
    },
]

TOOL_MAP = {
    "git_list_branches": lambda args: git_list_branches(args.get("per_page", 30), keyword=args.get("keyword", "")),
    "git_create_branch": lambda args: git_create_branch(
        args["branch_name"], args.get("base", "main")
    ),
    "git_delete_branch": lambda args: git_delete_branch(args["branch_name"]),
    "git_log": lambda args: git_log(args.get("branch", "main"), args.get("count", 10)),
    "git_diff": lambda args: git_diff(args.get("base", "main"), args.get("head", "")),
}
