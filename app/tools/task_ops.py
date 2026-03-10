"""飞书任务操作工具

通过飞书 Task v2 API 管理任务：
- 创建任务
- 查询任务列表
- 完成/重开任务

使用用户 OAuth token 访问用户的个人任务。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta

from app.tools.feishu_api import (
    feishu_get, feishu_post, feishu_patch, feishu_delete,
    has_user_token,
)
from app.tools._fuzzy import fuzzy_filter
from app.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

# 用户时区（中国标准时间 UTC+8）
_CN_TZ = timezone(timedelta(hours=8))


def _has_user() -> bool:
    """当前用户是否有 OAuth token（含 task:task:write scope）"""
    return has_user_token()


def _parse_timestamp(time_str: str) -> str:
    """解析用户时间为 Unix 毫秒时间戳字符串（飞书 Task v2 用毫秒）
    用户输入视为 UTC+8（中国时区）。
    """
    time_str = time_str.strip()
    if time_str.isdigit() and len(time_str) >= 13:
        return time_str  # 已经是毫秒
    if time_str.isdigit() and len(time_str) >= 10:
        return str(int(time_str) * 1000)  # 秒 → 毫秒
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(time_str, fmt)
            # 视为中国时间（UTC+8），转为 UTC epoch 毫秒
            aware = naive.replace(tzinfo=_CN_TZ)
            return str(int(aware.timestamp() * 1000))
        except ValueError:
            continue
    return ""


def _ts_to_display(ts: str) -> str:
    """时间戳 → 可读（自动处理秒/毫秒，显示为 UTC+8）"""
    if not ts or ts == "0":
        return ""
    try:
        val = int(ts)
        # 超过 1e12 认为是毫秒，转为秒
        if val > 1_000_000_000_000:
            val = val // 1000
        dt = datetime.fromtimestamp(val, tz=_CN_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return ts


def create_task(
    summary: str,
    description: str = "",
    due_time: str = "",
    assignee_open_ids: list[str] | None = None,
) -> ToolResult:
    """创建飞书任务，然后单独添加负责人"""
    use_user = _has_user()
    body: dict = {"summary": summary}

    if description:
        body["description"] = description

    if due_time:
        ts = _parse_timestamp(due_time)
        if ts:
            body["due"] = {"timestamp": ts, "is_all_day": False}

    # 先创建任务（不含 members，兼容性更好）
    data = feishu_post(
        "/task/v2/tasks",
        json=body,
        params={"user_id_type": "open_id"},
        use_user_token=use_user,
    )
    if isinstance(data, str) and use_user:
        data = feishu_post(
            "/task/v2/tasks",
            json=body,
            params={"user_id_type": "open_id"},
            use_user_token=False,
        )
        use_user = False
    if isinstance(data, str):
        return ToolResult.api_error(data)

    task = data.get("data", {}).get("task", {})
    guid = task.get("guid", "")
    result = f"任务已创建: {summary}\nID: {guid}"

    if due_time:
        result += f"\n截止: {due_time}"

    # 创建后再单独添加负责人
    if assignee_open_ids and guid:
        assigned = 0
        for uid in assignee_open_ids:
            member_data = feishu_post(
                f"/task/v2/tasks/{guid}/add_members",
                json={"members": [{"id": uid, "type": "user", "role": "assignee"}]},
                params={"user_id_type": "open_id"},
                use_user_token=use_user,
            )
            if isinstance(member_data, str):
                logger.warning("add member %s to task %s failed: %s", uid, guid, member_data[:80])
            else:
                assigned += 1
        result += f"\n已分配给 {assigned}/{len(assignee_open_ids)} 人"

    return ToolResult.success(result)


def _fetch_task_pages(completed: str, page_size: int, use_user_token: bool) -> list[dict]:
    """内部: 用指定 token 类型翻页获取全部任务"""
    items: list[dict] = []
    page_token = ""
    for _ in range(10):
        params: dict = {"page_size": min(page_size, 100), "user_id_type": "open_id"}
        if completed:
            params["completed"] = completed
        if page_token:
            params["page_token"] = page_token
        data = feishu_get("/task/v2/tasks", params=params, use_user_token=use_user_token)
        if isinstance(data, str):
            logger.warning("_fetch_task_pages(user=%s) failed: %s", use_user_token, data[:80])
            break
        page_items = data.get("data", {}).get("items", [])
        items.extend(page_items)
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break
    return items


def list_tasks(page_size: int = 100, completed: str = "", keyword: str = "") -> ToolResult:
    """查询任务列表（自动翻页，获取全部任务）。

    keyword 参数支持按任务标题模糊过滤。
    重要：如果关键词没匹配到任何任务，会返回全部任务列表（而不是空结果），
    这样 LLM 可以直接从列表中查找，避免反复尝试不同关键词。
    """
    use_user = _has_user()
    all_items = _fetch_task_pages(completed, page_size, use_user_token=use_user)

    # user token 没有结果时，尝试另一种 token 兜底
    if not all_items and use_user:
        all_items = _fetch_task_pages(completed, page_size, use_user_token=False)

    logger.info("list_tasks: %d tasks fetched (user_token=%s)", len(all_items), use_user)

    if not all_items:
        return ToolResult.success("没有找到任务。" + (f"（关键词: {keyword}）" if keyword else ""))

    # 模糊关键词过滤
    no_match_hint = ""
    total_count = len(all_items)
    if keyword:
        filtered = fuzzy_filter(all_items, keyword, ["summary"])
        if filtered:
            all_items = filtered
        else:
            # 关键词没匹配到任何任务 — 打印诊断日志，然后返回全部任务
            sample = [t.get("summary", "??")[:60] for t in all_items[:8]]
            logger.warning(
                "list_tasks: keyword='%s' matched 0/%d tasks. Sample summaries: %s",
                keyword, total_count, sample,
            )
            no_match_hint = (
                f"关键词「{keyword}」没有匹配到任何任务标题。"
                f"以下是全部 {total_count} 个任务，请直接从中查找目标任务：\n"
            )

    header = no_match_hint or (
        f"找到 {len(all_items)} 个任务"
        + (f"（关键词: {keyword}）" if keyword else "")
        + "：\n"
    )
    lines = [header]
    for task in all_items:
        summary = task.get("summary", "(无标题)")
        guid = task.get("guid", "")
        completed_at = task.get("completed_at", "0")
        status = "已完成" if completed_at and completed_at != "0" else "进行中"

        due = task.get("due", {})
        due_str = ""
        if due and due.get("timestamp"):
            due_str = f"  截止: {_ts_to_display(due['timestamp'])}"

        # 负责人
        members = task.get("members", [])
        assignees = [m.get("name", m.get("id", "")) for m in members if m.get("role") == "assignee"]
        assignee_str = f"  负责: {', '.join(assignees)}" if assignees else ""

        # 子任务数量
        subtask_count = task.get("subtask_count", 0)
        subtask_str = f"  子任务: {subtask_count}" if subtask_count else ""

        # 父任务
        parent = task.get("parent_task_guid", "")
        parent_str = f"  父任务: {parent}" if parent else ""

        lines.append(f"  [{status}] {summary} (ID: {guid}){due_str}{assignee_str}{subtask_str}{parent_str}")

    return ToolResult.success("\n".join(lines))


def complete_task(task_guid: str) -> ToolResult:
    """完成任务"""
    use_user = _has_user()
    ts = str(int(time.time() * 1000))  # 毫秒
    body = {
        "task": {"completed_at": ts},
        "update_fields": ["completed_at"],
    }
    data = feishu_patch(
        f"/task/v2/tasks/{task_guid}",
        json=body,
        params={"user_id_type": "open_id"},
        use_user_token=use_user,
    )
    if isinstance(data, str) and use_user:
        data = feishu_patch(
            f"/task/v2/tasks/{task_guid}",
            json=body,
            params={"user_id_type": "open_id"},
            use_user_token=False,
        )
    if isinstance(data, str):
        return ToolResult.api_error(data)
    return ToolResult.success(f"任务 {task_guid} 已标记为完成")


def reopen_task(task_guid: str) -> ToolResult:
    """重新打开任务"""
    use_user = _has_user()
    body = {
        "task": {"completed_at": "0"},
        "update_fields": ["completed_at"],
    }
    data = feishu_patch(
        f"/task/v2/tasks/{task_guid}",
        json=body,
        params={"user_id_type": "open_id"},
        use_user_token=use_user,
    )
    if isinstance(data, str) and use_user:
        data = feishu_patch(
            f"/task/v2/tasks/{task_guid}",
            json=body,
            params={"user_id_type": "open_id"},
            use_user_token=False,
        )
    if isinstance(data, str):
        return ToolResult.api_error(data)
    return ToolResult.success(f"任务 {task_guid} 已重新打开")


def update_task(
    task_guid: str,
    summary: str = "",
    description: str = "",
    due_time: str = "",
    assignee_open_ids: list[str] | None = None,
) -> ToolResult:
    """修改飞书任务的属性"""
    use_user = _has_user()
    task_body: dict = {}
    update_fields: list[str] = []

    if summary:
        task_body["summary"] = summary
        update_fields.append("summary")
    if description:
        task_body["description"] = description
        update_fields.append("description")
    if due_time:
        ts = _parse_timestamp(due_time)
        if ts:
            task_body["due"] = {"timestamp": ts, "is_all_day": False}
            update_fields.append("due")

    if not update_fields and not assignee_open_ids:
        return ToolResult.invalid_param("至少要指定一项要修改的内容（标题/描述/截止时间/负责人）")

    if update_fields:
        body = {
            "task": task_body,
            "update_fields": update_fields,
        }
        data = feishu_patch(
            f"/task/v2/tasks/{task_guid}",
            json=body,
            params={"user_id_type": "open_id"},
            use_user_token=use_user,
        )
        if isinstance(data, str) and use_user:
            data = feishu_patch(
                f"/task/v2/tasks/{task_guid}",
                json=body,
                params={"user_id_type": "open_id"},
                use_user_token=False,
            )
        if isinstance(data, str):
            return ToolResult.api_error(data)

    assigned = 0
    if assignee_open_ids:
        for uid in assignee_open_ids:
            member_data = feishu_post(
                f"/task/v2/tasks/{task_guid}/add_members",
                json={"members": [{"id": uid, "type": "user", "role": "assignee"}]},
                params={"user_id_type": "open_id"},
                use_user_token=use_user if use_user else False,
            )
            if isinstance(member_data, str):
                logger.warning("add member %s to task failed: %s", uid, member_data[:80])
            else:
                assigned += 1

    parts = []
    if summary:
        parts.append(f"标题→{summary}")
    if description:
        parts.append("描述已更新")
    if due_time:
        parts.append(f"截止→{due_time}")
    if assignee_open_ids:
        if assigned == len(assignee_open_ids):
            parts.append(f"已分配给 {assigned} 人")
        else:
            parts.append(f"分配负责人 {assigned}/{len(assignee_open_ids)} 成功")
    return ToolResult.success(f"任务 {task_guid} 已更新: {', '.join(parts)}")


def delete_task(task_guid: str) -> ToolResult:
    """删除飞书任务"""
    use_user = _has_user()
    data = feishu_delete(
        f"/task/v2/tasks/{task_guid}",
        use_user_token=use_user,
    )
    if isinstance(data, str) and use_user:
        data = feishu_delete(
            f"/task/v2/tasks/{task_guid}",
            use_user_token=False,
        )
    if isinstance(data, str):
        return ToolResult.api_error(data)
    return ToolResult.success(f"任务 {task_guid} 已删除")


def create_subtask(
    parent_task_guid: str,
    summary: str,
    description: str = "",
    due_time: str = "",
    assignee_open_ids: list[str] | None = None,
) -> ToolResult:
    """在父任务下创建子任务，然后单独添加负责人"""
    use_user = _has_user()
    body: dict = {"summary": summary}

    if description:
        body["description"] = description
    if due_time:
        ts = _parse_timestamp(due_time)
        if ts:
            body["due"] = {"timestamp": ts, "is_all_day": False}

    # 先创建子任务（不含 members）
    data = feishu_post(
        f"/task/v2/tasks/{parent_task_guid}/subtasks",
        json=body,
        params={"user_id_type": "open_id"},
        use_user_token=use_user,
    )
    if isinstance(data, str) and use_user:
        data = feishu_post(
            f"/task/v2/tasks/{parent_task_guid}/subtasks",
            json=body,
            params={"user_id_type": "open_id"},
            use_user_token=False,
        )
        use_user = False
    if isinstance(data, str):
        return ToolResult.api_error(data)

    subtask = data.get("data", {}).get("subtask", {})
    guid = subtask.get("guid", "")
    result = f"子任务已创建: {summary}\nID: {guid}\n父任务: {parent_task_guid}"

    # 创建后再单独添加负责人
    if assignee_open_ids and guid:
        assigned = 0
        for uid in assignee_open_ids:
            member_data = feishu_post(
                f"/task/v2/tasks/{guid}/add_members",
                json={"members": [{"id": uid, "type": "user", "role": "assignee"}]},
                params={"user_id_type": "open_id"},
                use_user_token=use_user,
            )
            if isinstance(member_data, str):
                logger.warning("add member %s to subtask %s failed: %s", uid, guid, member_data[:80])
            else:
                assigned += 1
        result += f"\n已分配给 {assigned}/{len(assignee_open_ids)} 人"

    return ToolResult.success(result)


def get_task(task_guid: str) -> ToolResult:
    """获取单个任务的详细信息（含子任务数、父任务、清单归属）"""
    use_user = _has_user()
    data = feishu_get(
        f"/task/v2/tasks/{task_guid}",
        params={"user_id_type": "open_id"},
        use_user_token=use_user,
    )
    if isinstance(data, str) and use_user:
        data = feishu_get(
            f"/task/v2/tasks/{task_guid}",
            params={"user_id_type": "open_id"},
            use_user_token=False,
        )
    if isinstance(data, str):
        return ToolResult.api_error(data)

    task = data.get("data", {}).get("task", {})
    if not task:
        return ToolResult.not_found(f"任务 {task_guid} 不存在或无权访问。")

    summary = task.get("summary", "(无标题)")
    completed_at = task.get("completed_at", "0")
    status = "已完成" if completed_at and completed_at != "0" else "进行中"

    lines = [f"任务详情: {summary}", f"  ID: {task.get('guid', '')}", f"  状态: {status}"]

    desc = task.get("description", "")
    if desc:
        lines.append(f"  描述: {desc}")

    due = task.get("due", {})
    if due and due.get("timestamp"):
        lines.append(f"  截止: {_ts_to_display(due['timestamp'])}")

    members = task.get("members", [])
    assignees = [m.get("name", m.get("id", "")) for m in members if m.get("role") == "assignee"]
    if assignees:
        lines.append(f"  负责: {', '.join(assignees)}")

    parent = task.get("parent_task_guid", "")
    if parent:
        lines.append(f"  父任务ID: {parent}")

    subtask_count = task.get("subtask_count", 0)
    lines.append(f"  子任务数: {subtask_count}")

    tasklists = task.get("tasklists", [])
    if tasklists:
        tl_names = [tl.get("tasklist_guid", "") for tl in tasklists]
        lines.append(f"  所属清单: {', '.join(tl_names)}")

    url = task.get("url", "")
    if url:
        lines.append(f"  链接: {url}")

    return ToolResult.success("\n".join(lines))


def _fetch_subtask_pages(parent_guid: str, page_size: int, use_user_token: bool) -> list[dict]:
    """内部: 用指定 token 类型翻页获取子任务"""
    items: list[dict] = []
    page_token = ""
    for _ in range(10):
        params: dict = {"page_size": min(page_size, 100), "user_id_type": "open_id"}
        if page_token:
            params["page_token"] = page_token
        data = feishu_get(
            f"/task/v2/tasks/{parent_guid}/subtasks",
            params=params,
            use_user_token=use_user_token,
        )
        if isinstance(data, str):
            break
        page_items = data.get("data", {}).get("items", [])
        items.extend(page_items)
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break
    return items


def list_subtasks(parent_task_guid: str, page_size: int = 50, keyword: str = "") -> ToolResult:
    """列出某个任务的所有子任务。
    keyword 未匹配时返回全部子任务列表以供查找。
    """
    use_user = _has_user()
    all_items = _fetch_subtask_pages(parent_task_guid, page_size, use_user_token=use_user)

    # user token 没有结果时，尝试 bot token 兜底
    if not all_items and use_user:
        all_items = _fetch_subtask_pages(parent_task_guid, page_size, use_user_token=False)

    if not all_items:
        return ToolResult.success(f"任务 {parent_task_guid} 没有子任务。")

    # 模糊关键词过滤
    no_match_hint = ""
    total_count = len(all_items)
    if keyword:
        filtered = fuzzy_filter(all_items, keyword, ["summary"])
        if filtered:
            all_items = filtered
        else:
            sample = [t.get("summary", "??")[:60] for t in all_items[:5]]
            logger.warning(
                "list_subtasks: keyword='%s' matched 0/%d. Samples: %s",
                keyword, total_count, sample,
            )
            no_match_hint = (
                f"关键词「{keyword}」没有匹配到子任务标题。"
                f"以下是全部 {total_count} 个子任务：\n"
            )

    header = no_match_hint or (
        f"任务 {parent_task_guid} 有 {len(all_items)} 个子任务"
        + (f"（关键词: {keyword}）" if keyword else "")
        + "：\n"
    )
    lines = [header]
    for task in all_items:
        summary = task.get("summary", "(无标题)")
        guid = task.get("guid", "")
        completed_at = task.get("completed_at", "0")
        status = "已完成" if completed_at and completed_at != "0" else "进行中"

        due = task.get("due", {})
        due_str = ""
        if due and due.get("timestamp"):
            due_str = f"  截止: {_ts_to_display(due['timestamp'])}"

        lines.append(f"  [{status}] {summary} (ID: {guid}){due_str}")

    return ToolResult.success("\n".join(lines))


def list_tasklists(page_size: int = 50, keyword: str = "") -> ToolResult:
    """查询用户可见的任务清单列表，支持 keyword 模糊过滤"""
    use_user = _has_user()
    params: dict = {"page_size": page_size, "user_id_type": "open_id"}

    data = feishu_get("/task/v2/tasklists", params=params, use_user_token=use_user)
    if isinstance(data, str) and use_user:
        data = feishu_get("/task/v2/tasklists", params=params, use_user_token=False)
    if isinstance(data, str):
        return ToolResult.api_error(data)

    items = data.get("data", {}).get("items", [])
    if keyword:
        items = fuzzy_filter(items, keyword, ["name"])

    if not items:
        if keyword:
            return ToolResult.success(f"没有找到匹配「{keyword}」的任务清单。")
        return ToolResult.success("没有找到任务清单。")

    lines = [f"找到 {len(items)} 个任务清单" + (f"（关键词: {keyword}）" if keyword else "") + "：\n"]
    for tl in items:
        name = tl.get("name", "(无名)")
        guid = tl.get("guid", "")
        creator = tl.get("creator", {})
        creator_name = creator.get("name", "")
        lines.append(f"  {name} (ID: {guid})" + (f"  创建者: {creator_name}" if creator_name else ""))
    return ToolResult.success("\n".join(lines))


def list_tasklist_tasks(
    tasklist_guid: str,
    page_size: int = 100,
    keyword: str = "",
    completed: str = "",
) -> ToolResult:
    """列出某个任务清单内的所有任务（按清单维度查看）。

    与 list_tasks（个人任务列表）不同，这里列出的是清单里的所有任务，
    不管任务是否分配给了当前用户。
    """
    use_user = _has_user()
    all_items: list[dict] = []
    page_token = ""

    for _ in range(10):
        params: dict = {"page_size": min(page_size, 100), "user_id_type": "open_id"}
        if completed:
            params["completed"] = completed
        if page_token:
            params["page_token"] = page_token

        data = feishu_get(
            f"/task/v2/tasklists/{tasklist_guid}/tasks",
            params=params,
            use_user_token=use_user,
        )
        if isinstance(data, str) and use_user:
            data = feishu_get(
                f"/task/v2/tasklists/{tasklist_guid}/tasks",
                params=params,
                use_user_token=False,
            )
        if isinstance(data, str):
            return ToolResult.api_error(data)

        page_items = data.get("data", {}).get("items", [])
        all_items.extend(page_items)
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break

    logger.info("list_tasklist_tasks(%s): %d tasks fetched", tasklist_guid[:8], len(all_items))

    if not all_items:
        return ToolResult.success(f"任务清单 {tasklist_guid} 中没有任务。")

    # 模糊关键词过滤
    no_match_hint = ""
    total_count = len(all_items)
    if keyword:
        filtered = fuzzy_filter(all_items, keyword, ["summary"])
        if filtered:
            all_items = filtered
        else:
            sample = [t.get("summary", "??")[:60] for t in all_items[:8]]
            logger.warning(
                "list_tasklist_tasks: keyword='%s' matched 0/%d. Samples: %s",
                keyword, total_count, sample,
            )
            no_match_hint = (
                f"关键词「{keyword}」没有匹配到清单内的任务。"
                f"以下是清单内全部 {total_count} 个任务：\n"
            )

    header = no_match_hint or (
        f"清单内找到 {len(all_items)} 个任务"
        + (f"（关键词: {keyword}）" if keyword else "")
        + "：\n"
    )
    lines = [header]
    for task in all_items:
        summary = task.get("summary", "(无标题)")
        guid = task.get("guid", "")
        completed_at = task.get("completed_at", "0")
        status = "已完成" if completed_at and completed_at != "0" else "进行中"

        due = task.get("due", {})
        due_str = ""
        if due and due.get("timestamp"):
            due_str = f"  截止: {_ts_to_display(due['timestamp'])}"

        members = task.get("members", [])
        assignees = [m.get("name", m.get("id", "")) for m in members if m.get("role") == "assignee"]
        assignee_str = f"  负责: {', '.join(assignees)}" if assignees else ""

        subtask_count = task.get("subtask_count", 0)
        subtask_str = f"  子任务: {subtask_count}" if subtask_count else ""

        lines.append(f"  [{status}] {summary} (ID: {guid}){due_str}{assignee_str}{subtask_str}")

    return ToolResult.success("\n".join(lines))


def add_task_to_tasklist(task_guid: str, tasklist_guid: str, section_guid: str = "") -> ToolResult:
    """把任务添加到指定的任务清单"""
    use_user = _has_user()
    body: dict = {"tasklist_guid": tasklist_guid}
    if section_guid:
        body["section_guid"] = section_guid

    data = feishu_post(
        f"/task/v2/tasks/{task_guid}/add_tasklist",
        json=body,
        use_user_token=use_user,
    )
    if isinstance(data, str) and use_user:
        data = feishu_post(
            f"/task/v2/tasks/{task_guid}/add_tasklist",
            json=body,
            use_user_token=False,
        )
    if isinstance(data, str):
        return ToolResult.api_error(data)
    return ToolResult.success(f"任务 {task_guid} 已添加到清单 {tasklist_guid}")


def remove_task_from_tasklist(task_guid: str, tasklist_guid: str) -> ToolResult:
    """从任务清单中移除任务"""
    use_user = _has_user()
    body: dict = {"tasklist_guid": tasklist_guid}

    data = feishu_post(
        f"/task/v2/tasks/{task_guid}/remove_tasklist",
        json=body,
        use_user_token=use_user,
    )
    if isinstance(data, str) and use_user:
        data = feishu_post(
            f"/task/v2/tasks/{task_guid}/remove_tasklist",
            json=body,
            use_user_token=False,
        )
    if isinstance(data, str):
        return ToolResult.api_error(data)
    return ToolResult.success(f"任务 {task_guid} 已从清单 {tasklist_guid} 移除")


# --------------- Tool definitions & map ---------------

TOOL_DEFINITIONS = [
    {
        "name": "create_feishu_task",
        "description": "创建飞书任务。可指定标题、描述、截止时间、负责人。",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "任务标题",
                },
                "description": {
                    "type": "string",
                    "description": "任务描述",
                    "default": "",
                },
                "due_time": {
                    "type": "string",
                    "description": "截止时间，格式: 'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DD'",
                    "default": "",
                },
                "assignee_open_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "负责人的 open_id 列表",
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "list_feishu_tasks",
        "description": "查询飞书任务列表（自动翻页获取全部）。支持按关键词过滤任务标题，强烈建议用 keyword 缩小范围。",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "按任务标题过滤的关键词（不区分大小写），如 'L8' 或 'Justin'。强烈建议使用以缩小结果范围。",
                    "default": "",
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页数量（默认100，自动翻页获取全部）",
                    "default": 100,
                },
                "completed": {
                    "type": "string",
                    "description": "筛选: 'true' 只看已完成，'false' 只看未完成，空字符串看全部",
                    "default": "",
                },
            },
        },
    },
    {
        "name": "complete_feishu_task",
        "description": "完成一个飞书任务。需要任务 ID（可先用 list_feishu_tasks 查到）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_guid": {
                    "type": "string",
                    "description": "任务 ID (guid)",
                },
            },
            "required": ["task_guid"],
        },
    },
    {
        "name": "reopen_feishu_task",
        "description": "重新打开一个已完成的飞书任务。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_guid": {
                    "type": "string",
                    "description": "任务 ID (guid)",
                },
            },
            "required": ["task_guid"],
        },
    },
    {
        "name": "update_feishu_task",
        "description": "修改飞书任务的属性（标题、描述、截止时间、负责人）。需要任务 ID。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_guid": {
                    "type": "string",
                    "description": "任务 ID (guid)",
                },
                "summary": {
                    "type": "string",
                    "description": "新的任务标题（不改就不传）",
                },
                "description": {
                    "type": "string",
                    "description": "新的任务描述（不改就不传）",
                },
                "due_time": {
                    "type": "string",
                    "description": "新的截止时间，格式: 'YYYY-MM-DD HH:MM'（不改就不传）",
                },
                "assignee_open_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要新增的负责人 open_id 列表",
                },
            },
            "required": ["task_guid"],
        },
    },
    {
        "name": "delete_feishu_task",
        "description": "删除一个飞书任务。需要任务 ID（可先用 list_feishu_tasks 查到）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_guid": {
                    "type": "string",
                    "description": "任务 ID (guid)",
                },
            },
            "required": ["task_guid"],
        },
    },
    {
        "name": "list_feishu_tasklists",
        "description": "查询用户可见的飞书任务清单列表。支持 keyword 模糊过滤清单名称。",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "按清单名称模糊过滤",
                    "default": "",
                },
                "page_size": {
                    "type": "integer",
                    "description": "返回数量，默认 50",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "add_task_to_tasklist",
        "description": "把任务添加到指定的任务清单。需要任务 ID 和清单 ID（可先用 list_feishu_tasklists 查到）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_guid": {
                    "type": "string",
                    "description": "任务 ID (guid)",
                },
                "tasklist_guid": {
                    "type": "string",
                    "description": "目标任务清单 ID",
                },
                "section_guid": {
                    "type": "string",
                    "description": "清单内的分组 ID（可选，不传则加到默认分组）",
                    "default": "",
                },
            },
            "required": ["task_guid", "tasklist_guid"],
        },
    },
    {
        "name": "remove_task_from_tasklist",
        "description": "从任务清单中移除任务。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_guid": {
                    "type": "string",
                    "description": "任务 ID (guid)",
                },
                "tasklist_guid": {
                    "type": "string",
                    "description": "要从哪个清单移除",
                },
            },
            "required": ["task_guid", "tasklist_guid"],
        },
    },
    {
        "name": "get_feishu_task",
        "description": "获取单个飞书任务的详细信息（含子任务数、父任务ID、所属清单）。用于查看某个任务的完整信息。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_guid": {
                    "type": "string",
                    "description": "任务 ID (guid)",
                },
            },
            "required": ["task_guid"],
        },
    },
    {
        "name": "list_feishu_subtasks",
        "description": "列出某个父任务下的所有子任务。支持 keyword 模糊过滤。",
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_task_guid": {
                    "type": "string",
                    "description": "父任务的 ID (guid)",
                },
                "keyword": {
                    "type": "string",
                    "description": "按子任务标题模糊过滤",
                    "default": "",
                },
            },
            "required": ["parent_task_guid"],
        },
    },
    {
        "name": "list_tasklist_tasks",
        "description": (
            "列出某个任务清单内的所有任务（按清单维度查看，能看到所有任务，不限于个人任务列表）。"
            "需要清单 ID（先用 list_feishu_tasklists 获取）。"
            "推荐：找任务时优先用这个工具从清单里找，比 list_feishu_tasks（个人列表）更全。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tasklist_guid": {
                    "type": "string",
                    "description": "任务清单 ID（从 list_feishu_tasklists 获取）",
                },
                "keyword": {
                    "type": "string",
                    "description": "按任务标题模糊过滤",
                    "default": "",
                },
                "completed": {
                    "type": "string",
                    "description": "'true' 只看已完成，'false' 只看未完成，空看全部",
                    "default": "",
                },
            },
            "required": ["tasklist_guid"],
        },
    },
    {
        "name": "create_feishu_subtask",
        "description": "在父任务下创建子任务（飞书任务支持父子层级关系）。需要父任务 ID。",
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_task_guid": {
                    "type": "string",
                    "description": "父任务的 ID (guid)",
                },
                "summary": {
                    "type": "string",
                    "description": "子任务标题",
                },
                "description": {
                    "type": "string",
                    "description": "子任务描述",
                    "default": "",
                },
                "due_time": {
                    "type": "string",
                    "description": "截止时间，格式: 'YYYY-MM-DD HH:MM'",
                    "default": "",
                },
                "assignee_open_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "负责人的 open_id 列表",
                },
            },
            "required": ["parent_task_guid", "summary"],
        },
    },
]

TOOL_MAP = {
    "create_feishu_task": lambda args: create_task(
        summary=args["summary"],
        description=args.get("description", ""),
        due_time=args.get("due_time", ""),
        assignee_open_ids=args.get("assignee_open_ids"),
    ),
    "list_feishu_tasks": lambda args: list_tasks(
        page_size=args.get("page_size", 100),
        completed=args.get("completed", ""),
        keyword=args.get("keyword", ""),
    ),
    "complete_feishu_task": lambda args: complete_task(
        task_guid=args["task_guid"],
    ),
    "reopen_feishu_task": lambda args: reopen_task(
        task_guid=args["task_guid"],
    ),
    "update_feishu_task": lambda args: update_task(
        task_guid=args["task_guid"],
        summary=args.get("summary", ""),
        description=args.get("description", ""),
        due_time=args.get("due_time", ""),
        assignee_open_ids=args.get("assignee_open_ids"),
    ),
    "delete_feishu_task": lambda args: delete_task(
        task_guid=args["task_guid"],
    ),
    "list_feishu_tasklists": lambda args: list_tasklists(
        page_size=args.get("page_size", 50),
        keyword=args.get("keyword", ""),
    ),
    "add_task_to_tasklist": lambda args: add_task_to_tasklist(
        task_guid=args["task_guid"],
        tasklist_guid=args["tasklist_guid"],
        section_guid=args.get("section_guid", ""),
    ),
    "remove_task_from_tasklist": lambda args: remove_task_from_tasklist(
        task_guid=args["task_guid"],
        tasklist_guid=args["tasklist_guid"],
    ),
    "list_tasklist_tasks": lambda args: list_tasklist_tasks(
        tasklist_guid=args["tasklist_guid"],
        keyword=args.get("keyword", ""),
        completed=args.get("completed", ""),
    ),
    "get_feishu_task": lambda args: get_task(
        task_guid=args["task_guid"],
    ),
    "list_feishu_subtasks": lambda args: list_subtasks(
        parent_task_guid=args["parent_task_guid"],
        keyword=args.get("keyword", ""),
    ),
    "create_feishu_subtask": lambda args: create_subtask(
        parent_task_guid=args["parent_task_guid"],
        summary=args["summary"],
        description=args.get("description", ""),
        due_time=args.get("due_time", ""),
        assignee_open_ids=args.get("assignee_open_ids"),
    ),
}
