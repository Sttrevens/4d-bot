from __future__ import annotations


_READ_ONLY_TOOLS = {
    "think", "web_search", "fetch_url", "read_file", "search_code",
    "search_files", "list_repository_files", "read_agent_skill_file",
    "list_agent_skill_files", "recall_memory", "read_tool_output",
}
_MESSAGE_TOOLS = {
    "send_message", "send_feishu_message", "send_message_to_user",
    "send_message_to_group", "reply_feishu_message", "notify_admin",
}
_CODE_MUTATION_TOOLS = {
    "self_write_file", "self_edit_file", "edit_file", "write_file",
    "commit_batch",
}
_INFRA_TOOLS = {
    "self_safe_deploy", "restart_instance", "destroy_instance",
    "provision_tenant", "install_package",
}
_PLATFORM_WRITE_TOOLS = {
    "create_calendar_event", "update_calendar_event", "delete_calendar_event",
    "create_feishu_task", "update_feishu_task", "create_feishu_doc",
    "export_file", "export_agent_skill_template",
}


def side_effect_class_for_tool(tool_name: str) -> str:
    if tool_name in _READ_ONLY_TOOLS:
        return "read_only"
    if tool_name in _MESSAGE_TOOLS:
        return "message_send"
    if tool_name in _CODE_MUTATION_TOOLS:
        return "code_mutation"
    if tool_name in _INFRA_TOOLS:
        return "infrastructure"
    if tool_name in _PLATFORM_WRITE_TOOLS:
        return "platform_write"
    return "unknown"


def is_side_effecting(tool_name: str) -> bool:
    return side_effect_class_for_tool(tool_name) not in {"read_only", "unknown"}


def tool_concurrency_key(tool_name: str, args: dict) -> str:
    if side_effect_class_for_tool(tool_name) == "read_only":
        return ""
    target = (
        args.get("event_id")
        or args.get("doc_token")
        or args.get("task_id")
        or args.get("customer_id")
        or args.get("path")
        or args.get("filename")
    )
    if target:
        return f"{tool_name}:{target}"
    if is_side_effecting(tool_name):
        return tool_name
    return ""
