"""GitHub Issue 操作工具

通过 GitHub REST API 管理 Issues：
- 列出、创建、关闭 Issue
- 添加 Issue 评论
"""

from __future__ import annotations

import logging

from app.tools.github_api import gh_get, gh_post, gh_patch
from app.tools._fuzzy import fuzzy_filter
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)


def list_issues(state: str = "open", per_page: int = 10, labels: str = "", keyword: str = "") -> ToolResult:
    """列出仓库的 Issues，支持 keyword 模糊过滤标题"""
    params: dict = {"state": state, "per_page": per_page}
    if labels:
        params["labels"] = labels
    data = gh_get("/issues", params=params)
    if isinstance(data, str):
        return ToolResult.api_error(data)
    # GitHub issues API 也会返回 PR，需要过滤
    issues = [i for i in data if "pull_request" not in i]
    if keyword:
        issues = fuzzy_filter(issues, keyword, ["title"])
    if not issues:
        msg = f"没有 {state} 状态的 Issue。" if not keyword else f"没有找到匹配「{keyword}」的 Issue。"
        return ToolResult.success(msg)
    lines = [f"共 {len(issues)} 个 {state} Issue：\n"]
    for issue in issues:
        labels_str = ", ".join(l["name"] for l in issue.get("labels", []))
        label_part = f" [{labels_str}]" if labels_str else ""
        assignee = issue.get("assignee")
        assignee_str = f"  指派: {assignee['login']}" if assignee else ""
        lines.append(
            f"  #{issue['number']} {issue['title']}{label_part}{assignee_str}"
        )
    return ToolResult.success("\n".join(lines))


def get_issue(issue_number: int) -> ToolResult:
    """获取单个 Issue 的详细信息"""
    data = gh_get(f"/issues/{issue_number}")
    if isinstance(data, str):
        return ToolResult.api_error(data)
    labels_str = ", ".join(l["name"] for l in data.get("labels", []))
    assignees = ", ".join(a["login"] for a in data.get("assignees", []))
    lines = [
        f"Issue #{data['number']}: {data['title']}",
        f"状态: {data['state']}",
        f"作者: {data['user']['login']}  创建: {data['created_at'][:10]}",
    ]
    if labels_str:
        lines.append(f"标签: {labels_str}")
    if assignees:
        lines.append(f"指派: {assignees}")
    lines.append(f"URL: {data.get('html_url', '')}")
    body = data.get("body") or ""
    if body:
        lines.append(f"\n描述:\n{body[:500]}")
    return ToolResult.success("\n".join(lines))


def create_issue(title: str, body: str = "", labels: list[str] | None = None) -> ToolResult:
    """创建新 Issue"""
    payload: dict = {"title": title}
    if body:
        payload["body"] = body
    if labels:
        payload["labels"] = labels
    result = gh_post("/issues", json=payload)
    if isinstance(result, str):
        return ToolResult.api_error(result)
    return ToolResult.success(f"Issue created: #{result['number']} {result.get('html_url', '')}")


def close_issue(issue_number: int) -> ToolResult:
    """关闭 Issue"""
    result = gh_patch(f"/issues/{issue_number}", json={"state": "closed"})
    if isinstance(result, str):
        return ToolResult.api_error(result)
    return ToolResult.success(f"Issue #{issue_number} 已关闭")


def add_issue_comment(issue_number: int, body: str) -> ToolResult:
    """给 Issue 添加评论"""
    result = gh_post(f"/issues/{issue_number}/comments", json={"body": body})
    if isinstance(result, str):
        return ToolResult.api_error(result)
    return ToolResult.success(f"评论已添加到 Issue #{issue_number}")


def list_issue_comments(issue_number: int) -> ToolResult:
    """查看 Issue 的所有评论"""
    data = gh_get(f"/issues/{issue_number}/comments", params={"per_page": 30})
    if isinstance(data, str):
        return ToolResult.api_error(data)
    if not data:
        return ToolResult.success(f"Issue #{issue_number} 没有评论。")
    lines = [f"Issue #{issue_number} 共 {len(data)} 条评论：\n"]
    for c in data:
        author = c["user"]["login"]
        date = c["created_at"][:10]
        body_text = c["body"][:200]
        lines.append(f"  [{date}] {author}: {body_text}")
    return ToolResult.success("\n".join(lines))


# --------------- Tool definitions & map ---------------

TOOL_DEFINITIONS = [
    {
        "name": "list_issues",
        "description": "列出仓库的 Issues。可按状态、标签筛选，支持 keyword 模糊过滤标题。",
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "description": "Issue 状态：open / closed / all，默认 open",
                    "default": "open",
                    "enum": ["open", "closed", "all"],
                },
                "per_page": {
                    "type": "integer",
                    "description": "返回数量，默认 10",
                    "default": 10,
                },
                "labels": {
                    "type": "string",
                    "description": "按标签筛选（逗号分隔），如 'bug,urgent'",
                    "default": "",
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
        "name": "get_issue",
        "description": "获取某个 Issue 的详细信息。",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_number": {
                    "type": "integer",
                    "description": "Issue 编号",
                },
            },
            "required": ["issue_number"],
        },
    },
    {
        "name": "create_issue",
        "description": "在仓库中创建新 Issue。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Issue 标题",
                },
                "body": {
                    "type": "string",
                    "description": "Issue 描述（支持 Markdown）",
                    "default": "",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签列表，如 ['bug', 'urgent']",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "close_issue",
        "description": "关闭一个 Issue。",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_number": {
                    "type": "integer",
                    "description": "要关闭的 Issue 编号",
                },
            },
            "required": ["issue_number"],
        },
    },
    {
        "name": "add_issue_comment",
        "description": "给 Issue 添加评论。",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_number": {
                    "type": "integer",
                    "description": "Issue 编号",
                },
                "body": {
                    "type": "string",
                    "description": "评论内容（支持 Markdown）",
                },
            },
            "required": ["issue_number", "body"],
        },
    },
    {
        "name": "list_issue_comments",
        "description": "查看 Issue 的所有评论。",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_number": {
                    "type": "integer",
                    "description": "Issue 编号",
                },
            },
            "required": ["issue_number"],
        },
    },
]

TOOL_MAP = {
    "list_issues": lambda args: list_issues(
        state=args.get("state", "open"),
        per_page=args.get("per_page", 10),
        labels=args.get("labels", ""),
        keyword=args.get("keyword", ""),
    ),
    "get_issue": lambda args: get_issue(
        issue_number=args["issue_number"],
    ),
    "create_issue": lambda args: create_issue(
        title=args["title"],
        body=args.get("body", ""),
        labels=args.get("labels"),
    ),
    "close_issue": lambda args: close_issue(
        issue_number=args["issue_number"],
    ),
    "add_issue_comment": lambda args: add_issue_comment(
        issue_number=args["issue_number"],
        body=args["body"],
    ),
    "list_issue_comments": lambda args: list_issue_comments(
        issue_number=args["issue_number"],
    ),
}
