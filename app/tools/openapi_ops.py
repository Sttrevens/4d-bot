"""飞书开放平台万能 API 调用工具

当现有的专用工具（doc_ops / calendar_ops / task_ops 等）不覆盖某个飞书功能时，
LLM 可以用这个工具直接调用飞书开放平台的任意 API 端点。

设计参考了飞书 CLI (larksuite/cli) 的三层架构中的 Raw API 层：
- 支持任意 HTTP 方法 + 路径 + 请求体
- 自动处理 token 和错误
- 内置分页支持
- 响应截断保护（避免 token 爆炸）
"""

from __future__ import annotations

import json
import logging

from app.tools.feishu_api import (
    feishu_delete,
    feishu_get,
    feishu_patch,
    feishu_post,
    feishu_put,
    has_user_token,
)
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# 响应最大字符数（超过则截断，给 LLM 留余量）
_MAX_RESPONSE_CHARS = 15000

# 禁止调用的路径前缀（安全：不允许 LLM 操作认证相关端点）
_BLOCKED_PATHS = (
    "/auth/",
    "/authen/",
    "/app_access_token",
    "/tenant_access_token",
)

_METHOD_DISPATCH = {
    "GET": lambda path, body, query, user_tok: feishu_get(
        path, params=query, use_user_token=user_tok,
    ),
    "POST": lambda path, body, query, user_tok: feishu_post(
        path, json=body or {}, params=query, use_user_token=user_tok,
    ),
    "PATCH": lambda path, body, query, user_tok: feishu_patch(
        path, json=body or {}, params=query, use_user_token=user_tok,
    ),
    "PUT": lambda path, body, query, user_tok: feishu_put(
        path, json=body or {}, params=query, use_user_token=user_tok,
    ),
    "DELETE": lambda path, body, query, user_tok: feishu_delete(
        path, json=body, params=query, use_user_token=user_tok,
    ),
}


def _normalize_path(path: str) -> str:
    """统一路径格式，接受多种写法。

    接受：
      - /im/v1/messages           → /im/v1/messages
      - /open-apis/im/v1/messages → /im/v1/messages
      - im/v1/messages            → /im/v1/messages
    """
    path = path.strip()
    if path.startswith("/open-apis/"):
        path = path[len("/open-apis"):]
    elif path.startswith("open-apis/"):
        path = "/" + path[len("open-apis"):]
    if not path.startswith("/"):
        path = "/" + path
    return path


def _truncate(text: str) -> str:
    if len(text) <= _MAX_RESPONSE_CHARS:
        return text
    return text[:_MAX_RESPONSE_CHARS] + f"\n\n... [响应被截断，原始长度 {len(text)} 字符。如需更多数据请用分页参数]"


def call_feishu_api(
    method: str,
    path: str,
    body: dict | None = None,
    query: dict | None = None,
    use_user_token: bool = False,
    page_size: int | None = None,
    page_token: str | None = None,
) -> ToolResult:
    """调用飞书开放平台任意 API。"""
    method = method.upper().strip()
    if method not in _METHOD_DISPATCH:
        return ToolResult.invalid_param(
            f"不支持的 HTTP 方法: {method}",
            retry_hint="method 必须是 GET/POST/PATCH/PUT/DELETE 之一",
        )

    path = _normalize_path(path)

    # 安全：禁止调用认证端点
    for blocked in _BLOCKED_PATHS:
        if path.startswith(blocked):
            return ToolResult.blocked(f"安全限制：不允许调用认证相关端点 {blocked}")

    # 用户 token 判断
    if use_user_token and not has_user_token():
        use_user_token = False
        logger.info("call_feishu_api: user_token 不可用，降级为 tenant_token")

    # 分页参数注入
    if query is None:
        query = {}
    if page_size is not None:
        query["page_size"] = str(page_size)
    if page_token:
        query["page_token"] = page_token

    dispatcher = _METHOD_DISPATCH[method]

    try:
        result = dispatcher(path, body, query, use_user_token)
    except Exception as e:
        logger.exception("call_feishu_api exception: %s %s", method, path)
        return ToolResult.api_error(f"请求异常: {type(e).__name__}: {e}")

    # 字符串结果 = 错误消息或纯文本
    if isinstance(result, str):
        if result.startswith("[ERROR]"):
            return ToolResult.api_error(result)
        return ToolResult.success(_truncate(result))

    # dict 结果 = 正常 JSON 响应
    output_parts = []

    data = result.get("data", result)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    output_parts.append(text)

    # 分页提示
    if isinstance(data, dict):
        has_more = data.get("has_more", False)
        next_token = data.get("page_token", "")
        if has_more and next_token:
            output_parts.append(
                f"\n📄 还有更多数据，用 page_token=\"{next_token}\" 继续翻页"
            )

    return ToolResult.success(_truncate("\n".join(output_parts)))


# ── 工具定义 ──

TOOL_DEFINITIONS = [
    {
        "name": "call_feishu_api",
        "description": (
            "调用飞书开放平台任意 API（万能接口）。"
            "当现有专用工具不覆盖你需要的功能时使用此工具。\n\n"
            "常用 API 路径示例：\n"
            "- 消息: GET /im/v1/messages, POST /im/v1/messages\n"
            "- 群聊: GET /im/v1/chats, POST /im/v1/chats\n"
            "- 文档: GET /docx/v1/documents/:id, POST /docx/v1/documents\n"
            "- 电子表格: GET /sheets/v3/spreadsheets/:id/sheets/query\n"
            "- 多维表格: GET /bitable/v1/apps/:app_token/tables/:table_id/records\n"
            "- 日历: GET /calendar/v4/calendars/:id/events\n"
            "- 任务: GET /task/v2/tasks, POST /task/v2/tasks\n"
            "- 通讯录: GET /contact/v3/users/:user_id\n"
            "- 知识库: GET /wiki/v2/spaces, GET /wiki/v2/spaces/:space_id/nodes\n"
            "- 云空间: GET /drive/v1/files\n"
            "- 邮箱: GET /mail/v1/mailgroups\n"
            "- 审批: POST /approval/v4/instances\n"
            "- 考勤: GET /attendance/v1/user_stats_datas/query\n"
            "- 搜索: POST /suite/docs-api/search/object\n\n"
            "完整 API 文档参考飞书开放平台。路径中 :id 等占位符需替换为实际值。\n"
            "支持自动分页：设置 page_size + 根据返回的 page_token 翻页。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"],
                    "description": "HTTP 方法",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "API 路径，如 /im/v1/messages 或 /open-apis/im/v1/messages。"
                        "路径中的变量用实际值替换，如 /docx/v1/documents/abc123"
                    ),
                },
                "body": {
                    "type": "object",
                    "description": "请求体（POST/PATCH/PUT 时使用）",
                },
                "query": {
                    "type": "object",
                    "description": "URL 查询参数，如 {\"user_id_type\": \"open_id\"}",
                },
                "use_user_token": {
                    "type": "boolean",
                    "description": "是否使用用户身份调用（默认用应用身份）。日历、邮箱等个人数据需要用户身份。",
                    "default": False,
                },
                "page_size": {
                    "type": "integer",
                    "description": "分页大小（列表接口可用）",
                },
                "page_token": {
                    "type": "string",
                    "description": "分页令牌（从上一次返回的 page_token 获取）",
                },
            },
            "required": ["method", "path"],
        },
    },
]

TOOL_MAP = {
    "call_feishu_api": lambda args: call_feishu_api(
        method=args["method"],
        path=args["path"],
        body=args.get("body"),
        query=args.get("query"),
        use_user_token=args.get("use_user_token", False),
        page_size=args.get("page_size"),
        page_token=args.get("page_token"),
    ),
}
