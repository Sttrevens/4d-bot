"""工具返回值类型 —— 替代 [ERROR] 字符串协议

所有工具函数统一返回 ToolResult，调用方用 .ok 判断成功/失败，
不再依赖字符串匹配 "[ERROR]"。

LLM 看到的仍然是 .content 字符串，内部逻辑用 .ok 和 .code 做结构化判断。
迁移期间向后兼容：agent loop 同时处理 ToolResult 和原始 str 返回值。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    content: str
    code: str = ""  # 错误码: not_found, permission, invalid_param, api_error, blocked, internal
    retry_hint: str = ""  # 给 LLM 的重试建议：参数应为何格式、推荐用哪个工具
    outcome: str = "ok"  # ok | partial_success | retryable_error | terminal_error | blocked

    def __str__(self) -> str:
        if self.retry_hint and not self.ok:
            return f"{self.content}\n\n💡 建议: {self.retry_hint}"
        return self.content

    @staticmethod
    def success(content: str) -> ToolResult:
        return ToolResult(ok=True, content=content, outcome="ok")

    @staticmethod
    def partial_success(content: str, code: str = "partial_success", retry_hint: str = "") -> ToolResult:
        return ToolResult(
            ok=True,
            content=content,
            code=code,
            retry_hint=retry_hint,
            outcome="partial_success",
        )

    @staticmethod
    def error(content: str, code: str = "error", retry_hint: str = "") -> ToolResult:
        return ToolResult(
            ok=False,
            content=content,
            code=code,
            retry_hint=retry_hint,
            outcome="terminal_error",
        )

    @staticmethod
    def retryable_error(content: str, code: str = "retryable_error", retry_hint: str = "") -> ToolResult:
        return ToolResult(
            ok=False,
            content=content,
            code=code,
            retry_hint=retry_hint,
            outcome="retryable_error",
        )

    @staticmethod
    def blocked(content: str) -> ToolResult:
        """写入被安全机制拦截（语法错误、缩水保护等）"""
        return ToolResult(ok=False, content=content, code="blocked", outcome="blocked")

    @staticmethod
    def not_found(content: str) -> ToolResult:
        return ToolResult(ok=False, content=content, code="not_found", outcome="terminal_error")

    @staticmethod
    def invalid_param(content: str, retry_hint: str = "") -> ToolResult:
        return ToolResult(
            ok=False,
            content=content,
            code="invalid_param",
            retry_hint=retry_hint,
            outcome="terminal_error",
        )

    @staticmethod
    def api_error(content: str) -> ToolResult:
        return ToolResult(ok=False, content=content, code="api_error", outcome="retryable_error")
