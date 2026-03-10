"""GitHub PR 操作工具

通过 GitHub REST API 管理 Pull Requests：
- 创建、列出、查看、合并、关闭 PR
- 添加 PR 评论
- 查看 CI 状态
"""

from __future__ import annotations

import logging

from app.tools.github_api import gh_get, gh_post, gh_put, gh_patch
from app.tools._fuzzy import fuzzy_filter
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)


def create_pull_request(
    title: str, body: str, head: str, base: str = "main"
) -> ToolResult:
    """创建 Pull Request，返回 PR URL"""
    result = gh_post("/pulls", json={
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    })
    if isinstance(result, str):
        return ToolResult.api_error(result)
    return ToolResult.success(f"PR created: {result.get('html_url', '')}")


def list_pull_requests(state: str = "open", per_page: int = 10, keyword: str = "") -> ToolResult:
    """列出仓库的 Pull Requests，支持 keyword 模糊过滤标题"""
    data = gh_get("/pulls", params={"state": state, "per_page": per_page})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if keyword:
        data = fuzzy_filter(data, keyword, ["title"])
    if not data:
        msg = f"没有 {state} 状态的 PR。" if not keyword else f"没有找到匹配「{keyword}」的 PR。"
        return ToolResult.success(msg)
    lines = [f"共 {len(data)} 个 {state} PR：\n"]
    for pr in data:
        labels = ", ".join(l["name"] for l in pr.get("labels", []))
        label_str = f" [{labels}]" if labels else ""
        lines.append(
            f"  #{pr['number']} {pr['title']}{label_str}\n"
            f"    {pr['head']['ref']} → {pr['base']['ref']}  "
            f"by {pr['user']['login']}  {pr['state']}"
        )
    return ToolResult.success("\n".join(lines))


def get_pull_request(pr_number: int) -> ToolResult:
    """获取单个 PR 的详细信息"""
    data = gh_get(f"/pulls/{pr_number}")
    if isinstance(data, str):
        return ToolResult.api_error(data)
    mergeable = data.get("mergeable")
    mergeable_str = {True: "可合并", False: "有冲突", None: "检查中"}.get(mergeable, "未知")
    lines = [
        f"PR #{data['number']}: {data['title']}",
        f"状态: {data['state']}  可合并: {mergeable_str}",
        f"分支: {data['head']['ref']} → {data['base']['ref']}",
        f"作者: {data['user']['login']}  创建: {data['created_at'][:10]}",
        f"变更: +{data.get('additions', 0)}/-{data.get('deletions', 0)}  "
        f"文件数: {data.get('changed_files', 0)}",
        f"URL: {data.get('html_url', '')}",
    ]
    body = data.get("body") or ""
    if body:
        lines.append(f"\n描述:\n{body[:500]}")
    return ToolResult.success("\n".join(lines))


def merge_pull_request(
    pr_number: int, merge_method: str = "squash", commit_title: str = ""
) -> ToolResult:
    """合并 Pull Request

    merge_method: merge / squash / rebase
    """
    if merge_method not in ("merge", "squash", "rebase"):
        return ToolResult.invalid_param(f"不支持的合并方式: {merge_method}，只支持 merge/squash/rebase")

    payload: dict = {"merge_method": merge_method}
    if commit_title:
        payload["commit_title"] = commit_title

    result = gh_put(f"/pulls/{pr_number}/merge", json=payload)
    if isinstance(result, str):
        return ToolResult.api_error(result)
    if result.get("merged"):
        return ToolResult.success(f"PR #{pr_number} 已成功合并 (方式: {merge_method})")
    return ToolResult.error(f"PR #{pr_number} 合并失败: {result.get('message', '未知原因')}", code="api_error")


def close_pull_request(pr_number: int) -> ToolResult:
    """关闭 PR（不合并）"""
    result = gh_patch(f"/pulls/{pr_number}", json={"state": "closed"})
    if isinstance(result, str):
        return ToolResult.api_error(result)
    return ToolResult.success(f"PR #{pr_number} 已关闭")


def add_pr_comment(pr_number: int, body: str) -> ToolResult:
    """给 PR 添加评论"""
    # PR comments 使用 issues API
    result = gh_post(f"/issues/{pr_number}/comments", json={"body": body})
    if isinstance(result, str):
        return ToolResult.api_error(result)
    return ToolResult.success(f"评论已添加到 PR #{pr_number}")


def list_pr_comments(pr_number: int) -> ToolResult:
    """查看 PR 的所有评论"""
    data = gh_get(f"/issues/{pr_number}/comments", params={"per_page": 30})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if not data:
        return ToolResult.success(f"PR #{pr_number} 没有评论。")
    lines = [f"PR #{pr_number} 共 {len(data)} 条评论：\n"]
    for c in data:
        author = c["user"]["login"]
        date = c["created_at"][:10]
        body = c["body"][:200]
        lines.append(f"  [{date}] {author}: {body}")
    return ToolResult.success("\n".join(lines))


def get_ci_status(ref: str) -> ToolResult:
    """查看某个分支/commit 的 CI 检查状态"""
    # 先查 check runs (GitHub Actions)
    data = gh_get(f"/commits/{ref}/check-runs")
    if isinstance(data, str):
        return ToolResult.api_error(data)

    runs = data.get("check_runs", [])
    if not runs:
        return ToolResult.success(f"分支 {ref} 没有 CI 检查记录。")

    lines = [f"分支 {ref} 的 CI 状态（共 {len(runs)} 项）：\n"]
    for run in runs:
        status = run.get("status", "unknown")
        conclusion = run.get("conclusion") or "进行中"
        name = run.get("name", "unknown")
        lines.append(f"  {name}: {status} / {conclusion}")
    return ToolResult.success("\n".join(lines))


def list_pr_files(pr_number: int) -> ToolResult:
    """查看 PR 中变更的文件列表"""
    data = gh_get(f"/pulls/{pr_number}/files", params={"per_page": 50})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if not data:
        return ToolResult.success(f"PR #{pr_number} 没有文件变更。")
    lines = [f"PR #{pr_number} 变更了 {len(data)} 个文件：\n"]
    for f in data:
        lines.append(
            f"  {f['status']:10s} {f['filename']} "
            f"(+{f['additions']}/-{f['deletions']})"
        )
    return ToolResult.success("\n".join(lines))


# --------------- Tool definitions & map ---------------

TOOL_DEFINITIONS = [
    {
        "name": "create_pull_request",
        "description": "在 GitHub 上创建一个 Pull Request。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "PR 标题",
                },
                "body": {
                    "type": "string",
                    "description": "PR 描述",
                },
                "head": {
                    "type": "string",
                    "description": "源分支名",
                },
                "base": {
                    "type": "string",
                    "description": "目标分支名，默认 main",
                    "default": "main",
                },
            },
            "required": ["title", "body", "head"],
        },
    },
    {
        "name": "list_pull_requests",
        "description": "列出仓库的 Pull Requests。可按状态筛选，支持 keyword 模糊过滤标题。",
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "PR 状态：open / closed / all，默认 open",
                    "default": "open",
                    "enum": ["open", "closed", "all"],
                },
                "per_page": {
                    "type": "integer",
                    "description": "返回数量，默认 10",
                    "default": 10,
                },
                "keyword": {
                    "type": "string",
                    "description": "按标题模糊过滤（支持子串、多词、去标点匹配）",
                    "default": "",
                },
            },
        },
    },
    {
        "name": "get_pull_request",
        "description": "获取某个 PR 的详细信息，包括状态、是否可合并、变更统计等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_number": {
                    "type": "integer",
                    "description": "PR 编号",
                },
            },
            "required": ["pr_number"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "合并一个 Pull Request。支持 merge / squash / rebase 三种方式。默认使用 squash。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_number": {
                    "type": "integer",
                    "description": "要合并的 PR 编号",
                },
                "merge_method": {
                    "type": "string",
                    "description": "合并方式：merge（普通合并）/ squash（压缩合并）/ rebase（变基合并），默认 squash",
                    "default": "squash",
                    "enum": ["merge", "squash", "rebase"],
                },
                "commit_title": {
                    "type": "string",
                    "description": "合并 commit 的标题，留空使用默认",
                    "default": "",
                },
            },
            "required": ["pr_number"],
        },
    },
    {
        "name": "close_pull_request",
        "description": "关闭一个 PR（不合并）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_number": {
                    "type": "integer",
                    "description": "要关闭的 PR 编号",
                },
            },
            "required": ["pr_number"],
        },
    },
    {
        "name": "add_pr_comment",
        "description": "给 PR 添加评论。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_number": {
                    "type": "integer",
                    "description": "PR 编号",
                },
                "body": {
                    "type": "string",
                    "description": "评论内容（支持 Markdown）",
                },
            },
            "required": ["pr_number", "body"],
        },
    },
    {
        "name": "list_pr_comments",
        "description": "查看 PR 的所有评论。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_number": {
                    "type": "integer",
                    "description": "PR 编号",
                },
            },
            "required": ["pr_number"],
        },
    },
    {
        "name": "list_pr_files",
        "description": "查看 PR 中变更的文件列表和每个文件的增删统计。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pr_number": {
                    "type": "integer",
                    "description": "PR 编号",
                },
            },
            "required": ["pr_number"],
        },
    },
    {
        "name": "get_ci_status",
        "description": "查看某个分支或 commit 的 CI 检查状态（GitHub Actions 等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "分支名或 commit SHA",
                },
            },
            "required": ["ref"],
        },
    },
]

TOOL_MAP = {
    "create_pull_request": lambda args: create_pull_request(
        title=args["title"],
        body=args["body"],
        head=args["head"],
        base=args.get("base", "main"),
    ),
    "list_pull_requests": lambda args: list_pull_requests(
        state=args.get("state", "open"),
        per_page=args.get("per_page", 10),
        keyword=args.get("keyword", ""),
    ),
    "get_pull_request": lambda args: get_pull_request(
        pr_number=args["pr_number"],
    ),
    "merge_pull_request": lambda args: merge_pull_request(
        pr_number=args["pr_number"],
        merge_method=args.get("merge_method", "squash"),
        commit_title=args.get("commit_title", ""),
    ),
    "close_pull_request": lambda args: close_pull_request(
        pr_number=args["pr_number"],
    ),
    "add_pr_comment": lambda args: add_pr_comment(
        pr_number=args["pr_number"],
        body=args["body"],
    ),
    "list_pr_comments": lambda args: list_pr_comments(
        pr_number=args["pr_number"],
    ),
    "list_pr_files": lambda args: list_pr_files(
        pr_number=args["pr_number"],
    ),
    "get_ci_status": lambda args: get_ci_status(
        ref=args["ref"],
    ),
}
