"""服务器运维工具 —— 查看运行日志、进程状态

通过 sandbox_caps 能力原语实现。
沙箱代码也可以直接 import sandbox_caps 使用同样的运维能力。
"""

from app.tools.tool_result import ToolResult
from app.tools.sandbox_caps import (
    get_process_info as _get_info,
    read_server_logs as _read_logs,
    search_logs as _search_logs,
)


def get_deploy_status(args: dict) -> ToolResult:
    """查看 bot 进程运行状态。"""
    info = _get_info()

    hours, remainder = divmod(info.uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    lines = [
        f"状态: {info.status}",
        f"运行时间: {hours}h {minutes}m {seconds}s",
        f"内存使用: {info.memory_mb} MB",
        f"PID: {info.pid}",
        f"日志文件: {info.log_file} ({info.log_size_kb} KB)",
    ]
    return ToolResult.success("\n".join(lines))


def get_deploy_logs(args: dict) -> ToolResult:
    """获取 bot 最近的运行日志。"""
    num_lines = args.get("num_lines", 100)
    result = _read_logs(num_lines)
    return ToolResult.success(result)


def search_logs(args: dict) -> ToolResult:
    """在日志中搜索包含关键词的行。"""
    keyword = args.get("keyword", "")
    if not keyword:
        return ToolResult.error("请提供搜索关键词")

    num_lines = args.get("num_lines", 50)
    result = _search_logs(keyword, num_lines)
    return ToolResult.success(result)


TOOL_DEFINITIONS = [
    {
        "name": "get_deploy_status",
        "description": "查看 bot 进程运行状态（运行时间、内存使用等）。用于检查 bot 是否正常运行。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_deploy_logs",
        "description": (
            "获取 bot 最近的运行日志（完整日志，包含 INFO/WARNING/ERROR）。"
            "比 get_bot_errors 更全面：可看到请求处理过程、API 调用细节、工具执行记录等完整上下文。"
            "排查问题时优先使用此工具。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "num_lines": {
                    "type": "integer",
                    "description": "获取最近多少行日志，默认100",
                    "default": 100,
                },
            },
        },
    },
    {
        "name": "search_logs",
        "description": (
            "在运行日志中搜索包含关键词的行。"
            "用于快速定位特定错误或事件，如 search_logs('TimeoutError') 或 search_logs('gemini')。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "要搜索的关键词（不区分大小写）",
                },
                "num_lines": {
                    "type": "integer",
                    "description": "最多返回多少条匹配结果，默认50",
                    "default": 50,
                },
            },
            "required": ["keyword"],
        },
    },
]

TOOL_MAP = {
    "get_deploy_status": get_deploy_status,
    "get_deploy_logs": get_deploy_logs,
    "search_logs": search_logs,
}
