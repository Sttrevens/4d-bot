from __future__ import annotations


def prefix_tool_name(server_id: str, tool_name: str) -> str:
    clean_server = "".join(ch if ch.isalnum() else "_" for ch in server_id.strip().lower()).strip("_")
    clean_tool = "".join(ch if ch.isalnum() else "_" for ch in tool_name.strip()).strip("_")
    return f"mcp_{clean_server}_{clean_tool}"
