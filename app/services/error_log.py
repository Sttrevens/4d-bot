"""运行时错误日志缓冲区

环形缓冲区记录 bot 运行中遇到的错误，供自我诊断工具查询。
不依赖外部服务，重启后清空。
"""

from __future__ import annotations

import json
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable


@dataclass
class ErrorRecord:
    time: str
    category: str  # tool_error / api_error / unhandled / timeout
    summary: str
    detail: str  # traceback 或完整错误文本
    tool_name: str = ""  # 触发错误的工具名
    tool_args: str = ""  # 工具调用参数（JSON 字符串，截断）


_MAX_RECORDS = 50
_buffer: deque[ErrorRecord] = deque(maxlen=_MAX_RECORDS)

# 错误回调：record_error 时自动调用，用于触发自动修复
# 签名: callback(category: str) -> None
_on_error_callback: Callable[[str], None] | None = None


def set_error_callback(callback: Callable[[str], None]) -> None:
    """设置错误回调（由 main.py 启动时注入 auto_fix.maybe_trigger_fix）"""
    global _on_error_callback
    _on_error_callback = callback


def record_error(
    category: str,
    summary: str,
    detail: str = "",
    exc: BaseException | None = None,
    *,
    tool_name: str = "",
    tool_args: dict | None = None,
) -> None:
    """记录一条错误到缓冲区，并触发自动修复回调。

    Parameters
    ----------
    tool_name : 触发错误的工具名（可选）
    tool_args : 工具调用时的参数字典（可选，自动序列化+截断）
    """
    if exc and not detail:
        detail = traceback.format_exception(type(exc), exc, exc.__traceback__)
        detail = "".join(detail)[-2000:]  # 截断保留最后 2000 字符

    args_str = ""
    if tool_args:
        try:
            args_str = json.dumps(tool_args, ensure_ascii=False, default=str)[:500]
        except Exception:
            args_str = str(tool_args)[:500]

    _buffer.append(ErrorRecord(
        time=datetime.now().isoformat(timespec="seconds"),
        category=category,
        summary=summary[:500],
        detail=detail[:2000],
        tool_name=tool_name,
        tool_args=args_str,
    ))

    # 通知自动修复模块
    if _on_error_callback:
        try:
            _on_error_callback(category)
        except Exception:
            pass  # 回调失败不影响主流程


def get_recent_errors(count: int = 20) -> list[ErrorRecord]:
    """获取最近 N 条错误"""
    return list(_buffer)[-count:]


def format_errors(count: int = 20) -> str:
    """格式化最近错误为文本，供工具返回给 LLM"""
    errors = get_recent_errors(count)
    if not errors:
        return "最近没有错误记录。bot 运行正常。"

    lines = [f"最近 {len(errors)} 条错误：\n"]
    for i, e in enumerate(errors, 1):
        lines.append(f"--- 错误 #{i} [{e.category}] {e.time} ---")
        if e.tool_name:
            lines.append(f"工具: {e.tool_name}")
        if e.tool_args:
            lines.append(f"参数: {e.tool_args}")
        lines.append(f"摘要: {e.summary}")
        if e.detail:
            # 只显示 detail 的最后几行，避免太长
            detail_lines = e.detail.strip().split("\n")
            if len(detail_lines) > 10:
                lines.append("...")
                lines.extend(detail_lines[-10:])
            else:
                lines.append(e.detail.strip())
        lines.append("")
    return "\n".join(lines)


def clear_errors() -> str:
    """清空错误缓冲区"""
    _buffer.clear()
    return "错误缓冲区已清空。"
