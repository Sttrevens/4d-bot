"""飞书多维表格（Bitable）工具

提供多维表格的完整 CRUD 能力：
- 查看表格结构（表、字段、视图）
- 查询/搜索记录
- 创建/更新/删除记录
- 新增字段

API 文档: https://open.feishu.cn/document/server-docs/docs/bitable-v1/bitable-overview
"""

from __future__ import annotations

import json
import logging
import re

from app.tools.feishu_api import feishu_get, feishu_post, feishu_put, feishu_delete
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# ── URL / ID 提取 ──

# 多维表格 URL 格式：
#   https://xxx.feishu.cn/base/XxxBitable?table=tblXxx&view=vewXxx
#   https://xxx.feishu.cn/wiki/XxxToken?table=tblXxx
_BITABLE_URL_RE = re.compile(
    r"feishu\.cn/(?:base|wiki)/([A-Za-z0-9]+)"
)
_TABLE_PARAM_RE = re.compile(r"[?&]table=([A-Za-z0-9_]+)")
_VIEW_PARAM_RE = re.compile(r"[?&]view=([A-Za-z0-9_]+)")


def _extract_app_token(token_or_url: str) -> str:
    token_or_url = token_or_url.strip()
    m = _BITABLE_URL_RE.search(token_or_url)
    if m:
        return m.group(1)
    if "?" in token_or_url:
        token_or_url = token_or_url.split("?")[0]
    return token_or_url


def _extract_table_from_url(url: str) -> str:
    m = _TABLE_PARAM_RE.search(url)
    return m.group(1) if m else ""


def _extract_view_from_url(url: str) -> str:
    m = _VIEW_PARAM_RE.search(url)
    return m.group(1) if m else ""


# ── 基础路径 ──

_BASE = "/bitable/v1/apps"


def _app_path(app_token: str) -> str:
    return f"{_BASE}/{app_token}"


def _table_path(app_token: str, table_id: str) -> str:
    return f"{_BASE}/{app_token}/tables/{table_id}"


def _record_path(app_token: str, table_id: str, record_id: str = "") -> str:
    base = f"{_BASE}/{app_token}/tables/{table_id}/records"
    return f"{base}/{record_id}" if record_id else base


# ── 格式化 ──

# 字段类型映射
_FIELD_TYPE_NAMES = {
    1: "文本", 2: "数字", 3: "单选", 4: "多选", 5: "日期",
    7: "复选框", 11: "人员", 13: "电话", 15: "超链接",
    17: "附件", 18: "关联", 19: "查找引用", 20: "公式",
    21: "双向关联", 22: "地理位置", 23: "群组", 1001: "创建时间",
    1002: "修改时间", 1003: "创建人", 1004: "修改人", 1005: "自动编号",
}


def _format_field_value(val) -> str:
    """将字段值转为可读字符串"""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "是" if val else "否"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict):
                # 人员字段 {"name": "xxx"} 或富文本 {"text": "xxx"}
                parts.append(item.get("name") or item.get("text") or str(item))
            else:
                parts.append(str(item))
        return ", ".join(parts)
    if isinstance(val, dict):
        # 超链接 {"link": "...", "text": "..."} 等
        if "text" in val:
            return val["text"]
        if "link" in val:
            return val["link"]
        return json.dumps(val, ensure_ascii=False)
    return str(val)


def _format_record(record: dict, fields_info: list[dict] | None = None) -> str:
    """格式化单条记录为可读文本"""
    record_id = record.get("record_id", "?")
    fields = record.get("fields", {})
    lines = [f"[{record_id}]"]
    for key, val in fields.items():
        lines.append(f"  {key}: {_format_field_value(val)}")
    return "\n".join(lines)


# ── 工具实现 ──


def get_bitable_info(app_token: str) -> ToolResult:
    """获取多维表格应用信息"""
    app_token = _extract_app_token(app_token)
    if not app_token:
        return ToolResult.invalid_param("app_token 不能为空")

    data = feishu_get(_app_path(app_token))
    if isinstance(data, str):
        return ToolResult.api_error(data)

    app = data.get("data", {}).get("app", {})
    name = app.get("name", "未知")
    url = app.get("url", "")
    revision = app.get("revision", 0)
    return ToolResult.success(f"多维表格: {name}\nURL: {url}\n版本: {revision}\napp_token: {app_token}")


def list_bitable_tables(app_token: str) -> ToolResult:
    """列出多维表格中的所有数据表"""
    app_token = _extract_app_token(app_token)
    if not app_token:
        return ToolResult.invalid_param("app_token 不能为空")

    data = feishu_get(f"{_app_path(app_token)}/tables", params={"page_size": 100})
    if isinstance(data, str):
        return ToolResult.api_error(data)

    tables = data.get("data", {}).get("items", [])
    if not tables:
        return ToolResult.success("该多维表格中没有数据表")

    lines = [f"共 {len(tables)} 张数据表:"]
    for t in tables:
        name = t.get("name", "无名")
        tid = t.get("table_id", "?")
        revision = t.get("revision", 0)
        lines.append(f"  - {name} (table_id={tid}, revision={revision})")
    return ToolResult.success("\n".join(lines))


def list_bitable_fields(app_token: str, table_id: str) -> ToolResult:
    """列出数据表的所有字段"""
    app_token = _extract_app_token(app_token)
    if not app_token or not table_id:
        return ToolResult.invalid_param("app_token 和 table_id 不能为空")

    data = feishu_get(
        f"{_table_path(app_token, table_id)}/fields",
        params={"page_size": 100},
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    fields = data.get("data", {}).get("items", [])
    if not fields:
        return ToolResult.success("该数据表没有字段")

    lines = [f"共 {len(fields)} 个字段:"]
    for f in fields:
        name = f.get("field_name", "?")
        ftype = f.get("type", 0)
        type_name = _FIELD_TYPE_NAMES.get(ftype, f"type={ftype}")
        fid = f.get("field_id", "?")
        lines.append(f"  - {name} ({type_name}, field_id={fid})")
    return ToolResult.success("\n".join(lines))


def list_bitable_views(app_token: str, table_id: str) -> ToolResult:
    """列出数据表的所有视图"""
    app_token = _extract_app_token(app_token)
    if not app_token or not table_id:
        return ToolResult.invalid_param("app_token 和 table_id 不能为空")

    data = feishu_get(
        f"{_table_path(app_token, table_id)}/views",
        params={"page_size": 50},
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    views = data.get("data", {}).get("items", [])
    if not views:
        return ToolResult.success("该数据表没有视图")

    lines = [f"共 {len(views)} 个视图:"]
    for v in views:
        name = v.get("view_name", "?")
        vid = v.get("view_id", "?")
        vtype = v.get("view_type", "")
        lines.append(f"  - {name} (view_id={vid}, type={vtype})")
    return ToolResult.success("\n".join(lines))


def list_bitable_records(
    app_token: str,
    table_id: str,
    view_id: str = "",
    filter_expr: str = "",
    sort_expr: str = "",
    page_size: int = 20,
    page_token: str = "",
) -> ToolResult:
    """列出/过滤数据表记录"""
    app_token = _extract_app_token(app_token)
    if not app_token or not table_id:
        return ToolResult.invalid_param("app_token 和 table_id 不能为空")

    params: dict = {"page_size": min(page_size, 100)}
    if view_id:
        params["view_id"] = view_id
    if filter_expr:
        params["filter"] = filter_expr
    if sort_expr:
        params["sort"] = sort_expr
    if page_token:
        params["page_token"] = page_token

    data = feishu_get(_record_path(app_token, table_id), params=params)
    if isinstance(data, str):
        return ToolResult.api_error(data)

    items = data.get("data", {}).get("items", [])
    total = data.get("data", {}).get("total", len(items))
    has_more = data.get("data", {}).get("has_more", False)
    next_token = data.get("data", {}).get("page_token", "")

    if not items:
        return ToolResult.success("没有匹配的记录")

    lines = [f"共 {total} 条记录（当前返回 {len(items)} 条）:"]
    for rec in items:
        lines.append(_format_record(rec))
    if has_more and next_token:
        lines.append(f"\n还有更多记录，page_token={next_token}")
    return ToolResult.success("\n".join(lines))


def search_bitable_records(
    app_token: str,
    table_id: str,
    filter_obj: dict | None = None,
    sort_obj: list | None = None,
    field_names: list | None = None,
    page_size: int = 20,
    page_token: str = "",
) -> ToolResult:
    """高级搜索记录（POST /search，支持复杂筛选条件）"""
    app_token = _extract_app_token(app_token)
    if not app_token or not table_id:
        return ToolResult.invalid_param("app_token 和 table_id 不能为空")

    body: dict = {"page_size": min(page_size, 100)}
    if filter_obj:
        body["filter"] = filter_obj
    if sort_obj:
        body["sort"] = sort_obj
    if field_names:
        body["field_names"] = field_names
    if page_token:
        body["page_token"] = page_token

    data = feishu_post(
        f"{_record_path(app_token, table_id)}/search",
        json=body,
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    items = data.get("data", {}).get("items", [])
    total = data.get("data", {}).get("total", len(items))
    has_more = data.get("data", {}).get("has_more", False)
    next_token = data.get("data", {}).get("page_token", "")

    if not items:
        return ToolResult.success("没有匹配的记录")

    lines = [f"搜索到 {total} 条记录（当前返回 {len(items)} 条）:"]
    for rec in items:
        lines.append(_format_record(rec))
    if has_more and next_token:
        lines.append(f"\n还有更多记录，page_token={next_token}")
    return ToolResult.success("\n".join(lines))


def create_bitable_record(
    app_token: str,
    table_id: str,
    fields: dict,
) -> ToolResult:
    """创建一条记录"""
    app_token = _extract_app_token(app_token)
    if not app_token or not table_id:
        return ToolResult.invalid_param("app_token 和 table_id 不能为空")
    if not fields:
        return ToolResult.invalid_param("fields 不能为空")

    data = feishu_post(
        _record_path(app_token, table_id),
        json={"fields": fields},
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    record = data.get("data", {}).get("record", {})
    rid = record.get("record_id", "?")
    return ToolResult.success(f"记录已创建，record_id={rid}\n{_format_record(record)}")


def batch_create_bitable_records(
    app_token: str,
    table_id: str,
    records: list[dict],
) -> ToolResult:
    """批量创建记录（每条是 {"fields": {...}}）"""
    app_token = _extract_app_token(app_token)
    if not app_token or not table_id:
        return ToolResult.invalid_param("app_token 和 table_id 不能为空")
    if not records:
        return ToolResult.invalid_param("records 列表不能为空")
    if len(records) > 500:
        return ToolResult.invalid_param("单次最多创建 500 条记录")

    data = feishu_post(
        f"{_record_path(app_token, table_id)}/batch_create",
        json={"records": records},
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    created = data.get("data", {}).get("records", [])
    return ToolResult.success(f"成功创建 {len(created)} 条记录")


def update_bitable_record(
    app_token: str,
    table_id: str,
    record_id: str,
    fields: dict,
) -> ToolResult:
    """更新一条记录"""
    app_token = _extract_app_token(app_token)
    if not app_token or not table_id or not record_id:
        return ToolResult.invalid_param("app_token、table_id、record_id 不能为空")
    if not fields:
        return ToolResult.invalid_param("fields 不能为空")

    data = feishu_put(
        _record_path(app_token, table_id, record_id),
        json={"fields": fields},
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    record = data.get("data", {}).get("record", {})
    return ToolResult.success(f"记录已更新\n{_format_record(record)}")


def delete_bitable_record(
    app_token: str,
    table_id: str,
    record_id: str,
) -> ToolResult:
    """删除一条记录"""
    app_token = _extract_app_token(app_token)
    if not app_token or not table_id or not record_id:
        return ToolResult.invalid_param("app_token、table_id、record_id 不能为空")

    data = feishu_delete(_record_path(app_token, table_id, record_id))
    if isinstance(data, str):
        return ToolResult.api_error(data)

    return ToolResult.success(f"记录 {record_id} 已删除")


def create_bitable_field(
    app_token: str,
    table_id: str,
    field_name: str,
    field_type: int,
    property_obj: dict | None = None,
) -> ToolResult:
    """新增一个字段"""
    app_token = _extract_app_token(app_token)
    if not app_token or not table_id or not field_name:
        return ToolResult.invalid_param("app_token、table_id、field_name 不能为空")

    body: dict = {"field_name": field_name, "type": field_type}
    if property_obj:
        body["property"] = property_obj

    data = feishu_post(
        f"{_table_path(app_token, table_id)}/fields",
        json=body,
    )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    field = data.get("data", {}).get("field", {})
    fid = field.get("field_id", "?")
    return ToolResult.success(f"字段已创建: {field_name} (field_id={fid})")


def create_bitable_table(
    app_token: str,
    name: str,
    fields: list[dict] | None = None,
) -> ToolResult:
    """在多维表格中新建一张数据表"""
    app_token = _extract_app_token(app_token)
    if not app_token or not name:
        return ToolResult.invalid_param("app_token 和 name 不能为空")

    body: dict = {"table": {"name": name}}
    if fields:
        body["table"]["fields"] = fields

    data = feishu_post(f"{_app_path(app_token)}/tables", json=body)
    if isinstance(data, str):
        return ToolResult.api_error(data)

    tid = data.get("data", {}).get("table_id", "?")
    return ToolResult.success(f"数据表已创建: {name} (table_id={tid})")


# ── 工具定义 ──

TOOL_DEFINITIONS = [
    {
        "name": "get_bitable_info",
        "description": "获取多维表格的基本信息（名称、URL 等）。传入 app_token 或多维表格链接。",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
            },
            "required": ["app_token"],
        },
    },
    {
        "name": "list_bitable_tables",
        "description": "列出多维表格中的所有数据表。先用这个获取 table_id，再操作具体表。",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
            },
            "required": ["app_token"],
        },
    },
    {
        "name": "list_bitable_fields",
        "description": "列出数据表的所有字段（列）及其类型。写入记录前先用这个了解表结构。",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "table_id": {
                    "type": "string",
                    "description": "数据表 ID（tbl 开头）",
                },
            },
            "required": ["app_token", "table_id"],
        },
    },
    {
        "name": "list_bitable_views",
        "description": "列出数据表的所有视图。",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "table_id": {
                    "type": "string",
                    "description": "数据表 ID",
                },
            },
            "required": ["app_token", "table_id"],
        },
    },
    {
        "name": "list_bitable_records",
        "description": (
            "列出数据表中的记录。支持简单筛选（filter 表达式）和排序。"
            "filter 格式示例: CurrentValue.[字段名]=\"值\""
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "table_id": {
                    "type": "string",
                    "description": "数据表 ID",
                },
                "view_id": {
                    "type": "string",
                    "description": "视图 ID（可选，按某个视图过滤）",
                    "default": "",
                },
                "filter_expr": {
                    "type": "string",
                    "description": "筛选表达式，如 CurrentValue.[状态]=\"进行中\"",
                    "default": "",
                },
                "sort_expr": {
                    "type": "string",
                    "description": "排序表达式",
                    "default": "",
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页记录数（默认20，最大100）",
                    "default": 20,
                },
                "page_token": {
                    "type": "string",
                    "description": "翻页 token（上次返回的 page_token）",
                    "default": "",
                },
            },
            "required": ["app_token", "table_id"],
        },
    },
    {
        "name": "search_bitable_records",
        "description": (
            "高级搜索记录，支持复合筛选条件和排序。"
            "filter 格式: {\"conjunction\": \"and\", \"conditions\": [{\"field_name\": \"字段名\", \"operator\": \"is\", \"value\": [\"值\"]}]}。"
            "operator 支持: is, isNot, contains, doesNotContain, isEmpty, isNotEmpty, isGreater, isLess 等。"
            "sort 格式: [{\"field_name\": \"字段名\", \"desc\": true}]"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "table_id": {
                    "type": "string",
                    "description": "数据表 ID",
                },
                "filter_obj": {
                    "type": "object",
                    "description": "筛选对象，含 conjunction 和 conditions",
                },
                "sort_obj": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "排序数组 [{\"field_name\": \"...\", \"desc\": true}]",
                },
                "field_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只返回指定字段（可选，留空返回全部）",
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页记录数（默认20，最大100）",
                    "default": 20,
                },
                "page_token": {
                    "type": "string",
                    "description": "翻页 token",
                    "default": "",
                },
            },
            "required": ["app_token", "table_id"],
        },
    },
    {
        "name": "create_bitable_record",
        "description": (
            "在数据表中创建一条记录。fields 是字段名到值的映射。"
            "写入前先用 list_bitable_fields 了解表结构和字段名。"
            "文本字段传字符串，数字字段传数字，单选传选项文本，多选传数组，"
            "人员字段传 [{\"id\": \"open_id\"}]，日期字段传毫秒时间戳。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "table_id": {
                    "type": "string",
                    "description": "数据表 ID",
                },
                "fields": {
                    "type": "object",
                    "description": "字段名到值的映射，如 {\"标题\": \"新任务\", \"状态\": \"待处理\", \"优先级\": 1}",
                },
            },
            "required": ["app_token", "table_id", "fields"],
        },
    },
    {
        "name": "batch_create_bitable_records",
        "description": "批量创建记录（最多500条）。每条格式: {\"fields\": {\"字段名\": \"值\"}}",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "table_id": {
                    "type": "string",
                    "description": "数据表 ID",
                },
                "records": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "记录数组，每项 {\"fields\": {\"字段名\": \"值\"}}",
                },
            },
            "required": ["app_token", "table_id", "records"],
        },
    },
    {
        "name": "update_bitable_record",
        "description": "更新一条记录。只需传要修改的字段，未传的字段不变。",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "table_id": {
                    "type": "string",
                    "description": "数据表 ID",
                },
                "record_id": {
                    "type": "string",
                    "description": "记录 ID（rec 开头，从 list/search 结果中获取）",
                },
                "fields": {
                    "type": "object",
                    "description": "要更新的字段名到新值的映射",
                },
            },
            "required": ["app_token", "table_id", "record_id", "fields"],
        },
    },
    {
        "name": "delete_bitable_record",
        "description": "删除一条记录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "table_id": {
                    "type": "string",
                    "description": "数据表 ID",
                },
                "record_id": {
                    "type": "string",
                    "description": "记录 ID",
                },
            },
            "required": ["app_token", "table_id", "record_id"],
        },
    },
    {
        "name": "create_bitable_field",
        "description": (
            "给数据表新增一个字段（列）。"
            "常用 field_type: 1=文本, 2=数字, 3=单选, 4=多选, 5=日期, 7=复选框, 11=人员。"
            "单选/多选需传 property: {\"options\": [{\"name\": \"选项1\"}, {\"name\": \"选项2\"}]}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "table_id": {
                    "type": "string",
                    "description": "数据表 ID",
                },
                "field_name": {
                    "type": "string",
                    "description": "字段名称",
                },
                "field_type": {
                    "type": "integer",
                    "description": "字段类型（1=文本,2=数字,3=单选,4=多选,5=日期,7=复选框,11=人员）",
                },
                "property_obj": {
                    "type": "object",
                    "description": "字段属性（单选/多选的 options 等），可选",
                },
            },
            "required": ["app_token", "table_id", "field_name", "field_type"],
        },
    },
    {
        "name": "create_bitable_table",
        "description": (
            "在多维表格中新建一张数据表。"
            "可选传 fields 定义初始字段: [{\"field_name\": \"名称\", \"type\": 1}]"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_token": {
                    "type": "string",
                    "description": "多维表格的 app_token 或完整链接",
                },
                "name": {
                    "type": "string",
                    "description": "数据表名称",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "初始字段定义数组（可选）",
                },
            },
            "required": ["app_token", "name"],
        },
    },
]

# ── 工具映射 ──

TOOL_MAP = {
    "get_bitable_info": lambda args: get_bitable_info(
        app_token=args["app_token"],
    ),
    "list_bitable_tables": lambda args: list_bitable_tables(
        app_token=args["app_token"],
    ),
    "list_bitable_fields": lambda args: list_bitable_fields(
        app_token=args["app_token"],
        table_id=args["table_id"],
    ),
    "list_bitable_views": lambda args: list_bitable_views(
        app_token=args["app_token"],
        table_id=args["table_id"],
    ),
    "list_bitable_records": lambda args: list_bitable_records(
        app_token=args["app_token"],
        table_id=args["table_id"],
        view_id=args.get("view_id", ""),
        filter_expr=args.get("filter_expr", ""),
        sort_expr=args.get("sort_expr", ""),
        page_size=args.get("page_size", 20),
        page_token=args.get("page_token", ""),
    ),
    "search_bitable_records": lambda args: search_bitable_records(
        app_token=args["app_token"],
        table_id=args["table_id"],
        filter_obj=args.get("filter_obj"),
        sort_obj=args.get("sort_obj"),
        field_names=args.get("field_names"),
        page_size=args.get("page_size", 20),
        page_token=args.get("page_token", ""),
    ),
    "create_bitable_record": lambda args: create_bitable_record(
        app_token=args["app_token"],
        table_id=args["table_id"],
        fields=args["fields"],
    ),
    "batch_create_bitable_records": lambda args: batch_create_bitable_records(
        app_token=args["app_token"],
        table_id=args["table_id"],
        records=args["records"],
    ),
    "update_bitable_record": lambda args: update_bitable_record(
        app_token=args["app_token"],
        table_id=args["table_id"],
        record_id=args["record_id"],
        fields=args["fields"],
    ),
    "delete_bitable_record": lambda args: delete_bitable_record(
        app_token=args["app_token"],
        table_id=args["table_id"],
        record_id=args["record_id"],
    ),
    "create_bitable_field": lambda args: create_bitable_field(
        app_token=args["app_token"],
        table_id=args["table_id"],
        field_name=args["field_name"],
        field_type=args["field_type"],
        property_obj=args.get("property_obj"),
    ),
    "create_bitable_table": lambda args: create_bitable_table(
        app_token=args["app_token"],
        name=args["name"],
        fields=args.get("fields"),
    ),
}
