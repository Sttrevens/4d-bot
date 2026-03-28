"""飞书开放平台 API 域提示 —— 给 call_feishu_api 万能工具提供上下文

当用户消息涉及特定飞书业务域（审批、考勤、搜索等）时，
动态注入该域的 API 路径、参数格式、常见陷阱。

灵感来自飞书 CLI (larksuite/cli) 的 19 个 Skill 文档，
提炼了最实用的 API 知识，让 LLM 知道该怎么构造 call_feishu_api 调用。

只包含现有专用工具（doc_ops / calendar_ops / task_ops 等）**不覆盖**的域，
避免与专用工具的 description 重复。
"""

from __future__ import annotations

import re
from typing import Sequence

# ── 域提示定义 ──
# key = 域名（用于日志），value = dict(keywords, hint)
# keywords: 用户消息中的触发词（任一命中即注入）
# hint: 注入到 system prompt 的指导文本

_DOMAIN_HINTS: dict[str, dict] = {
    "approval": {
        "keywords": ["审批", "请假", "报销", "approval", "审批流", "OA"],
        "hint": """
## 飞书审批 API (call_feishu_api)

创建审批实例：POST /approval/v4/instances
  body: {"approval_code": "...", "form": "[{\"id\":\"widget1\",\"value\":\"...\"}]"}
  注意：form 是 JSON 字符串，不是对象。approval_code 从审批定义获取。

查询审批定义：GET /approval/v4/approvals/:approval_code

查询审批实例列表：GET /approval/v4/instances
  query: {"approval_code": "...", "start_time": "毫秒时间戳", "end_time": "毫秒时间戳"}

查询单个实例：GET /approval/v4/instances/:instance_id

审批/拒绝：POST /approval/v4/instances/approve 或 /reject
  body: {"approval_code": "...", "instance_code": "...", "user_id": "...", "task_id": "..."}

⚠️ 时间戳用毫秒（Unix × 1000）。user_id 需要 open_id 格式。""",
    },
    "attendance": {
        "keywords": ["考勤", "打卡", "出勤", "迟到", "早退", "attendance"],
        "hint": """
## 飞书考勤 API (call_feishu_api)

查询考勤统计：POST /attendance/v1/user_stats_datas/query
  body: {"user_ids": ["ou_xxx"], "start_date": 20260101, "end_date": 20260131}
  query: {"employee_type": "employee_id"}
  注意：日期格式 YYYYMMDD（整数），不是字符串。

查询打卡记录：POST /attendance/v1/user_flows/query
  body: {"user_ids": ["ou_xxx"], "start_time": "秒时间戳", "end_time": "秒时间戳"}

查询班次：GET /attendance/v1/shifts/:shift_id

⚠️ user_ids 用 employee_id 或 open_id，取决于 employee_type 参数。""",
    },
    "search": {
        "keywords": ["搜索飞书", "全局搜索", "飞书搜索", "搜文档", "搜消息", "lark search"],
        "hint": """
## 飞书搜索 API (call_feishu_api)

搜索消息：POST /search/v2/message
  body: {"query": "关键词", "message_type": "file/image/text", "chat_id": "oc_xxx"}
  query: {"page_size": 20}
  ⚠️ 需要 user_access_token（use_user_token=true）

搜索文档/云空间：POST /suite/docs-api/search/object
  body: {"search_key": "关键词", "count": 20, "offset": 0, "owner_ids": [], "docs_types": []}
  ⚠️ 也需要 user_access_token。

搜索群组：GET /im/v1/chats/search
  query: {"query": "群名关键词", "page_size": 20}""",
    },
    "sheets": {
        "keywords": ["电子表格", "spreadsheet", "sheet", "单元格", "工作表"],
        "hint": """
## 飞书电子表格 API (call_feishu_api)

获取表格元数据：GET /sheets/v3/spreadsheets/:spreadsheet_token

获取工作表列表：GET /sheets/v3/spreadsheets/:token/sheets/query

读取单元格范围：GET /sheets/v2/spreadsheets/:token/values/:range
  range 格式: "SheetName!A1:C10" 或 "sheet_id!A1:C10"

写入单元格：PUT /sheets/v2/spreadsheets/:token/values
  body: {"valueRange": {"range": "Sheet1!A1:C2", "values": [["a","b","c"],["d","e","f"]]}}

追加数据：POST /sheets/v2/spreadsheets/:token/values_append
  query: {"insertDataOption": "INSERT_ROWS"}
  body: {"valueRange": {"range": "Sheet1!A:C", "values": [["新行1","新行2","新行3"]]}}

⚠️ spreadsheet_token 从 URL 提取：feishu.cn/sheets/:token""",
    },
    "contact": {
        "keywords": ["通讯录", "部门", "组织架构", "人员信息", "employee", "department"],
        "hint": """
## 飞书通讯录 API (call_feishu_api)

搜索用户：GET /contact/v3/users/find_by_department
  或 POST /search/v1/user
  body: {"query": "姓名"}
  query: {"page_size": 20}

获取用户详情：GET /contact/v3/users/:user_id
  query: {"user_id_type": "open_id"}
  ⚠️ user_id_type 很重要，不传默认 open_id。

获取部门列表：GET /contact/v3/departments/:department_id/children
  根部门 ID: "0"

获取部门用户：GET /contact/v3/users/find_by_department
  query: {"department_id": "xxx", "page_size": 50}""",
    },
    "wiki": {
        "keywords": ["知识库", "wiki", "知识空间", "文档节点"],
        "hint": """
## 飞书知识库 API (call_feishu_api)

获取知识空间列表：GET /wiki/v2/spaces
  query: {"page_size": 50}

获取空间下节点：GET /wiki/v2/spaces/:space_id/nodes
  query: {"page_size": 50, "parent_node_token": "根节点留空"}

获取节点详情：GET /wiki/v2/spaces/get_node
  query: {"token": "wiki_token"}
  ⚠️ 返回 obj_token（文档 ID）和 obj_type（doc/sheet/bitable 等），用对应工具操作内容。

在知识库创建文档：POST /wiki/v2/spaces/:space_id/nodes
  body: {"obj_type": "doc", "parent_node_token": "..."}""",
    },
    "drive": {
        "keywords": ["云空间", "云文档", "文件夹", "drive", "文件列表", "文件权限"],
        "hint": """
## 飞书云空间 API (call_feishu_api)

获取文件列表：GET /drive/v1/files
  query: {"folder_token": "根目录留空", "page_size": 50}

创建文件夹：POST /drive/v1/files/create_folder
  body: {"name": "文件夹名", "folder_token": "父目录 token"}

获取文件元数据：GET /drive/v1/metas/batch_query
  body: {"request_docs": [{"doc_token": "xxx", "doc_type": "doc"}]}

移动文件：POST /drive/v1/files/:file_token/move
  body: {"type": "doc", "folder_token": "目标文件夹"}

⚠️ folder_token 从 URL 提取：feishu.cn/drive/folder/:token""",
    },
    "event": {
        "keywords": ["事件订阅", "webhook", "回调", "event subscription", "实时通知"],
        "hint": """
## 飞书事件订阅 API (call_feishu_api)

获取已订阅事件列表：GET /event/v1/outbound_ip

注意：事件订阅通常在飞书开发者后台配置，不通过 API。
如果用户想监听消息/日程变更等事件，建议在飞书开发者后台的「事件订阅」中配置。""",
    },
    "vc": {
        "keywords": ["视频会议", "会议室", "预定会议室", "video conference", "meeting room"],
        "hint": """
## 飞书视频会议 API (call_feishu_api)

获取会议室列表：GET /vc/v1/rooms
  query: {"page_size": 50}

预定会议室：POST /vc/v1/rooms/batch_book
  body: {"room_ids": ["..."], "time_min": "RFC3339", "time_max": "RFC3339"}

获取会议列表：GET /vc/v1/meetings
  query: {"start_time": "秒时间戳", "end_time": "秒时间戳"}

⚠️ 时间格式混合：有的用 RFC3339，有的用 Unix 时间戳，注意看返回错误调整。""",
    },
}

# 预编译关键词正则（性能优化：避免每次请求重新编译）
_DOMAIN_PATTERNS: dict[str, re.Pattern] = {}
for _domain, _info in _DOMAIN_HINTS.items():
    _kw_pattern = "|".join(re.escape(kw) for kw in _info["keywords"])
    _DOMAIN_PATTERNS[_domain] = re.compile(_kw_pattern, re.IGNORECASE)


def detect_domains(user_text: str) -> list[str]:
    """检测用户消息涉及的飞书业务域（可能多个）。"""
    if not user_text:
        return []
    matched = []
    for domain, pattern in _DOMAIN_PATTERNS.items():
        if pattern.search(user_text):
            matched.append(domain)
    return matched


def get_hints_for_domains(domains: Sequence[str]) -> str:
    """返回指定域的 API 提示文本，拼接为一个字符串。"""
    if not domains:
        return ""
    parts = []
    for domain in domains:
        info = _DOMAIN_HINTS.get(domain)
        if info:
            parts.append(info["hint"])
    if not parts:
        return ""
    header = "\n\n以下是用户可能需要的飞书 API 参考（用 call_feishu_api 工具调用）：\n"
    return header + "\n".join(parts)


def get_domain_hints(user_text: str) -> str:
    """一步到位：检测用户消息 → 返回相关域的 API 提示。

    返回空字符串表示没有命中任何域（不注入）。
    """
    domains = detect_domains(user_text)
    return get_hints_for_domains(domains)
