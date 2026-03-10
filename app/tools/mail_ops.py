"""飞书邮箱操作工具

通过飞书 Mail v1 API 管理用户邮箱：
- 查询用户邮箱地址
- 列出收件箱邮件
- 获取邮件详情
- 发送邮件

所有操作需要用户 OAuth 授权（mail:user_mailbox scope）。
"""

from __future__ import annotations

import base64
import logging

from app.tools.feishu_api import (
    feishu_get, feishu_post,
    has_user_token, _current_user_open_id,
)
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# 飞书邮箱 API 中 user_mailbox_id 使用 "me" 代表当前授权用户
_ME = "me"


def _use_user() -> bool:
    """邮箱操作必须用用户身份"""
    return has_user_token()


def _require_auth() -> ToolResult | None:
    """检查用户是否已授权邮箱。未授权则返回错误提示。"""
    if not has_user_token():
        return ToolResult.error(
            "用户未授权邮箱功能。请发 /auth 授权后再试。"
            "邮箱功能需要用户 OAuth 授权才能使用。",
            code="auth_required",
        )
    return None



def query_mail_address() -> ToolResult:
    """查询当前用户的邮箱地址"""
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    open_id = _current_user_open_id.get("")
    if not open_id:
        return ToolResult.error("无法获取当前用户信息")

    # 用通讯录 API 查询用户邮箱
    data = feishu_get(
        f"/contact/v3/users/{open_id}",
        params={"user_id_type": "open_id"},
        use_user_token=False,  # 通讯录用 tenant token
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    user_info = data.get("data", {}).get("user", {})
    email = user_info.get("enterprise_email") or user_info.get("email") or ""
    name = user_info.get("name", "")

    if not email:
        return ToolResult.not_found(
            f"未找到用户 {name} 的企业邮箱地址。"
            "可能原因：1) 企业未开通飞书邮箱 2) 该用户未分配邮箱"
        )

    return ToolResult.success(f"用户 {name} 的邮箱地址：{email}")


def _get_inbox_folder_id() -> tuple[str | None, str]:
    """获取收件箱 folder_id。

    策略：先尝试 /folders API（需要 mail:user_mailbox.folder:read scope），
    失败则 fallback 到常见 folder_id 候选值逐个试探。
    结果会缓存在模块级变量中。

    Returns:
        (folder_id, error_msg) — folder_id 非空表示成功，
        error_msg 记录最后一次 API 错误（用于上层展示）。
    """
    global _inbox_folder_id
    if _inbox_folder_id:
        return _inbox_folder_id, ""

    last_error = ""

    # 方案 1：调 /folders API 获取
    data = feishu_get(
        f"/mail/v1/user_mailboxes/{_ME}/folders",
        use_user_token=True,
    )
    if not isinstance(data, str):
        folders = data.get("data", {}).get("items", [])
        for f in folders:
            name = (f.get("name") or "").upper()
            folder_type = f.get("folder_type")
            if name in ("INBOX", "收件箱") or folder_type == 1:
                _inbox_folder_id = f.get("folder_id", "")
                return _inbox_folder_id, ""
        if folders:
            _inbox_folder_id = folders[0].get("folder_id", "")
            return _inbox_folder_id, ""
    else:
        last_error = data
        logger.warning("get mail folders failed: %s — trying fallback", data[:200])

    # 方案 2：fallback — 逐个试探常见 folder_id
    # 飞书可能用 "INBOX"、"1" 或数字 ID 作为收件箱标识
    for candidate in ("INBOX", "1"):
        test = feishu_get(
            f"/mail/v1/user_mailboxes/{_ME}/messages",
            params={"page_size": 1, "folder_id": candidate},
            use_user_token=True,
        )
        if not isinstance(test, str):
            logger.info("mail folder_id fallback succeeded: %s", candidate)
            _inbox_folder_id = candidate
            return _inbox_folder_id, ""
        last_error = test

    return None, last_error


_inbox_folder_id: str = ""


def list_mails(count: int = 10, only_unread: bool = False) -> ToolResult:
    """列出邮箱收件箱中的邮件

    Args:
        count: 获取数量，默认 10，最大 20
        only_unread: 是否只查未读邮件
    """
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    count = max(1, min(count, 20))  # API 上限 20

    # 策略：先尝试获取 inbox folder_id，失败则不传 folder_id（SDK 确认它是可选的）
    folder_id, folder_err = _get_inbox_folder_id()

    params: dict = {"page_size": count}
    if folder_id:
        params["folder_id"] = folder_id
    if only_unread:
        params["only_unread"] = True

    data = feishu_get(
        f"/mail/v1/user_mailboxes/{_ME}/messages",
        params=params,
        use_user_token=True,
    )

    # 如果带 folder_id 失败了，试一次不带 folder_id
    if isinstance(data, str) and folder_id:
        logger.warning("list_mails with folder_id=%s failed: %s — retrying without folder_id", folder_id, data[:200])
        params_no_folder: dict = {"page_size": count}
        if only_unread:
            params_no_folder["only_unread"] = True
        data = feishu_get(
            f"/mail/v1/user_mailboxes/{_ME}/messages",
            params=params_no_folder,
            use_user_token=True,
        )

    if isinstance(data, str):
        detail = f"\nAPI 错误详情：{data[:300]}"
        if folder_err:
            detail += f"\n文件夹查询错误：{folder_err[:200]}"
        return ToolResult.error(
            "无法获取邮箱邮件。可能原因：\n"
            "1) 企业未开通飞书邮箱\n"
            "2) 用户未授权邮箱权限（发送 /auth 重新授权）\n"
            "3) 飞书开发者后台未开通邮件相关权限\n"
            f"{detail}\n\n"
            "建议先尝试重新发送 /auth 命令授权。"
            "这不是代码bug，不需要自我修复。",
            code="mail_api_error",
        )

    # API 返回邮件 ID 列表，需要逐个获取详情
    msg_ids = data.get("data", {}).get("items", [])
    if not msg_ids:
        return ToolResult.success("收件箱为空，没有邮件。")

    # 逐个获取邮件摘要
    lines = [f"共 {len(msg_ids)} 封邮件：\n"]
    for i, msg_id in enumerate(msg_ids, 1):
        detail = feishu_get(
            f"/mail/v1/user_mailboxes/{_ME}/messages/{msg_id}",
            use_user_token=True,
        )
        if isinstance(detail, str):
            lines.append(f"{i}. (获取失败) ID: {msg_id}")
            lines.append("")
            continue

        msg = detail.get("data", {})
        subject = msg.get("subject", "(无主题)")
        from_info = msg.get("from", {})
        from_addr = from_info.get("address", from_info.get("name", ""))
        date = msg.get("date", "")
        is_read = msg.get("is_read", False)
        read_mark = "" if is_read else "[未读] "

        lines.append(f"{i}. {read_mark}{subject}")
        if from_addr:
            lines.append(f"   来自: {from_addr}")
        if date:
            lines.append(f"   时间: {date}")
        lines.append(f"   ID: {msg_id}")
        lines.append("")

    return ToolResult.success("\n".join(lines))


def get_mail(message_id: str) -> ToolResult:
    """获取邮件详情

    Args:
        message_id: 邮件 ID（从 list_mails 获取）
    """
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    if not message_id:
        return ToolResult.invalid_param("message_id 不能为空")

    data = feishu_get(
        f"/mail/v1/user_mailboxes/{_ME}/messages/{message_id}",
        use_user_token=True,
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    msg = data.get("data", {})
    subject = msg.get("subject", "(无主题)")
    from_info = msg.get("from", {})
    from_addr = from_info.get("address", from_info.get("name", "未知"))
    to_list = msg.get("to", [])
    to_addrs = ", ".join(t.get("address", t.get("name", "")) for t in to_list) if to_list else "未知"
    date = msg.get("date", "")
    body = msg.get("body", {})

    # body 可能是 plain_text 或 html
    body_text = body.get("plain_text", "")
    if not body_text and body.get("html"):
        body_text = "(HTML 邮件，纯文本内容不可用)"

    # 附件
    attachments = msg.get("attachments", [])
    attach_info = ""
    if attachments:
        attach_names = [a.get("filename", "未命名") for a in attachments]
        attach_info = f"\n附件: {', '.join(attach_names)}"

    result = (
        f"主题: {subject}\n"
        f"发件人: {from_addr}\n"
        f"收件人: {to_addrs}\n"
        f"时间: {date}\n"
        f"{attach_info}\n"
        f"正文:\n{body_text[:3000]}"
    )

    return ToolResult.success(result.strip())


def send_mail(to: str, subject: str, body: str, cc: str = "") -> ToolResult:
    """发送邮件

    Args:
        to: 收件人邮箱地址（多个用逗号分隔）
        subject: 邮件主题
        body: 邮件正文（纯文本）
        cc: 抄送地址（可选，多个用逗号分隔）
    """
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    if not to or not to.strip():
        return ToolResult.invalid_param("收件人(to)不能为空")
    if not subject or not subject.strip():
        return ToolResult.invalid_param("邮件主题(subject)不能为空")
    if not body or not body.strip():
        return ToolResult.invalid_param("邮件正文(body)不能为空")

    # 构建收件人列表
    to_list = [{"address": addr.strip()} for addr in to.split(",") if addr.strip()]
    if not to_list:
        return ToolResult.invalid_param("收件人地址格式不正确")

    # 构建抄送列表
    cc_list = []
    if cc and cc.strip():
        cc_list = [{"address": addr.strip()} for addr in cc.split(",") if addr.strip()]

    # 构建邮件请求体
    message_body: dict = {
        "subject": subject.strip(),
        "to": to_list,
        "body": {
            "plain_text": body.strip(),
        },
    }
    if cc_list:
        message_body["cc"] = cc_list

    data = feishu_post(
        f"/mail/v1/user_mailboxes/{_ME}/messages/send",
        json=message_body,
        use_user_token=True,
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    to_display = ", ".join(a["address"] for a in to_list)
    return ToolResult.success(f"邮件已发送给 {to_display}，主题: {subject}")


# ── 工具定义 ──

TOOL_DEFINITIONS = [
    {
        "name": "query_mail_address",
        "description": "查询当前用户的飞书邮箱地址。需要用户已 /auth 授权。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_mails",
        "description": "列出飞书邮箱收件箱中的邮件（主题、发件人、时间）。需要用户已 /auth 授权。",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "获取邮件数量，默认 10，最大 20",
                },
                "only_unread": {
                    "type": "boolean",
                    "description": "是否只查未读邮件，默认 false",
                },
            },
        },
    },
    {
        "name": "get_mail",
        "description": "获取一封邮件的详细内容（正文、附件等）。需要先用 list_mails 获取邮件 ID。",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "邮件 ID（从 list_mails 结果中获取）",
                },
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "send_mail",
        "description": "通过飞书邮箱发送邮件。需要用户已 /auth 授权。",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "收件人邮箱地址，多个用逗号分隔",
                },
                "subject": {
                    "type": "string",
                    "description": "邮件主题",
                },
                "body": {
                    "type": "string",
                    "description": "邮件正文（纯文本）",
                },
                "cc": {
                    "type": "string",
                    "description": "抄送地址，多个用逗号分隔（可选）",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
]

TOOL_MAP = {
    "query_mail_address": lambda args: query_mail_address(),
    "list_mails": lambda args: list_mails(
        count=args.get("count", 10),
        only_unread=args.get("only_unread", False),
    ),
    "get_mail": lambda args: get_mail(
        message_id=args["message_id"],
    ),
    "send_mail": lambda args: send_mail(
        to=args["to"],
        subject=args["subject"],
        body=args["body"],
        cc=args.get("cc", ""),
    ),
}
